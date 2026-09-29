from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import AsyncMock, patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from digest_service.catalog.models import Collection
from digest_service.operations.models import BackgroundJob, DeliverySafetyState
from digest_service.operations.service import (
    acquire_process_lease,
    execute_job,
    release_process_lease,
)
from digest_service.scheduling.models import Schedule
from digest_service.subscriptions.erasure import (
    cancel_erasure_confirmation,
    confirm_profile_erasure,
    purge_expired_privacy_records,
    request_profile_erasure,
)
from digest_service.subscriptions.models import (
    ErasureTombstone,
    PreferenceRevision,
    SubscriberGeneration,
)
from digest_service.subscriptions.service import choose_language, save_preferences, start_subscriber
from digest_service.telegram_bot.management.commands.send_telegram_queue import (
    Command as SendCommand,
)
from digest_service.telegram_bot.models import BotReply, TelegramUpdateReceipt
from digest_service.telegram_bot.processor import process_update_payload
from digest_service.telegram_bot.queue_service import claim_next_reply
from digest_service.telegram_bot.recovery import recover_stale_telegram_claims


def message_update(update_id, text, user_id):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime(2026, 9, 11, tzinfo=UTC).timestamp()),
            "chat": {"id": user_id, "type": "private", "first_name": "Privacy"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Privacy"},
            "text": text,
        },
    }


def callback_update(update_id, data, user_id):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"privacy-{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Privacy"},
            "chat_instance": "privacy-fixture",
            "data": data,
            "message": {
                "message_id": update_id,
                "date": int(datetime(2026, 9, 11, tzinfo=UTC).timestamp()),
                "chat": {"id": user_id, "type": "private", "first_name": "Privacy"},
            },
        },
    }


class PrivacyAndProcessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())
        cls.collection = Collection.objects.get(pk="main")
        cls.collection.is_active = True
        cls.collection.save()
        cls.schedule = Schedule.objects.get(pk="daily")
        cls.schedule.visible_to_new_subscribers = True
        cls.schedule.delivery_enabled = True
        cls.schedule.save()

    def subscriber(self, user_id=70001):
        subscriber, _ = start_subscriber(telegram_user_id=user_id, telegram_chat_id=user_id)
        subscriber = choose_language(subscriber=subscriber, language="ru")
        save_preferences(
            subscriber=subscriber,
            collection_ids=[self.collection.pk],
            schedule_id=self.schedule.pk,
        )
        subscriber.refresh_from_db()
        return subscriber

    def test_process_lease_has_one_owner_and_recovers_after_expiry(self):
        now = timezone.now()
        self.assertTrue(acquire_process_lease(name="sender", owner="first", now=now))
        self.assertFalse(acquire_process_lease(name="sender", owner="second", now=now))
        self.assertTrue(
            acquire_process_lease(
                name="sender", owner="second", now=now + timedelta(seconds=301)
            )
        )
        self.assertFalse(release_process_lease(name="sender", owner="first"))
        self.assertTrue(release_process_lease(name="sender", owner="second"))

    @override_settings(TELEGRAM_BOT_TOKEN="123456:fixture-token")
    def test_continuous_sender_once_releases_lease(self):
        command_path = "digest_service.telegram_bot.management.commands.run_telegram_sender"
        with (
            patch.object(SendCommand, "_run_with_bot", new=AsyncMock(return_value=0)),
            patch(f"{command_path}.recover_stale_telegram_claims", return_value={}),
            patch(f"{command_path}.acquire_process_lease", return_value=True) as acquire,
            patch(f"{command_path}.release_process_lease", return_value=True) as release,
        ):
            call_command("run_telegram_sender", once=True, stdout=StringIO())
        self.assertEqual(acquire.call_count, 2)
        release.assert_called_once()

    def test_stale_claim_becomes_unknown_without_automatic_retry(self):
        receipt = TelegramUpdateReceipt.objects.create(
            update_id=90001, payload_hash="b" * 64, status="processed"
        )
        reply = BotReply.objects.create(receipt=receipt, chat_id=70000, text="Fixture")
        claimed_at = timezone.now()
        claim_next_reply(now=claimed_at)
        result = recover_stale_telegram_claims(
            now=claimed_at + timedelta(seconds=601)
        )
        reply.refresh_from_db()
        self.assertEqual(result["replies_unknown"], 1)
        self.assertEqual(reply.status, "unknown")
        self.assertEqual(reply.error_code, "sender_lost_after_claim")

    def test_erasure_confirmation_can_be_cancelled_before_acceptance(self):
        subscriber = self.subscriber()
        request = request_profile_erasure(subscriber=subscriber)
        self.assertEqual(request.status, "pending_confirmation")
        cancel_erasure_confirmation(subscriber=subscriber)
        request.refresh_from_db()
        subscriber.refresh_from_db()
        self.assertEqual(request.status, "cancelled")
        self.assertEqual(subscriber.privacy_state, "normal")

    def test_wrong_or_expired_confirmation_does_not_stop_subscription(self):
        subscriber = self.subscriber()
        now = timezone.now()
        request = request_profile_erasure(subscriber=subscriber, now=now)
        with self.assertRaises(ValidationError):
            confirm_profile_erasure(
                subscriber=subscriber,
                token=request.confirmation_token,
                now=now + timedelta(minutes=6),
            )
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.subscription_status, "active")
        result = purge_expired_privacy_records(now=now + timedelta(minutes=6))
        request.refresh_from_db()
        self.assertEqual(result["confirmations_cancelled"], 1)
        self.assertEqual(request.status, "cancelled")

    def test_confirmed_erasure_removes_working_profile_and_allows_fresh_generation(self):
        original_user_id = 70003
        subscriber = self.subscriber(user_id=original_user_id)
        receipt = TelegramUpdateReceipt.objects.create(
            update_id=90003, payload_hash="a" * 64, status="processed"
        )
        BotReply.objects.create(receipt=receipt, chat_id=original_user_id, text="Stored reply")
        request = request_profile_erasure(subscriber=subscriber)
        confirm_profile_erasure(
            subscriber=subscriber, token=request.confirmation_token
        )
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.privacy_state, "erasure_pending")
        self.assertEqual(subscriber.subscription_status, "inactive")
        job = BackgroundJob.objects.get(kind="erase_subscriber")
        outcome = execute_job(job_id=job.pk, now=job.next_attempt_at, worker_id="privacy-test")
        self.assertEqual(outcome["status"], "done")
        request.refresh_from_db()
        self.assertEqual(request.status, "completed")
        self.assertIsNone(request.subscriber_id)
        self.assertFalse(SubscriberGeneration.objects.filter(pk=subscriber.pk).exists())
        self.assertFalse(PreferenceRevision.objects.filter(subscriber_id=subscriber.pk).exists())
        self.assertFalse(BotReply.objects.filter(chat_id=original_user_id).exists())
        self.assertFalse(TelegramUpdateReceipt.objects.filter(pk=receipt.pk).exists())
        self.assertEqual(ErasureTombstone.objects.get().last_generation, 1)
        self.assertEqual(DeliverySafetyState.objects.get().status, "blocked")
        self.assertTrue(BackgroundJob.objects.filter(kind="export_erasure_guard").exists())
        fresh, created = start_subscriber(
            telegram_user_id=original_user_id, telegram_chat_id=original_user_id
        )
        self.assertTrue(created)
        self.assertEqual(fresh.generation, 2)
        self.assertEqual(fresh.setup_status, "incomplete")

    def test_telegram_data_menu_requires_button_confirmation(self):
        user_id = 70004
        self.subscriber(user_id=user_id)
        prompt, created = process_update_payload(message_update(92001, "/data", user_id))
        self.assertTrue(created)
        self.assertIn("Удалить мои данные", prompt.text)
        callback_data = prompt.keyboard[0][0]["callback_data"]
        accepted, _ = process_update_payload(callback_update(92002, callback_data, user_id))
        subscriber = SubscriberGeneration.objects.get(telegram_user_id=user_id)
        self.assertEqual(subscriber.privacy_state, "erasure_pending")
        self.assertIn("Запрос принят", accepted.text)
        self.assertTrue(BackgroundJob.objects.filter(kind="erase_subscriber").exists())
