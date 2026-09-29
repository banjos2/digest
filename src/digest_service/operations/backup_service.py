import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from .models import BackupRun, RestoreDrill


class BackupConfigurationError(RuntimeError):
    pass


def _digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _artifact_path(name):
    root = Path(settings.BACKUP_OUTPUT_DIR).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise BackupConfigurationError("invalid_artifact_name")
    return path


def _migration_manifest():
    migrations = sorted(f"{app}.{name}" for app, name in MigrationRecorder(connection).applied_migrations())
    encoded = "\n".join(migrations).encode()
    return {
        "migration_count": len(migrations),
        "migration_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _finish_success(run, artifact, *, encrypted, manifest):
    run.status = "succeeded"
    run.encrypted = encrypted
    run.artifact_name = artifact.name
    run.sha256 = _digest(artifact)
    run.size_bytes = artifact.stat().st_size
    run.manifest = manifest
    run.finished_at = timezone.now()
    run.save()
    return run


def _finish_failure(run, code):
    run.status = "failed"
    run.error_code = code
    run.finished_at = timezone.now()
    run.save()


def _remove_partial_artifacts(run):
    for suffix in ("sqlite3", "tmp", "dump.tmp", "dump.age"):
        try:
            _artifact_path(f"database-{run.id}.{suffix}").unlink(missing_ok=True)
        except (OSError, BackupConfigurationError):
            pass


def create_database_backup(*, request_key=None):
    engine = settings.DATABASES["default"]["ENGINE"]
    backend = "postgresql" if "postgresql" in engine else "sqlite"
    storage_class = settings.BACKUP_STORAGE_CLASS
    defaults = {
        "kind": "database",
        "database_backend": backend,
        "storage_class": storage_class,
        "expires_at": timezone.now() + timedelta(days=settings.BACKUP_RETENTION_DAYS),
    }
    if request_key:
        run, created = BackupRun.objects.get_or_create(request_key=request_key, defaults=defaults)
        if not created:
            if run.kind != "database" or run.database_backend != backend:
                raise BackupConfigurationError("backup_request_key_conflict")
            if run.status == "succeeded":
                return run
            if run.status == "running":
                raise BackupConfigurationError("backup_already_running")
            run.status = "running"
            run.error_code = ""
            run.finished_at = None
            run.save()
    else:
        run = BackupRun.objects.create(**defaults)
    try:
        root = Path(settings.BACKUP_OUTPUT_DIR).resolve()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {"format_version": 1, "created_at": timezone.now().isoformat()}
        manifest.update(_migration_manifest())
        if settings.DELIVERY_RECOVERY_GUARD_REQUIRED:
            from .guard_service import delivery_guard_ready
            from .models import DeliverySafetyState

            if not delivery_guard_ready():
                raise BackupConfigurationError("erasure_guard_not_ready")
            manifest["erasure_guard_sha256"] = DeliverySafetyState.objects.get(
                key="global"
            ).applied_guard_sha256
        if backend == "sqlite":
            if not settings.BACKUP_ALLOW_SQLITE_TRAINING or storage_class != "local_training":
                raise BackupConfigurationError("sqlite_backup_is_training_only")
            artifact = _artifact_path(f"database-{run.id}.sqlite3")
            temporary = artifact.with_suffix(".tmp")
            connection.ensure_connection()
            destination = sqlite3.connect(temporary)
            try:
                connection.connection.backup(destination)
            finally:
                destination.close()
            temporary.replace(artifact)
            manifest["scope"] = "local_training"
            return _finish_success(run, artifact, encrypted=False, manifest=manifest)

        if storage_class != "external":
            raise BackupConfigurationError("postgresql_requires_external_storage")
        recipient = settings.BACKUP_AGE_RECIPIENT
        pg_dump = shutil.which("pg_dump")
        age = shutil.which("age")
        if not recipient or not pg_dump or not age:
            raise BackupConfigurationError("backup_tools_or_recipient_missing")
        artifact = _artifact_path(f"database-{run.id}.dump.age")
        raw = _artifact_path(f"database-{run.id}.dump.tmp")
        database = settings.DATABASES["default"]
        environment = os.environ.copy()
        environment["PGPASSWORD"] = database.get("PASSWORD", "")
        dump_command = [
            pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(raw),
            "--host",
            str(database.get("HOST", "")),
            "--port",
            str(database.get("PORT", "5432")),
            "--username",
            str(database.get("USER", "")),
            str(database.get("NAME", "")),
        ]
        try:
            subprocess.run(
                dump_command,
                env=environment,
                capture_output=True,
                check=True,
                timeout=settings.BACKUP_COMMAND_TIMEOUT_SECONDS,
            )
            subprocess.run(
                [age, "--recipient", recipient, "--output", str(artifact), str(raw)],
                capture_output=True,
                check=True,
                timeout=settings.BACKUP_COMMAND_TIMEOUT_SECONDS,
            )
        finally:
            raw.unlink(missing_ok=True)
        manifest["scope"] = "postgresql_full"
        return _finish_success(run, artifact, encrypted=True, manifest=manifest)
    except BackupConfigurationError as exc:
        _remove_partial_artifacts(run)
        _finish_failure(run, str(exc))
        raise
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        _remove_partial_artifacts(run)
        _finish_failure(run, "backup_execution_failed")
        raise BackupConfigurationError("backup_execution_failed") from None


def run_restore_drill(backup, *, target_dsn=""):
    drill = RestoreDrill.objects.create(backup=backup)
    temporary_path = None
    try:
        if backup.status != "succeeded":
            raise BackupConfigurationError("backup_not_ready")
        artifact = _artifact_path(backup.artifact_name)
        if not artifact.is_file() or _digest(artifact) != backup.sha256:
            raise BackupConfigurationError("artifact_hash_mismatch")

        if backup.database_backend == "sqlite":
            if backup.storage_class != "local_training" or backup.encrypted:
                raise BackupConfigurationError("invalid_sqlite_backup_metadata")
            restored = sqlite3.connect(artifact)
            try:
                integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
                migrations = restored.execute("SELECT COUNT(*) FROM django_migrations").fetchone()[0]
                tables = restored.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            finally:
                restored.close()
            checks = {
                "sha256": "ok",
                "integrity_check": integrity,
                "migration_count": migrations,
                "table_count": tables,
            }
            if integrity != "ok" or migrations < 1 or tables < 1:
                raise BackupConfigurationError("restored_database_checks_failed")
        elif backup.database_backend == "postgresql":
            if not target_dsn:
                raise BackupConfigurationError("restore_target_dsn_required")
            age = shutil.which("age")
            pg_restore = shutil.which("pg_restore")
            identity = settings.BACKUP_AGE_IDENTITY_FILE
            if not age or not pg_restore or not identity:
                raise BackupConfigurationError("restore_tools_or_identity_missing")
            from psycopg.conninfo import conninfo_to_dict

            target = conninfo_to_dict(target_dsn)
            source = settings.DATABASES["default"]
            if not str(target.get("dbname", "")).startswith(settings.BACKUP_RESTORE_DATABASE_PREFIX):
                raise BackupConfigurationError("restore_target_name_prefix_required")
            if target.get("dbname") == str(source.get("NAME")) and target.get("host", "") == str(
                source.get("HOST", "")
            ):
                raise BackupConfigurationError("restore_target_matches_live_database")
            handle = tempfile.NamedTemporaryFile(suffix=".dump", delete=False)
            handle.close()
            temporary_path = Path(handle.name)
            subprocess.run(
                [age, "--decrypt", "--identity", identity, "--output", str(temporary_path), str(artifact)],
                capture_output=True,
                check=True,
                timeout=settings.BACKUP_COMMAND_TIMEOUT_SECONDS,
            )
            restore_environment = os.environ.copy()
            for source_key, environment_key in (
                ("host", "PGHOST"),
                ("port", "PGPORT"),
                ("user", "PGUSER"),
                ("password", "PGPASSWORD"),
                ("dbname", "PGDATABASE"),
            ):
                if target.get(source_key):
                    restore_environment[environment_key] = str(target[source_key])
            subprocess.run(
                [
                    pg_restore,
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    str(target["dbname"]),
                    str(temporary_path),
                ],
                env=restore_environment,
                capture_output=True,
                check=True,
                timeout=settings.BACKUP_COMMAND_TIMEOUT_SECONDS,
            )
            import psycopg

            with psycopg.connect(target_dsn) as restored:
                migrations = restored.execute("SELECT COUNT(*) FROM django_migrations").fetchone()[0]
                tables = restored.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"
                ).fetchone()[0]
            checks = {
                "sha256": "ok",
                "pg_restore": "ok",
                "migration_count": migrations,
                "table_count": tables,
            }
            if migrations < 1 or tables < 1:
                raise BackupConfigurationError("restored_database_checks_failed")
        else:
            raise BackupConfigurationError("unsupported_backup_backend")

        drill.status = "succeeded"
        drill.checks = checks
        drill.finished_at = timezone.now()
        drill.save()
        return drill
    except (BackupConfigurationError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, BackupConfigurationError) else "restore_execution_failed"
        drill.status = "failed"
        drill.error_code = code
        drill.finished_at = timezone.now()
        drill.save()
        if isinstance(exc, BackupConfigurationError):
            raise
        raise BackupConfigurationError(code) from None
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def purge_expired_backup_artifacts(*, now=None):
    now = now or timezone.now()
    root = Path(settings.BACKUP_OUTPUT_DIR).resolve()
    purged = 0
    for backup in BackupRun.objects.filter(
        kind="database", status="succeeded", expires_at__lte=now
    ):
        try:
            artifact = _artifact_path(backup.artifact_name)
            artifact.unlink(missing_ok=True)
        except (OSError, BackupConfigurationError):
            continue
        backup.status = "expired"
        backup.save()
        purged += 1
    return {"purged": purged, "storage_root": root.name}
