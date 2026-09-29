from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from digest_service.operations.service import (
    enqueue_maintenance,
    enqueue_source_collection,
    plan_schedule_slots,
)


class Command(BaseCommand):
    help = "Создать слоты рассылок и периодические задания без выполнения"

    def add_arguments(self, parser):
        parser.add_argument("--slot-count", type=int, default=10)

    def handle(self, *args, **options):
        count = options["slot_count"]
        if count < 1 or count > 20:
            raise CommandError("slot-count must be between 1 and 20")
        now = timezone.now()
        slots = plan_schedule_slots(count=count, now=now)
        sources = enqueue_source_collection(now=now)
        maintenance = enqueue_maintenance(now=now)
        self.stdout.write(
            self.style.SUCCESS(
                f"slots={slots}, source_jobs={sources}, maintenance_jobs={maintenance}"
            )
        )
