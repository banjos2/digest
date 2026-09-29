from django.core.management.base import BaseCommand, CommandError

from digest_service.operations.backup_service import BackupConfigurationError, run_restore_drill
from digest_service.operations.models import BackupRun


class Command(BaseCommand):
    help = "Восстанавливает копию изолированно и записывает результаты проверок."

    def add_arguments(self, parser):
        parser.add_argument("backup_id")
        parser.add_argument("--target-dsn", default="")

    def handle(self, *args, **options):
        try:
            backup = BackupRun.objects.get(pk=options["backup_id"])
            drill = run_restore_drill(backup, target_dsn=options["target_dsn"])
        except BackupRun.DoesNotExist as exc:
            raise CommandError("backup_not_found") from exc
        except BackupConfigurationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"restore_drill_id={drill.id} checks={drill.checks}"))
