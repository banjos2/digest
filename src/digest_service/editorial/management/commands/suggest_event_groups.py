from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from digest_service.editorial.grouping import suggest_groupings


class Command(BaseCommand):
    help = "Создать консервативные предложения объединения свежих публикаций"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=48)
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--threshold", type=float, default=0.55)

    def handle(self, *args, **options):
        if options["hours"] < 1 or options["hours"] > 24 * 14:
            raise CommandError("hours must be between 1 and 336")
        if options["limit"] < 2 or options["limit"] > 2000:
            raise CommandError("limit must be between 2 and 2000")
        if options["threshold"] < 0.3 or options["threshold"] > 1:
            raise CommandError("threshold must be between 0.3 and 1")
        created = suggest_groupings(
            since=timezone.now() - timedelta(hours=options["hours"]),
            limit=options["limit"],
            threshold=options["threshold"],
        )
        self.stdout.write(self.style.SUCCESS(f"created={len(created)}"))
