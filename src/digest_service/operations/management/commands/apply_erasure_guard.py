from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.backup_service import BackupConfigurationError
from digest_service.operations.guard_service import apply_current_erasure_guard


class Command(BaseCommand):
    help = "Применяет текущий внешний guard к восстановленной БД и разрешает доставку."

    def handle(self, *args, **options):
        try:
            result = apply_current_erasure_guard()
        except BackupConfigurationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "entries_applied={entries_applied} subscribers_deleted={subscribers_deleted}".format(
                    **result
                )
            )
        )
