from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.service import run_due_jobs


class Command(BaseCommand):
    help = "Выполнить готовые задания локально, без RabbitMQ"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 1000:
            raise CommandError("limit must be between 1 and 1000")
        results = run_due_jobs(limit=limit)
        done = sum(row["status"] == "done" for row in results)
        retry = sum(row["status"] == "retry_wait" for row in results)
        failed = sum(row["status"] == "failed" for row in results)
        self.stdout.write(
            self.style.SUCCESS(f"claimed={len(results)}, done={done}, retry={retry}, failed={failed}")
        )
