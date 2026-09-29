from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

PROJECT_APPS = {
    "core",
    "catalog",
    "ingestion",
    "ai_gateway",
    "editorial",
    "scheduling",
    "subscriptions",
    "delivery",
    "telegram_bot",
    "operations",
}

ROLE_RULES = {
    "catalog_manager": {
        "view": {"core", "catalog", "scheduling"},
        "add": {"catalog", "scheduling"},
        "change": {"catalog", "scheduling"},
    },
    "editor": {
        "view": {"catalog", "scheduling", "ingestion", "editorial", "ai_gateway", "operations"},
        "add": {"editorial"},
        "change": {"editorial"},
    },
    "support": {
        "view": {"catalog", "scheduling", "subscriptions", "delivery", "telegram_bot", "operations"},
    },
    "privacy_operator": {
        "view": {"catalog", "scheduling", "subscriptions", "delivery", "telegram_bot", "operations"},
        "change_models": {("subscriptions", "erasurerequest")},
    },
    "auditor": {"view": PROJECT_APPS},
}


def permissions_for_rule(rule):
    permission_ids = set()
    for action in ("view", "add", "change"):
        apps = rule.get(action, set())
        permission_ids.update(
            Permission.objects.filter(
                content_type__app_label__in=apps,
                codename__startswith=f"{action}_",
            ).values_list("id", flat=True)
        )
    for app_label, model in rule.get("change_models", set()):
        permission_ids.update(
            Permission.objects.filter(
                content_type__app_label=app_label,
                codename=f"change_{model}",
            ).values_list("id", flat=True)
        )
    return Permission.objects.filter(pk__in=permission_ids)


class Command(BaseCommand):
    help = "Создаёт стандартные группы сотрудников и синхронизирует минимальные права."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Только проверить расхождения.")

    @transaction.atomic
    def handle(self, *args, **options):
        differences = []
        for role, rule in ROLE_RULES.items():
            group = Group.objects.filter(name=role).first()
            if group is None:
                differences.append(role)
                if options["check"]:
                    continue
                group = Group.objects.create(name=role)
            expected = set(permissions_for_rule(rule).values_list("id", flat=True))
            actual = set(group.permissions.values_list("id", flat=True))
            if actual != expected:
                differences.append(role)
                if not options["check"]:
                    group.permissions.set(expected)
        if options["check"] and differences:
            raise CommandError(f"roles_out_of_sync={','.join(differences)}")
        action = "checked" if options["check"] else "configured"
        self.stdout.write(self.style.SUCCESS(f"{action}={len(ROLE_RULES)}"))
