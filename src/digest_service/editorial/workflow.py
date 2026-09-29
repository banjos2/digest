from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from digest_service.catalog.models import Collection
from digest_service.scheduling.models import ScheduleSlot

from .edition_service import build_english_edition, build_russian_edition, get_or_create_edition
from .models import (
    EditionRevision,
    EditorialSelection,
    Event,
    EventCardRevision,
    EventMembership,
    GroupingSuggestion,
)


@transaction.atomic
def accept_grouping_suggestion(*, suggestion):
    suggestion = GroupingSuggestion.objects.select_for_update().select_related(
        "first_version", "second_version"
    ).get(pk=suggestion.pk)
    if suggestion.status == "rejected":
        raise ValidationError("Отклонённое предложение нельзя принять.")
    memberships = list(
        EventMembership.objects.select_related("event")
        .filter(
            publication_version_id__in=[
                suggestion.first_version_id,
                suggestion.second_version_id,
            ],
            event__status__in=["proposed", "review", "approved", "held"],
        )
        .order_by("event_id")
    )
    events = {item.event_id: item.event for item in memberships}
    if len(events) > 1:
        raise ValidationError("Материалы уже относятся к разным событиям; требуется ручное слияние.")
    if events:
        event = next(iter(events.values()))
    else:
        event = Event.objects.create(
            working_title=suggestion.first_version.title[:300], status="review"
        )
    existing_ids = set(event.memberships.values_list("publication_version_id", flat=True))
    for position, version in enumerate(
        [suggestion.first_version, suggestion.second_version]
    ):
        if version.pk not in existing_ids:
            EventMembership.objects.create(
                event=event,
                publication_version=version,
                role="primary" if not existing_ids and position == 0 else "corroborates",
            )
            existing_ids.add(version.pk)
    if event.status == "proposed":
        event.status = "review"
        event.save()
    suggestion.status = "accepted"
    suggestion.save()
    return event


def discover_editorial_candidates(*, slot):
    slot = ScheduleSlot.objects.select_related("schedule_revision").get(pk=slot.pk)
    created = 0
    collections = Collection.objects.filter(
        is_active=True, release_target="v1"
    ).exclude(kind="video_digest")
    for collection in collections:
        routed_to_collection = Q(
            memberships__publication_version__publication__source__sourcecollection__collection=collection,
            memberships__publication_version__publication__source__sourcecollection__is_active=True,
        ) | Q(
            collection_assignments__collection=collection,
            collection_assignments__is_active=True,
        )
        events = (
            Event.objects.filter(
                status__in=["review", "approved"],
                memberships__publication_version__publication__published_at__gte=slot.window_start,
                memberships__publication_version__publication__published_at__lt=slot.window_end,
                memberships__publication_version__publication__source__is_active=True,
            )
            .filter(routed_to_collection)
            .distinct()
            .order_by("id")
        )
        for event in events:
            _, was_created = EditorialSelection.objects.get_or_create(
                slot=slot, collection=collection, event=event
            )
            created += int(was_created)
    return created


def _slot_key(slot):
    return f"{slot.schedule_revision.schedule_id}:{slot.scheduled_at.isoformat()}"


def _same_shape(revision, ordered_cards):
    expected = [
        (str(card.event_id), tier, card.pk)
        for tier, card in ordered_cards
    ]
    actual = [
        (item["event_id"], item["tier"], item["card_revision_id"])
        for item in revision.item_snapshot
    ]
    return actual == expected


@transaction.atomic
def build_selected_editions(*, slot, collection):
    slot = ScheduleSlot.objects.select_related("schedule_revision").get(pk=slot.pk)
    selections = list(
        EditorialSelection.objects.filter(
            slot=slot, collection=collection, status__in=["main", "additional"]
        ).order_by("status", "-weighted_score", "event_id")
    )
    main = [item for item in selections if item.status == "main"]
    additional = [item for item in selections if item.status == "additional"]
    if not selections:
        raise ValidationError("Для подборки не выбрано ни одного сюжета.")
    if len(main) > 10 or len(additional) > 5:
        raise ValidationError("Превышен лимит: 10 главных и 5 дополнительных сюжетов.")
    slot_key = _slot_key(slot)
    ru_cards = {
        item.event_id: item
        for item in EventCardRevision.objects.filter(
            event_id__in=[row.event_id for row in selections],
            slot_key=slot_key,
            language="ru",
            editorial_state="approved",
            is_current=True,
        )
    }
    en_cards = {
        item.event_id: item
        for item in EventCardRevision.objects.filter(
            event_id__in=[row.event_id for row in selections],
            slot_key=slot_key,
            language="en",
            editorial_state="approved",
            is_current=True,
        ).select_related("source_ru_revision")
    }
    if len(ru_cards) != len(selections) or len(en_cards) != len(selections):
        raise ValidationError("Не все выбранные сюжеты имеют утверждённые RU/EN-карточки.")
    if any(en_cards[key].source_ru_revision_id != ru_cards[key].pk for key in ru_cards):
        raise ValidationError("Один из EN-переводов относится к другой RU-редакции.")
    ordered_ru = [("main", ru_cards[row.event_id]) for row in main] + [
        ("additional", ru_cards[row.event_id]) for row in additional
    ]
    edition = get_or_create_edition(
        collection=collection,
        schedule_revision=slot.schedule_revision,
        scheduled_at=slot.scheduled_at,
        window_start=slot.window_start,
        window_end=slot.window_end,
    )
    russian = EditionRevision.objects.filter(
        edition=edition, language="ru", is_current=True
    ).first()
    if not russian or not _same_shape(russian, ordered_ru):
        russian = build_russian_edition(
            edition=edition,
            main_cards=[card for tier, card in ordered_ru if tier == "main"],
            additional_cards=[card for tier, card in ordered_ru if tier == "additional"],
        )
    english = EditionRevision.objects.filter(
        edition=edition,
        language="en",
        source_ru_revision=russian,
        is_current=True,
    ).first()
    if not english:
        english = build_english_edition(russian_revision=russian)
    return russian, english


@transaction.atomic
def approve_card_pair(*, revision):
    revision = EventCardRevision.objects.select_for_update().get(pk=revision.pk)
    russian = revision if revision.language == "ru" else revision.source_ru_revision
    if not russian or not russian.is_current:
        raise ValidationError("RU-карточка больше не является текущей.")
    english = EventCardRevision.objects.select_for_update().filter(
        source_ru_revision=russian, language="en", is_current=True
    ).first()
    if not english:
        raise ValidationError("Для RU-карточки ещё нет текущего EN-перевода.")
    EventCardRevision.objects.filter(pk__in=[russian.pk, english.pk]).update(
        editorial_state="approved"
    )
    return russian, english


@transaction.atomic
def approve_edition_pair(*, revision):
    revision = EditionRevision.objects.select_for_update().get(pk=revision.pk)
    russian = revision if revision.language == "ru" else revision.source_ru_revision
    if not russian or not russian.is_current:
        raise ValidationError("RU-выпуск больше не является текущим.")
    english = EditionRevision.objects.select_for_update().filter(
        source_ru_revision=russian, language="en", is_current=True
    ).first()
    if not english:
        raise ValidationError("Для RU-выпуска ещё нет текущей EN-версии.")
    EditionRevision.objects.filter(pk__in=[russian.pk, english.pk]).update(
        editorial_state="approved"
    )
    return russian, english


@transaction.atomic
def publish_edition_pair(*, revision):
    revision = EditionRevision.objects.select_for_update().get(pk=revision.pk)
    russian = revision if revision.language == "ru" else revision.source_ru_revision
    if not russian or not russian.is_current:
        raise ValidationError("RU-выпуск больше не является текущим.")
    english = EditionRevision.objects.select_for_update().filter(
        source_ru_revision=russian, language="en", is_current=True
    ).first()
    if not english or {russian.editorial_state, english.editorial_state} != {"approved"}:
        raise ValidationError("Перед публикацией нужно утвердить текущую RU/EN-пару.")
    EditionRevision.objects.filter(pk__in=[russian.pk, english.pk]).update(
        editorial_state="published"
    )
    return russian, english
