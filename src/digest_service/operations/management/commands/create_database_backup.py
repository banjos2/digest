from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.backup_service import (
    BackupConfigurationError,
    create_database_backup,
)


class Command(BaseCommand):
    help = "Создаёт согласованную копию SQLite для разработки или зашифрованный pg_dump."

    def handle(self, *args, **options):
        try:
            run = create_database_backup()
        except BackupConfigurationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"backup_id={run.id} artifact={run.artifact_name} sha256={run.sha256}"
            )
        )
