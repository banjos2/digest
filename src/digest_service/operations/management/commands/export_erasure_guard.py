from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.backup_service import BackupConfigurationError
from digest_service.operations.guard_service import create_erasure_guard_export


class Command(BaseCommand):
    help = "Экспортирует актуальный HMAC-реестр удалений в независимое хранилище."

    def handle(self, *args, **options):
        try:
            run = create_erasure_guard_export()
        except BackupConfigurationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"guard_backup_id={run.id} entries={run.manifest['entry_count']} sha256={run.sha256}"
            )
        )
