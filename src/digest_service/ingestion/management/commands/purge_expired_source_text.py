from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from digest_service.ingestion.models import PublicationVersion


class Command(BaseCommand):
    help = "Очистить исходные тексты, срок хранения которых истёк"

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1 or batch_size > 5000:
            raise ValueError("batch-size must be between 1 and 5000")
        now = timezone.now()
        purged = 0
        with transaction.atomic():
            versions = list(
                PublicationVersion.objects.select_for_update(skip_locked=True)
                .filter(expires_at__lte=now, purged_at__isnull=True)
                .order_by("expires_at")[:batch_size]
            )
            for version in versions:
                purged += int(version.purge_content(at=now))
        self.stdout.write(self.style.SUCCESS(f"purged={purged}"))
