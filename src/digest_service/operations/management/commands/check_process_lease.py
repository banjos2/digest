from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from digest_service.operations.models import ProcessLease


class Command(BaseCommand):
    help = "Проверить, что постоянный процесс обновляет действующую аренду в базе."

    def add_arguments(self, parser):
        parser.add_argument("name")

    def handle(self, *args, **options):
        name = options["name"]
        lease = ProcessLease.objects.filter(name=name).first()
        if lease is None:
            raise CommandError(f"process_lease_missing={name}")
        if lease.lease_expires_at <= timezone.now():
            raise CommandError(f"process_lease_expired={name}")
        self.stdout.write(self.style.SUCCESS(f"process_lease_ready={name}"))
