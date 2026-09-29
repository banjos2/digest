from django.conf import settings
from django.core.exceptions import ValidationError

from digest_service.ai_gateway.openrouter_provider import OpenRouterProvider
from digest_service.catalog.models import Collection
from digest_service.ingestion.models import PublicationVersion

from .grouping import suggest_groupings
from .models import EditorialSelection, Event, EventCollectionAssignment, EventMembership
from .service import create_event_from_versions, draft_russian_card, translate_english_card
from .workflow import (
    accept_grouping_suggestion,
    approve_card_pair,
    approve_edition_pair,
    build_selected_editions,
    discover_editorial_candidates,
    publish_edition_pair,
)

SKIPPED_SINGLETON_TITLE_PARTS = (
    "главные новости",
    "главное за ночь",
    "итоги недели",
    "дайджест за",
)


def _latest_versions(slot):
    rows = (
        PublicationVersion.objects.filter(
            purged_at__isnull=True,
            publication__published_at__gte=slot.window_start,
            publication__published_at__lt=slot.window_end,
            publication__source__is_active=True,
            publication__source__sourcecollection__is_active=True,
            publication__source__sourcecollection__collection__is_active=True,
        )
        .select_related("publication__source")
        .order_by("publication_id", "-number")
        .distinct()
    )
    latest = {}
    for version in rows:
        latest.setdefault(version.publication_id, version)
    return list(latest.values())


def _in_slot(version, slot):
    published_at = version.publication.published_at
    return published_at and slot.window_start <= published_at < slot.window_end


def _approve_automatic_event(event):
    if event.status == "approved":
        return False
    event.status = "approved"
    if event.memberships.values(
        "publication_version__publication__source__origin_group_id"
    ).distinct().count() < 2:
        event.review_note = (
            "Автоматический выпуск: материал одного происхождения сохранён с явной атрибуцией."
        )
    event.full_clean()
    event.save()
    return True


def _create_events(slot, versions):
    suggest_groupings(since=slot.window_start, limit=1000, threshold=0.55)
    auto_grouped = 0
    from .models import GroupingSuggestion

    suggestions = GroupingSuggestion.objects.filter(
        status="pending", score__gte=settings.AUTOMATIC_GROUPING_THRESHOLD
    ).select_related("first_version__publication", "second_version__publication")
    for suggestion in suggestions:
        if not (
            _in_slot(suggestion.first_version, slot)
            and _in_slot(suggestion.second_version, slot)
        ):
            continue
        try:
            event = accept_grouping_suggestion(suggestion=suggestion)
        except ValidationError:
            continue
        _approve_automatic_event(event)
        auto_grouped += 1

    singleton_events = 0
    for version in versions:
        if EventMembership.objects.filter(
            publication_version=version,
            event__status__in=["proposed", "review", "approved", "held"],
        ).exists():
            continue
        folded_title = version.title.casefold()
        if any(part in folded_title for part in SKIPPED_SINGLETON_TITLE_PARTS):
            continue
        event = create_event_from_versions(
            working_title=version.title[:300], version_ids=[version.pk]
        )
        event.occurred_at = version.publication.published_at
        _approve_automatic_event(event)
        singleton_events += 1
    return auto_grouped, singleton_events


def _route_by_keywords(events):
    created = 0
    collections = [
        collection
        for collection in Collection.objects.filter(is_active=True, release_target="v1")
        if collection.routing_keywords
    ]
    for event in events:
        text = " ".join(
            event.memberships.values_list("publication_version__title", flat=True)
        ).casefold()
        for collection in collections:
            matched = [
                keyword
                for keyword in collection.routing_keywords
                if keyword.casefold() in text
            ]
            if not matched:
                continue
            _, was_created = EventCollectionAssignment.objects.get_or_create(
                event=event,
                collection=collection,
                defaults={
                    "assigned_by": "rule",
                    "rationale": f"Совпали ключевые слова: {', '.join(matched[:5])}.",
                },
            )
            created += int(was_created)
    return created


def _rank_selections(slot):
    selected = []
    for collection in Collection.objects.filter(is_active=True, release_target="v1"):
        EditorialSelection.objects.filter(
            slot=slot,
            collection=collection,
            status__in=["main", "additional"],
            rationale__startswith="Автоматический отбор",
        ).update(status="pending")
        existing = list(
            EditorialSelection.objects.filter(
                slot=slot, collection=collection, status__in=["main", "additional"]
            ).select_related("event")
        )
        selected.extend(existing)
        main_count = sum(row.status == "main" for row in existing)
        additional_count = sum(row.status == "additional" for row in existing)
        pending = list(
            EditorialSelection.objects.filter(
                slot=slot, collection=collection, status="pending"
            ).select_related("event")
        )
        pending.sort(
            key=lambda row: (
                row.event.memberships.values(
                    "publication_version__publication__source__origin_group_id"
                ).distinct().count(),
                row.event.occurred_at or slot.window_start,
            ),
            reverse=True,
        )
        for selection in pending:
            origin_count = selection.event.memberships.values(
                "publication_version__publication__source__origin_group_id"
            ).distinct().count()
            selection.impact = 4 if origin_count >= 2 else 3
            selection.relevance = 5
            selection.novelty = 4
            selection.timeliness = 5
            selection.corroboration = 5 if origin_count >= 2 else 2
            if main_count < settings.AUTOMATIC_MAIN_STORIES:
                selection.status = "main"
                main_count += 1
            elif additional_count < settings.AUTOMATIC_ADDITIONAL_STORIES:
                selection.status = "additional"
                additional_count += 1
            else:
                selection.status = "excluded"
            selection.rationale = (
                "Автоматический отбор по актуальности, тематической маршрутизации и числу "
                f"независимых происхождений ({origin_count})."
            )
            selection.full_clean()
            selection.save()
            if selection.status in ["main", "additional"]:
                selected.append(selection)
    return selected


def run_automatic_digest_cycle(*, slot, provider=None):
    versions = _latest_versions(slot)
    auto_grouped, singleton_events = _create_events(slot, versions)
    events = Event.objects.filter(
        memberships__publication_version__in=versions,
        status__in=["review", "approved"],
    ).distinct()
    approved = 0
    for event in events:
        approved += int(_approve_automatic_event(event))
    routes = _route_by_keywords(events)
    discovered = discover_editorial_candidates(slot=slot)
    selections = _rank_selections(slot)
    provider = provider or OpenRouterProvider()
    slot_key = f"{slot.schedule_revision.schedule_id}:{slot.scheduled_at.isoformat()}"
    cards = {}
    for selection in selections:
        if selection.event_id in cards:
            continue
        russian = draft_russian_card(
            event=selection.event, slot_key=slot_key, provider=provider
        )
        english = translate_english_card(russian_card=russian, provider=provider)
        approve_card_pair(revision=english)
        cards[selection.event_id] = russian.pk
    editions = 0
    for collection_id in {selection.collection_id for selection in selections}:
        russian, english = build_selected_editions(
            slot=slot, collection=Collection.objects.get(pk=collection_id)
        )
        approve_edition_pair(revision=english)
        publish_edition_pair(revision=russian)
        editions += 1
    return {
        "versions": len(versions),
        "grouped": auto_grouped,
        "singletons": singleton_events,
        "approved": approved,
        "routes": routes,
        "discovered": discovered,
        "selected": len(selections),
        "cards": len(cards),
        "editions": editions,
    }
