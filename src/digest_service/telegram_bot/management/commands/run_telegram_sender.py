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
from digest_service.telegram_bot.recovery import recover_stale_telegram_claims


class Command(BaseCommand):
    help = "Постоянно отправлять Telegram-очередь одним активным процессом"

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=int, default=2)
        parser.add_argument("--batch-size", type=int, default=25)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")
        if not 1 <= options["poll_seconds"] <= 60:
            raise CommandError("poll-seconds must be between 1 and 60")
        if not 1 <= options["batch_size"] <= 1000:
            raise CommandError("batch-size must be between 1 and 1000")
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        if not acquire_process_lease(name="telegram_sender", owner=owner):
            raise CommandError("another Telegram sender holds the active lease")
        sender = SendCommand()
        try:
            processed = asyncio.run(self._run(sender=sender, owner=owner, options=options))
            if options["once"]:
                self.stdout.write(self.style.SUCCESS(f"processed={processed}"))
        finally:
            release_process_lease(name="telegram_sender", owner=owner)

    async def _run(self, *, sender, owner, options):
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        try:
            while True:
                await sync_to_async(recover_stale_telegram_claims, thread_sensitive=True)()
                processed = await sender._run_with_bot(bot=bot, limit=options["batch_size"])
                renewed = await sync_to_async(acquire_process_lease, thread_sensitive=True)(
                    name="telegram_sender", owner=owner
                )
                if not renewed:
                    raise CommandError("Telegram sender lost its process lease")
                if options["once"]:
                    return processed
                if processed == 0:
                    await asyncio.sleep(options["poll_seconds"])
        finally:
            await bot.session.close()
