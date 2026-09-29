from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.service import publish_outbox


class Command(BaseCommand):
    help = "Передать ожидающие задания из transactional outbox в RabbitMQ"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 1000:
            raise CommandError("limit must be between 1 and 1000")
        self.stdout.write(self.style.SUCCESS(f"published={publish_outbox(limit=limit)}"))
