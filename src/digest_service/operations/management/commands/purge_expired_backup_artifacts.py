from django.core.management.base import BaseCommand

from digest_service.operations.backup_service import purge_expired_backup_artifacts
from digest_service.operations.guard_service import purge_expired_guard_artifacts


class Command(BaseCommand):
    help = "Удаляет только истёкшие артефакты из настроенного каталога резервных копий."

    def handle(self, *args, **options):
        result = purge_expired_backup_artifacts()
        guards_purged = purge_expired_guard_artifacts()
        self.stdout.write(
            self.style.SUCCESS(
                f"database_backups_purged={result['purged']} guards_purged={guards_purged}"
            )
        )
