import json
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import TestCase, override_settings

from digest_service.catalog.models import Collection
from digest_service.operations.models import ProcessLease
from digest_service.scheduling.models import Schedule
from digest_service.subscriptions.models import SubscriberGeneration
from digest_service.telegram_bot.models import BotReply, TelegramUpdateReceipt
from digest_service.telegram_bot.processor import process_update_payload
from digest_service.telegram_bot.queue_service import claim_next_reply, record_reply_result
from digest_service.telegram_bot.sender import send_bot_reply


def message_update(update_id, text, user_id=40004):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime(2026, 9, 11, tzinfo=UTC).timestamp()),
            "chat": {"id": user_id, "type": "private", "first_name": "Test"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def callback_update(update_id, data, user_id=40004):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat_instance": "fixture-chat-instance",
            "data": data,
            "message": {
                "message_id": update_id,
                "date": int(datetime(2026, 9, 11, tzinfo=UTC).timestamp()),
                "chat": {"id": user_id, "type": "private", "first_name": "Test"},
            },
        },
    }


@override_settings(TELEGRAM_BOT_TOKEN="123456:fixture-token", TELEGRAM_WEBHOOK_SECRET="secret")
class TelegramBotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())
        cls.collection = Collection.objects.get(pk="main")
        cls.collection.is_active = True
        cls.collection.save()
        cls.ai_collection = Collection.objects.get(pk="ai")
        cls.ai_collection.is_active = True
        cls.ai_collection.save()
        cls.schedule = Schedule.objects.get(pk="daily")
        cls.schedule.visible_to_new_subscribers = True
        cls.schedule.delivery_enabled = True
        cls.schedule.save()

    def reply(self, update_id):
        return BotReply.objects.get(receipt__update_id=update_id)

    def test_webhook_requires_secret_and_deduplicates_update(self):
        payload = message_update(1, "/start")
        response = self.client.post(
            "/telegram/webhook/", data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/telegram/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret",
        )
        self.assertEqual(response.status_code, 200)
        duplicate = self.client.post(
            "/telegram/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="secret",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(TelegramUpdateReceipt.objects.count(), 1)
        self.assertEqual(BotReply.objects.count(), 1)
        self.assertEqual(SubscriberGeneration.objects.count(), 1)
        self.assertIn("lang:ru", str(self.reply(1).keyboard))

    def test_complete_english_onboarding_from_callback_buttons(self):
        start, created = process_update_payload(message_update(10, "/start"))
        self.assertTrue(created)
        language_data = start.keyboard[0][1]["callback_data"]
        collections, _ = process_update_payload(callback_update(11, language_data))
        collection_data = collections.keyboard[0][0]["callback_data"]
        selected, _ = process_update_payload(callback_update(12, collection_data))
        done_data = selected.keyboard[-1][0]["callback_data"]
        schedules, _ = process_update_payload(callback_update(13, done_data))
        schedule_data = schedules.keyboard[0][0]["callback_data"]
        review, _ = process_update_payload(callback_update(14, schedule_data))
        self.assertIn("Check your settings", review.text)
        save_data = review.keyboard[0][0]["callback_data"]
        complete, _ = process_update_payload(callback_update(15, save_data))
        subscriber = SubscriberGeneration.objects.get(telegram_user_id=40004)
        self.assertEqual(subscriber.setup_status, "complete")
        self.assertEqual(subscriber.subscription_status, "active")
        self.assertEqual(subscriber.digest_language, "en")
        self.assertEqual(subscriber.current_preferences.selected_collections.count(), 1)
        self.assertIn("All set", complete.text)

        stale, _ = process_update_payload(callback_update(16, collection_data))
        subscriber.refresh_from_db()
        self.assertIn("out of date", stale.text.lower())
        self.assertEqual(subscriber.current_preferences.selected_collections.count(), 1)

    def test_collection_buttons_set_the_named_collection_idempotently(self):
        start, _ = process_update_payload(message_update(17, "/start", user_id=40017))
        language_data = start.keyboard[0][0]["callback_data"]
        collections, _ = process_update_payload(
            callback_update(18, language_data, user_id=40017)
        )
        collection_buttons = [row[0] for row in collections.keyboard[:-1]]
        main_button = next(
            button for button in collection_buttons if ":main:" in button["callback_data"]
        )
        ai_button = next(
            button for button in collection_buttons if ":ai:" in button["callback_data"]
        )
        main_data = main_button["callback_data"]
        ai_data = ai_button["callback_data"]
        self.assertIn(":main:select", main_data)
        self.assertIn(":ai:select", ai_data)

        selected, _ = process_update_payload(callback_update(19, ai_data, user_id=40017))
        subscriber = SubscriberGeneration.objects.get(telegram_user_id=40017)
        self.assertEqual(subscriber.preference_draft.collection_ids, ["ai"])
        selected_buttons = [row[0] for row in selected.keyboard[:-1]]
        self.assertTrue(
            next(button for button in selected_buttons if ":ai:" in button["callback_data"])[
                "text"
            ].startswith("✅")
        )
        self.assertTrue(
            next(button for button in selected_buttons if ":main:" in button["callback_data"])[
                "text"
            ].startswith("▫️")
        )

        repeated, _ = process_update_payload(callback_update(21, ai_data, user_id=40017))
        subscriber.preference_draft.refresh_from_db()
        self.assertEqual(subscriber.preference_draft.collection_ids, ["ai"])
        repeated_buttons = [row[0] for row in repeated.keyboard[:-1]]
        self.assertTrue(
            next(button for button in repeated_buttons if ":ai:" in button["callback_data"])[
                "text"
            ].startswith("✅")
        )

    def test_group_updates_are_ignored_without_storing_content(self):
        payload = message_update(20, "/start")
        payload["message"]["chat"] = {"id": -100500, "type": "group", "title": "Group"}
        reply, created = process_update_payload(payload)
        self.assertTrue(created)
        self.assertIsNone(reply)
        receipt = TelegramUpdateReceipt.objects.get(update_id=20)
        self.assertEqual(receipt.status, "ignored")
        self.assertFalse(BotReply.objects.exists())
        self.assertFalse(SubscriberGeneration.objects.exists())
        self.assertFalse(hasattr(receipt, "payload"))

    def test_reply_queue_retries_and_marks_chat_reachable(self):
        reply, _ = process_update_payload(message_update(30, "/start"))
        claimed = claim_next_reply()
        self.assertEqual(claimed.pk, reply.pk)
        self.assertEqual(claimed.attempt_count, 1)
        record_reply_result(
            reply=claimed,
            outcome="temporary_failure",
            error_code="retry_after",
            retry_after_seconds=60,
        )
        self.assertIsNone(claim_next_reply())
        BotReply.objects.filter(pk=reply.pk).update(
            next_attempt_at=datetime(2020, 1, 1, tzinfo=UTC)
        )
        claimed = claim_next_reply()
        self.assertEqual(claimed.attempt_count, 2)
        record_reply_result(reply=claimed, outcome="sent", telegram_message_id="701")
        reply.refresh_from_db()
        subscriber = SubscriberGeneration.objects.get(telegram_user_id=40004)
        self.assertEqual(reply.status, "sent")
        self.assertEqual(reply.telegram_message_id, "701")
        self.assertEqual(subscriber.chat_reachability, "reachable")

    def test_sender_builds_native_inline_keyboard(self):
        reply, _ = process_update_payload(message_update(40, "/start"))

        class FakeBot:
            def __init__(self):
                self.sent = None

            async def answer_callback_query(self, callback_query_id):
                return True

            async def send_message(self, **kwargs):
                self.sent = kwargs
                return SimpleNamespace(message_id=801)

            async def edit_message_text(self, **kwargs):
                self.sent = kwargs
                return SimpleNamespace(message_id=kwargs["message_id"])

        bot = FakeBot()
        result = async_to_sync(send_bot_reply)(bot, reply)
        self.assertEqual(result.message_id, 801)
        self.assertEqual(bot.sent["chat_id"], 40004)
        self.assertEqual(bot.sent["parse_mode"], "HTML")
        self.assertEqual(bot.sent["reply_markup"].inline_keyboard[0][0].callback_data, "lang:ru")

    def test_callback_edits_the_existing_menu_message(self):
        start, _ = process_update_payload(message_update(41, "/start"))
        language_data = start.keyboard[0][0]["callback_data"]
        reply, _ = process_update_payload(callback_update(42, language_data))

        class FakeBot:
            def __init__(self):
                self.edited = None

            async def answer_callback_query(self, callback_query_id):
                return True

            async def edit_message_text(self, **kwargs):
                self.edited = kwargs
                return SimpleNamespace(message_id=kwargs["message_id"])

        bot = FakeBot()
        result = async_to_sync(send_bot_reply)(bot, reply)
        self.assertEqual(result.message_id, 42)
        self.assertEqual(bot.edited["message_id"], 42)
        self.assertIn("Выберите подборки", bot.edited["text"])

    @patch(
        "digest_service.telegram_bot.management.commands.run_telegram_polling.Command._run",
        new_callable=AsyncMock,
    )
    def test_local_polling_command_releases_receiver_lease(self, run_mock):
        run_mock.return_value = {"updates": 1, "sent": 1}
        output = StringIO()

        call_command(
            "run_telegram_polling",
            once=True,
            timeout=0,
            receive_only=True,
            stdout=output,
        )

        run_mock.assert_awaited_once()
        self.assertTrue(run_mock.await_args.kwargs["options"]["receive_only"])
        self.assertIn("updates=1, queued_messages=1", output.getvalue())
        self.assertFalse(ProcessLease.objects.filter(name="telegram_receiver").exists())
