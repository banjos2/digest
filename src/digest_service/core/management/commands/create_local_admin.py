import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create an owner for local development and save its password to an ignored private file."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Local bootstrap is disabled outside DEBUG. Use createsuperuser.")
        path = settings.BASE_DIR / "var/local-admin.txt"
        if get_user_model().objects.filter(username="local-owner").exists():
            self.stdout.write("Local account exists. Credentials were not changed.")
            return
        password = secrets.token_urlsafe(24)
        with transaction.atomic():
            get_user_model().objects.create_superuser("local-owner", password=password)
            # Exclusive creation prevents replacing a previous credentials file.
            with path.open("x", encoding="utf8") as stream:
                stream.write(
                    f"Local development only\nUsername: local-owner\nPassword: {password}\n"
                )
        self.stdout.write(
            "Created local-owner. Credentials are in var/local-admin.txt (not printed or committed)."
        )
