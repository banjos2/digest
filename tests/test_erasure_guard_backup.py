import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from digest_service.operations.backup_service import BackupConfigurationError
from digest_service.operations.guard_service import (
    apply_current_erasure_guard,
    create_erasure_guard_export,
    delivery_guard_ready,
    mark_delivery_guard_blocked,
)
from digest_service.operations.models import BackupRun, DeliverySafetyState
from digest_service.subscriptions.models import ErasureTombstone, SubscriberGeneration
from digest_service.subscriptions.privacy import erasure_subject_hash
from digest_service.telegram_bot.models import BotReply, TelegramUpdateReceipt


class ErasureGuardBackupTests(TestCase):
    def settings(self, directory, **extra):
        values = {
            "ERASURE_GUARD_OUTPUT_DIR": Path(directory),
            "ERASURE_GUARD_STORAGE_CLASS": "local_training",
            "ERASURE_GUARD_BACKUP_RETENTION_DAYS": 60,
            "BACKUP_ALLOW_SQLITE_TRAINING": True,
            "DELIVERY_RECOVERY_GUARD_REQUIRED": True,
        }
        values.update(extra)
        return override_settings(**values)

    def test_export_writes_independent_artifact_and_unlocks_matching_state(self):
        now = timezone.now()
        ErasureTombstone.objects.create(
            subject_hash=erasure_subject_hash(81234),
            last_generation=1,
            completed_at=now,
            expires_at=now + timedelta(days=45),
        )
        with tempfile.TemporaryDirectory() as directory, self.settings(directory):
            export = create_erasure_guard_export(now=now)
            marker = json.loads((Path(directory) / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(export.kind, "erasure_guard")
            self.assertEqual(export.manifest["entry_count"], 1)
            self.assertEqual(marker["artifact_sha256"], export.sha256)
            self.assertTrue(delivery_guard_ready())

            mark_delivery_guard_blocked(reason="restore_started")
            self.assertFalse(delivery_guard_ready())

    def test_apply_guard_removes_resurrected_generation_then_unlocks(self):
        user_id = 82345
        now = timezone.now()
        ErasureTombstone.objects.create(
            subject_hash=erasure_subject_hash(user_id),
            last_generation=1,
            completed_at=now,
            expires_at=now + timedelta(days=45),
        )
        with tempfile.TemporaryDirectory() as directory, self.settings(directory):
            export = create_erasure_guard_export(now=now)
            ErasureTombstone.objects.all().delete()
            DeliverySafetyState.objects.all().delete()
            subscriber = SubscriberGeneration.objects.create(
                telegram_user_id=user_id,
                telegram_chat_id=user_id,
                generation=1,
            )

            result = apply_current_erasure_guard(now=now + timedelta(minutes=1))

            self.assertEqual(result, {"entries_applied": 1, "subscribers_deleted": 1})
            self.assertFalse(SubscriberGeneration.objects.filter(pk=subscriber.pk).exists())
            self.assertEqual(ErasureTombstone.objects.get().last_generation, 1)
            self.assertEqual(
                DeliverySafetyState.objects.get().applied_guard_sha256, export.sha256
            )
            self.assertTrue(delivery_guard_ready())

    def test_tampered_guard_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory, self.settings(directory):
            export = create_erasure_guard_export()
            (Path(directory) / export.artifact_name).write_text("tampered", encoding="utf-8")
            with self.assertRaises(BackupConfigurationError):
                apply_current_erasure_guard()
            self.assertEqual(DeliverySafetyState.objects.get().status, "blocked")

    def test_guard_preserves_a_legitimate_newer_generation_and_its_reply(self):
        user_id = 83456
        now = timezone.now()
        ErasureTombstone.objects.create(
            subject_hash=erasure_subject_hash(user_id),
            last_generation=1,
            completed_at=now,
            expires_at=now + timedelta(days=45),
        )
        with tempfile.TemporaryDirectory() as directory, self.settings(directory):
            create_erasure_guard_export(now=now)
            ErasureTombstone.objects.all().delete()
            DeliverySafetyState.objects.all().delete()
            old = SubscriberGeneration.objects.create(
                telegram_user_id=user_id,
                telegram_chat_id=user_id,
                generation=1,
                is_current=False,
            )
            current = SubscriberGeneration.objects.create(
                telegram_user_id=user_id,
                telegram_chat_id=user_id,
                generation=2,
            )
            receipt = TelegramUpdateReceipt.objects.create(
                update_id=83456,
                payload_hash="c" * 64,
                status="processed",
            )
            reply = BotReply.objects.create(receipt=receipt, chat_id=user_id, text="New reply")

            apply_current_erasure_guard(now=now + timedelta(minutes=1))

            self.assertFalse(SubscriberGeneration.objects.filter(pk=old.pk).exists())
            self.assertTrue(SubscriberGeneration.objects.filter(pk=current.pk).exists())
            self.assertTrue(BotReply.objects.filter(pk=reply.pk).exists())

    def test_external_guard_requires_separate_storage(self):
        with tempfile.TemporaryDirectory() as directory, self.settings(
            directory,
            ERASURE_GUARD_STORAGE_CLASS="external",
            BACKUP_OUTPUT_DIR=Path(directory),
        ):
            with self.assertRaises(BackupConfigurationError):
                create_erasure_guard_export()
            self.assertEqual(
                BackupRun.objects.latest("created_at").error_code,
                "guard_storage_must_be_separate",
            )

    @override_settings(
        TELEGRAM_BOT_TOKEN="123456:fixture-token",
        DELIVERY_RECOVERY_GUARD_REQUIRED=True,
    )
    def test_sender_refuses_to_start_without_current_guard(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(
            ERASURE_GUARD_OUTPUT_DIR=Path(directory)
        ):
            with self.assertRaises(CommandError):
                call_command("send_telegram_queue", limit=1, stdout=StringIO())
