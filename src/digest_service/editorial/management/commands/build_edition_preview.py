from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from digest_service.catalog.models import Collection
from digest_service.editorial.edition_service import (
    build_english_edition,
    build_russian_edition,
    get_or_create_edition,
)
from digest_service.editorial.models import EventCardRevision
from digest_service.scheduling.models import Schedule


def _ids(value):
    return [int(item) for item in value.split(",") if item.strip()]


class Command(BaseCommand):
    help = "Собрать полный RU/EN-предпросмотр ближайшего выпуска"

    def add_arguments(self, parser):
        parser.add_argument("collection_id")
        parser.add_argument("schedule_id")
        parser.add_argument("--main", required=True, help="RU card IDs separated by commas")
        parser.add_argument("--additional", default="", help="RU card IDs separated by commas")
        parser.add_argument("--after", help="Aware ISO datetime used to choose the next slot")

    def handle(self, *args, **options):
        try:
            collection = Collection.objects.get(pk=options["collection_id"])
            schedule = Schedule.objects.get(pk=options["schedule_id"])
            after = parse_datetime(options["after"]) if options["after"] else timezone.now()
            if not after or not after.tzinfo:
                raise ValueError
            timing = schedule.preview(after=after)[0]
            main_ids = _ids(options["main"])
            additional_ids = _ids(options["additional"])
            cards = {
                card.pk: card
                for card in EventCardRevision.objects.filter(
                    pk__in=main_ids + additional_ids, language="ru", is_current=True
                )
            }
            if len(cards) != len(set(main_ids + additional_ids)):
                raise ValidationError("Не все RU-карточки найдены или актуальны.")
            edition = get_or_create_edition(
                collection=collection,
                schedule_revision=schedule.revisions.get(number=schedule.revision),
                scheduled_at=timing["scheduled_at"],
                window_start=timing["window_start"],
                window_end=timing["window_end"],
            )
            russian = build_russian_edition(
                edition=edition,
                main_cards=[cards[item] for item in main_ids],
                additional_cards=[cards[item] for item in additional_ids],
            )
            english = build_english_edition(russian_revision=russian)
        except (
            Collection.DoesNotExist,
            Schedule.DoesNotExist,
            ValueError,
            ValidationError,
        ) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            self.style.SUCCESS(
                f"edition={edition.pk}, ru={russian.pk} ({len(russian.rendered_parts)} parts), "
                f"en={english.pk} ({len(english.rendered_parts)} parts)"
            )
        )
