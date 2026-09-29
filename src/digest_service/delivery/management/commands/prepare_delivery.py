from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from digest_service.delivery.service import prepare_delivery
from digest_service.subscriptions.models import SubscriberGeneration


class Command(BaseCommand):
    help = "Зафиксировать многочастную отправку без вызова Telegram Bot API"

    def add_arguments(self, parser):
        parser.add_argument("subscriber_id")
        parser.add_argument("scheduled_at", help="Плановый момент в ISO 8601 с часовым поясом")
        parser.add_argument(
            "--kind", choices=["automatic", "manual", "correction"], default="automatic"
        )
        parser.add_argument("--request-key", default="")

    def handle(self, *args, **options):
        try:
            subscriber = SubscriberGeneration.objects.get(pk=options["subscriber_id"])
            scheduled_at = parse_datetime(options["scheduled_at"])
            if not scheduled_at or not scheduled_at.tzinfo:
                raise ValidationError("Дата должна содержать часовой пояс.")
            delivery = prepare_delivery(
                subscriber=subscriber,
                scheduled_at=scheduled_at,
                kind=options["kind"],
                request_key=options["request_key"],
            )
        except (SubscriberGeneration.DoesNotExist, ValueError, ValidationError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            self.style.SUCCESS(f"delivery={delivery.pk}, parts={delivery.parts.count()}")
        )
