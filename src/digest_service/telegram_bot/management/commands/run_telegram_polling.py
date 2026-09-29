import asyncio
import os
import socket
import uuid

from aiogram import Bot
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.service import acquire_process_lease, release_process_lease
from digest_service.telegram_bot.management.commands.send_telegram_queue import (
    Command as SendCommand,
)
from digest_service.telegram_bot.processor import process_update_payload


class Command(BaseCommand):
    help = "Получать команды бота через long polling без публичного HTTPS webhook"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--timeout", type=int, default=30)
        parser.add_argument("--reply-limit", type=int, default=100)
        parser.add_argument(
            "--receive-only",
            action="store_true",
            help="Только принимать updates; отправку выполняет отдельный run_telegram_sender.",
        )
        parser.add_argument(
            "--delete-webhook",
            action="store_true",
            help="Удалить ранее настроенный webhook перед запуском polling.",
        )

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")
        if not 0 <= options["timeout"] <= 50:
            raise CommandError("timeout must be between 0 and 50")
        if not 1 <= options["reply_limit"] <= 1000:
            raise CommandError("reply-limit must be between 1 and 1000")
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        if not acquire_process_lease(name="telegram_receiver", owner=owner):
            raise CommandError("another Telegram receiver holds the active lease")
        try:
            result = asyncio.run(self._run(owner=owner, options=options))
        finally:
            release_process_lease(name="telegram_receiver", owner=owner)
        if options["once"]:
            self.stdout.write(
                self.style.SUCCESS(f"updates={result['updates']}, queued_messages={result['sent']}")
            )

    async def _run(self, *, owner, options):
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        sender = SendCommand()
        offset = None
        updates_processed = 0
        messages_sent = 0
        try:
            webhook = await bot.get_webhook_info()
            if webhook.url:
                if options["delete_webhook"]:
                    await bot.delete_webhook(drop_pending_updates=False)
                else:
                    raise CommandError(
                        "Bot has an active webhook; remove it before starting polling"
                    )
            while True:
                timeout = 0 if options["once"] else options["timeout"]
                updates = await bot.get_updates(
                    offset=offset,
                    limit=100,
                    timeout=timeout,
                    allowed_updates=["message", "callback_query"],
                    request_timeout=timeout + 10,
                )
                for update in updates:
                    payload = update.model_dump(mode="json", exclude_none=True)
                    await sync_to_async(process_update_payload, thread_sensitive=True)(payload)
                    offset = max(offset or 0, update.update_id + 1)
                    updates_processed += 1
                if not options["receive_only"]:
                    messages_sent += await sender._run_with_bot(
                        bot=bot, limit=options["reply_limit"]
                    )
                renewed = await sync_to_async(acquire_process_lease, thread_sensitive=True)(
                    name="telegram_receiver", owner=owner
                )
                if not renewed:
                    raise CommandError("Telegram receiver lost its process lease")
                if options["once"]:
                    return {"updates": updates_processed, "sent": messages_sent}
        finally:
            await bot.session.close()
