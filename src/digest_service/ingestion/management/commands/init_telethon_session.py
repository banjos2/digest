from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from telethon.sync import TelegramClient


class Command(BaseCommand):
    help = "Интерактивно создать локальную сессию выделенного Telegram-аккаунта"

    def handle(self, *args, **options):
        if not settings.TELEGRAM_SOURCE_API_ID or not settings.TELEGRAM_SOURCE_API_HASH:
            raise CommandError("telethon_credentials_missing")
        if not settings.TELEGRAM_SOURCE_PHONE:
            raise CommandError("telethon_phone_missing")
        session_path = Path(settings.TELEGRAM_SOURCE_SESSION_PATH)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(session_path),
            settings.TELEGRAM_SOURCE_API_ID,
            settings.TELEGRAM_SOURCE_API_HASH,
        )
        try:
            client.start(phone=settings.TELEGRAM_SOURCE_PHONE)
            account = client.get_me()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Telethon session authorized: account_id={account.id}, session={session_path}.session"
                )
            )
        finally:
            client.disconnect()
