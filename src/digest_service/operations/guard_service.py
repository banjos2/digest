import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from digest_service.subscriptions.erasure import delete_subscriber_records
from digest_service.subscriptions.models import ErasureTombstone, SubscriberGeneration
from digest_service.subscriptions.privacy import erasure_subject_hash

from .backup_service import BackupConfigurationError
from .models import BackupRun, DeliverySafetyState


def _root():
    return Path(settings.ERASURE_GUARD_OUTPUT_DIR).resolve()


def _path(name):
    root = _root()
    path = (root / name).resolve()
    if path.parent != root:
        raise BackupConfigurationError("invalid_guard_artifact_name")
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@transaction.atomic
def mark_delivery_guard_blocked(*, reason):
    state, _ = DeliverySafetyState.objects.select_for_update().get_or_create(key="global")
    state.status = "blocked"
    state.reason = reason[:100]
    state.save()
    return state


def _marker():
    try:
        payload = json.loads(_path("current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    required = {"format_version", "artifact_name", "artifact_sha256", "encrypted", "exported_at"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        return None
    try:
        int(payload["artifact_sha256"], 16)
        exported_at = datetime.fromisoformat(payload["exported_at"])
    except (TypeError, ValueError):
        return None
    if (
        payload["format_version"] != 1
        or not isinstance(payload["artifact_name"], str)
        or not payload["artifact_name"]
        or not isinstance(payload["encrypted"], bool)
        or len(payload["artifact_sha256"]) != 64
        or timezone.is_naive(exported_at)
    ):
        return None
    return payload


def delivery_guard_ready():
    if not settings.DELIVERY_RECOVERY_GUARD_REQUIRED:
        return True
    marker = _marker()
    state = DeliverySafetyState.objects.filter(key="global", status="ready").first()
    return bool(
        marker
        and state
        and marker["artifact_sha256"] == state.applied_guard_sha256
        and marker["artifact_name"]
    )


def _set_ready(sha256):
    with transaction.atomic():
        state, _ = DeliverySafetyState.objects.select_for_update().get_or_create(key="global")
        state.status = "ready"
        state.applied_guard_sha256 = sha256
        state.reason = ""
        state.applied_at = timezone.now()
        state.save()


def create_erasure_guard_export(*, request_key=None, now=None):
    now = now or timezone.now()
    storage_class = settings.ERASURE_GUARD_STORAGE_CLASS
    defaults = {
        "kind": "erasure_guard",
        "database_backend": "portable_json",
        "storage_class": storage_class,
        "expires_at": now + timedelta(days=settings.ERASURE_GUARD_BACKUP_RETENTION_DAYS),
    }
    if request_key:
        run, created = BackupRun.objects.get_or_create(request_key=request_key, defaults=defaults)
        if not created:
            if run.kind != "erasure_guard":
                raise BackupConfigurationError("guard_request_key_conflict")
            if run.status == "succeeded":
                marker = _marker()
                if marker and marker["artifact_sha256"] == run.sha256:
                    _set_ready(run.sha256)
                    return run
            if run.status == "running":
                raise BackupConfigurationError("guard_export_already_running")
            run.status = "running"
            run.error_code = ""
            run.finished_at = None
            run.save()
    else:
        run = BackupRun.objects.create(**defaults)

    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    if storage_class == "external" and root == Path(settings.BACKUP_OUTPUT_DIR).resolve():
        run.status = "failed"
        run.error_code = "guard_storage_must_be_separate"
        run.finished_at = timezone.now()
        run.save()
        raise BackupConfigurationError(run.error_code)
    payload = {
        "format_version": 1,
        "exported_at": now.isoformat(),
        "entries": [
            {
                "subject_hash": row.subject_hash,
                "last_generation": row.last_generation,
                "completed_at": row.completed_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
            }
            for row in ErasureTombstone.objects.filter(expires_at__gt=now).order_by("subject_hash")
        ],
    }
    raw = _path(f"erasure-guard-{run.id}.json.tmp")
    artifact = _path(f"erasure-guard-{run.id}.json")
    marker_tmp = _path("current.json.tmp")
    encrypted = False
    try:
        raw.write_bytes(_canonical_json(payload))
        if storage_class == "external":
            age = shutil.which("age")
            if not age or not settings.BACKUP_AGE_RECIPIENT:
                raise BackupConfigurationError("guard_encryption_not_configured")
            artifact = _path(f"erasure-guard-{run.id}.json.age")
            subprocess.run(
                [
                    age,
                    "--recipient",
                    settings.BACKUP_AGE_RECIPIENT,
                    "--output",
                    str(artifact),
                    str(raw),
                ],
                capture_output=True,
                check=True,
                timeout=settings.BACKUP_COMMAND_TIMEOUT_SECONDS,
            )
            encrypted = True
        elif storage_class == "local_training" and settings.BACKUP_ALLOW_SQLITE_TRAINING:
            raw.replace(artifact)
        else:
            raise BackupConfigurationError("plaintext_guard_is_training_only")

        artifact_hash = _sha256(artifact)
        marker = {
            "format_version": 1,
            "artifact_name": artifact.name,
            "artifact_sha256": artifact_hash,
            "encrypted": encrypted,
            "exported_at": now.isoformat(),
        }
        marker_tmp.write_bytes(_canonical_json(marker))
        marker_tmp.replace(_path("current.json"))

        run.status = "succeeded"
        run.encrypted = encrypted
        run.artifact_name = artifact.name
        run.sha256 = artifact_hash
        run.size_bytes = artifact.stat().st_size
        run.manifest = {"format_version": 1, "entry_count": len(payload["entries"])}
        run.finished_at = timezone.now()
        run.save()
        _set_ready(artifact_hash)
        return run
    except BackupConfigurationError as exc:
        run.status = "failed"
        run.error_code = str(exc)
        run.finished_at = timezone.now()
        run.save()
        raise
    except (OSError, subprocess.SubprocessError):
        run.status = "failed"
        run.error_code = "guard_export_failed"
        run.finished_at = timezone.now()
        run.save()
        raise BackupConfigurationError(run.error_code) from None
    finally:
        raw.unlink(missing_ok=True)
        marker_tmp.unlink(missing_ok=True)
        if run.status != "succeeded":
            artifact.unlink(missing_ok=True)


def _load_guard_payload(marker):
    artifact = _path(marker["artifact_name"])
    if not artifact.is_file() or _sha256(artifact) != marker["artifact_sha256"]:
        raise BackupConfigurationError("guard_artifact_hash_mismatch")
    temporary = None
    try:
        source = artifact
        if marker["encrypted"]:
            age = shutil.which("age")
            identity = settings.BACKUP_AGE_IDENTITY_FILE
            if not age or not identity:
                raise BackupConfigurationError("guard_decryption_not_configured")
            handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            handle.close()
            temporary = Path(handle.name)
            subprocess.run(
                [age, "--decrypt", "--identity", identity, "--output", str(temporary), str(artifact)],
                capture_output=True,
                check=True,
                timeout=settings.BACKUP_COMMAND_TIMEOUT_SECONDS,
            )
            source = temporary
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise BackupConfigurationError("guard_payload_unreadable") from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise BackupConfigurationError("guard_payload_version_invalid")
    if not isinstance(payload.get("entries"), list):
        raise BackupConfigurationError("guard_entries_invalid")
    return payload


def _validated_entries(payload, *, now):
    entries = {}
    for item in payload["entries"]:
        try:
            subject_hash = item["subject_hash"]
            int(subject_hash, 16)
            completed_at = datetime.fromisoformat(item["completed_at"])
            expires_at = datetime.fromisoformat(item["expires_at"])
            generation = int(item["last_generation"])
        except (KeyError, TypeError, ValueError):
            raise BackupConfigurationError("guard_entry_invalid") from None
        if len(subject_hash) != 64 or generation < 1 or subject_hash in entries:
            raise BackupConfigurationError("guard_entry_invalid")
        if timezone.is_naive(completed_at) or timezone.is_naive(expires_at):
            raise BackupConfigurationError("guard_entry_invalid")
        if expires_at <= completed_at:
            raise BackupConfigurationError("guard_entry_invalid")
        if expires_at > now:
            entries[subject_hash] = (generation, completed_at, expires_at)
    return entries


def apply_current_erasure_guard(*, now=None):
    now = now or timezone.now()
    mark_delivery_guard_blocked(reason="guard_application_in_progress")
    marker = _marker()
    if not marker:
        raise BackupConfigurationError("guard_marker_missing")
    payload = _load_guard_payload(marker)
    entries = _validated_entries(payload, now=now)
    deleted = 0
    with transaction.atomic():
        subscribers = list(SubscriberGeneration.objects.select_for_update().all())
        for subscriber in subscribers:
            entry = entries.get(erasure_subject_hash(subscriber.telegram_user_id))
            if entry and subscriber.generation <= entry[0]:
                newer_generation_exists = SubscriberGeneration.objects.filter(
                    telegram_user_id=subscriber.telegram_user_id,
                    generation__gt=entry[0],
                ).exists()
                delete_subscriber_records(
                    subscriber=subscriber,
                    delete_chat_records=not newer_generation_exists,
                )
                deleted += 1
        for subject_hash, (generation, completed_at, expires_at) in entries.items():
            current = ErasureTombstone.objects.filter(pk=subject_hash).first()
            if (
                not current
                or current.last_generation < generation
                or (
                    current.last_generation == generation
                    and current.completed_at <= completed_at
                )
            ):
                ErasureTombstone.objects.update_or_create(
                    subject_hash=subject_hash,
                    defaults={
                        "last_generation": generation,
                        "completed_at": completed_at,
                        "expires_at": expires_at,
                    },
                )
        state, _ = DeliverySafetyState.objects.select_for_update().get_or_create(key="global")
        state.status = "ready"
        state.applied_guard_sha256 = marker["artifact_sha256"]
        state.reason = ""
        state.applied_at = now
        state.save()
    return {"entries_applied": len(entries), "subscribers_deleted": deleted}


def purge_expired_guard_artifacts(*, now=None):
    now = now or timezone.now()
    marker = _marker() or {}
    current_name = marker.get("artifact_name")
    purged = 0
    for backup in BackupRun.objects.filter(
        kind="erasure_guard", status="succeeded", expires_at__lte=now
    ).exclude(artifact_name=current_name):
        try:
            _path(backup.artifact_name).unlink(missing_ok=True)
        except (OSError, BackupConfigurationError):
            continue
        backup.status = "expired"
        backup.save()
        purged += 1
    return purged
