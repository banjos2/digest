import hashlib
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings

from digest_service.operations.backup_service import (
    BackupConfigurationError,
    create_database_backup,
    purge_expired_backup_artifacts,
    run_restore_drill,
)
from digest_service.operations.guard_service import create_erasure_guard_export
from digest_service.operations.models import BackupRun, RestoreDrill


class StaffRoleTests(TestCase):
    def test_roles_are_idempotent_and_never_receive_delete_permissions(self):
        call_command("configure_staff_roles", stdout=StringIO())
        call_command("configure_staff_roles", stdout=StringIO())
        call_command("configure_staff_roles", "--check", stdout=StringIO())
        self.assertEqual(Group.objects.filter(name__in=[
            "catalog_manager", "editor", "support", "privacy_operator", "auditor"
        ]).count(), 5)
        for group in Group.objects.all():
            self.assertFalse(group.permissions.filter(codename__startswith="delete_").exists())

    def test_editor_and_privacy_permissions_are_separated(self):
        call_command("configure_staff_roles", stdout=StringIO())
        editor = Group.objects.get(name="editor")
        privacy = Group.objects.get(name="privacy_operator")
        self.assertTrue(editor.permissions.filter(codename="change_event").exists())
        self.assertFalse(editor.permissions.filter(codename="view_erasuretombstone").exists())
        self.assertTrue(privacy.permissions.filter(codename="change_erasurerequest").exists())
        self.assertFalse(privacy.permissions.filter(codename="change_subscribergeneration").exists())


class BackupTests(TransactionTestCase):
    def test_database_backup_records_current_independent_guard(self):
        with (
            tempfile.TemporaryDirectory() as backup_directory,
            tempfile.TemporaryDirectory() as guard_directory,
            override_settings(
                BACKUP_OUTPUT_DIR=Path(backup_directory),
                BACKUP_STORAGE_CLASS="local_training",
                BACKUP_ALLOW_SQLITE_TRAINING=True,
                ERASURE_GUARD_OUTPUT_DIR=Path(guard_directory),
                ERASURE_GUARD_STORAGE_CLASS="local_training",
                DELIVERY_RECOVERY_GUARD_REQUIRED=True,
            ),
        ):
            guard = create_erasure_guard_export()
            backup = create_database_backup()
            self.assertEqual(backup.manifest["erasure_guard_sha256"], guard.sha256)

    def test_local_backup_and_isolated_restore_drill(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            BACKUP_OUTPUT_DIR=Path(directory),
            BACKUP_STORAGE_CLASS="local_training",
            BACKUP_ALLOW_SQLITE_TRAINING=True,
            BACKUP_RETENTION_DAYS=30,
        ):
            backup = create_database_backup()
            artifact = Path(directory) / backup.artifact_name
            self.assertTrue(artifact.is_file())
            self.assertEqual(backup.status, "succeeded")
            self.assertFalse(backup.encrypted)
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), backup.sha256)

            drill = run_restore_drill(backup)
            self.assertEqual(drill.status, "succeeded")
            self.assertEqual(drill.checks["integrity_check"], "ok")
            self.assertGreater(drill.checks["migration_count"], 0)

            backup.expires_at = backup.created_at
            backup.save()
            result = purge_expired_backup_artifacts(now=backup.created_at)
            backup.refresh_from_db()
            self.assertEqual(result["purged"], 1)
            self.assertEqual(backup.status, "expired")
            self.assertFalse(artifact.exists())

    def test_tampered_backup_fails_restore_drill(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            BACKUP_OUTPUT_DIR=Path(directory),
            BACKUP_STORAGE_CLASS="local_training",
            BACKUP_ALLOW_SQLITE_TRAINING=True,
        ):
            backup = create_database_backup()
            with (Path(directory) / backup.artifact_name).open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaises(BackupConfigurationError):
                run_restore_drill(backup)
            self.assertEqual(RestoreDrill.objects.get(backup=backup).status, "failed")
            self.assertEqual(
                RestoreDrill.objects.get(backup=backup).error_code, "artifact_hash_mismatch"
            )

    @override_settings(BACKUP_ALLOW_SQLITE_TRAINING=False, BACKUP_STORAGE_CLASS="external")
    def test_sqlite_is_refused_as_production_backup(self):
        with self.assertRaises(BackupConfigurationError):
            create_database_backup()
        self.assertEqual(BackupRun.objects.latest("created_at").error_code, "sqlite_backup_is_training_only")

    def test_role_check_detects_manual_permission_drift(self):
        call_command("configure_staff_roles", stdout=StringIO())
        group = Group.objects.get(name="editor")
        group.permissions.clear()
        with self.assertRaises(CommandError):
            call_command("configure_staff_roles", "--check", stdout=StringIO())
