from django.core.management.base import BaseCommand, CommandError

from digest_service.ai_gateway.openrouter_provider import (
    AIProviderUnavailable,
    OpenRouterProvider,
)
from digest_service.ai_gateway.service import AIBudgetExceeded
from digest_service.editorial.models import Event
from digest_service.editorial.service import draft_russian_card, translate_english_card


class Command(BaseCommand):
    help = "Создать связанные RU и EN карточки события через настроенный OpenRouter API"

    def add_arguments(self, parser):
        parser.add_argument("event_id")
        parser.add_argument("slot_key")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        try:
            event = Event.objects.get(pk=options["event_id"])
        except (Event.DoesNotExist, ValueError) as error:
            raise CommandError("event_not_found") from error
        try:
            provider = OpenRouterProvider()
            russian = draft_russian_card(
                event=event,
                slot_key=options["slot_key"],
                provider=provider,
                force=options["force"],
            )
            english = translate_english_card(
                russian_card=russian,
                provider=provider,
                force=options["force"],
            )
        except (AIProviderUnavailable, AIBudgetExceeded) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"ru={russian.pk}, en={english.pk}"))
