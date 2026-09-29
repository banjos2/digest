import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Max, When

from digest_service.ai_gateway.service import run_provider_call
from digest_service.ingestion.models import PublicationVersion

from .models import Event, EventCardRevision, EventMembership
from .providers import CardDraftProvider, EventBrief, EvidenceItem


def create_event_from_versions(*, working_title, version_ids):
    version_ids = list(version_ids)
    requested_order = Case(
        *[When(pk=version_id, then=position) for position, version_id in enumerate(version_ids)],
        output_field=IntegerField(),
    )
    versions = list(
        PublicationVersion.objects.select_related(
            "publication__source__profile", "publication__source"
        )
        .filter(pk__in=version_ids)
        .order_by(requested_order)
    )
    if len(version_ids) != len(set(version_ids)) or len(versions) != len(version_ids):
        raise ValidationError("Все версии публикаций должны существовать и не повторяться.")
    with transaction.atomic():
        event = Event.objects.create(working_title=working_title)
        for index, version in enumerate(versions):
            EventMembership.objects.create(
                event=event,
                publication_version=version,
                role="primary" if index == 0 else "corroborates",
            )
    return event


def build_event_brief(event):
    memberships = event.memberships.select_related(
        "publication_version__publication__source__profile",
        "publication_version__publication__source",
    ).order_by("id")
    evidence = tuple(
        EvidenceItem(
            membership_id=item.pk,
            title=item.publication_version.title,
            body=item.publication_version.body,
            source_name=item.publication_version.publication.source.profile.name,
            source_url=item.publication_version.publication.canonical_url,
            language=item.publication_version.language,
            role=item.role,
        )
        for item in memberships
    )
    if not evidence:
        raise ValidationError("У события нет материалов.")
    if any(not item.body.strip() for item in evidence):
        raise ValidationError("Исходный текст одного из материалов уже очищен.")
    return EventBrief(str(event.pk), event.working_title, evidence)


def evidence_hash(event):
    values = list(
        event.memberships.order_by("id").values_list("id", "publication_version__content_hash")
    )
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _validate_russian_draft(event, draft):
    valid_ids = set(event.memberships.values_list("id", flat=True))
    citation_ids = list(draft.citation_membership_ids)
    if not citation_ids or not set(citation_ids).issubset(valid_ids):
        raise ValidationError("Провайдер вернул неизвестные или пустые ссылки на доказательства.")
    if len(citation_ids) > 6 or len(citation_ids) != len(set(citation_ids)):
        raise ValidationError("Провайдер вернул недопустимый набор ссылок.")
    _validate_card_text(draft.title, draft.summary)


def _validate_card_text(title, summary):
    if not title.strip() or len(title) > 300:
        raise ValidationError("Провайдер вернул недопустимый заголовок.")
    if not summary.strip() or len(summary) > 2000:
        raise ValidationError("Провайдер вернул недопустимый текст карточки.")


@transaction.atomic
def _save_card(*, event, slot_key, language, title, summary, citation_ids, source_ru_revision=None):
    evidence_ids = list(event.memberships.order_by("id").values_list("id", flat=True))
    current = list(
        EventCardRevision.objects.select_for_update().filter(
            event=event, slot_key=slot_key, language=language, is_current=True
        )
    )
    EventCardRevision.objects.filter(pk__in=[item.pk for item in current]).update(is_current=False)
    revision = (
        EventCardRevision.objects.filter(
            event=event, slot_key=slot_key, language=language
        ).aggregate(value=Max("revision"))["value"]
        or 0
    ) + 1
    return EventCardRevision.objects.create(
        event=event,
        slot_key=slot_key,
        language=language,
        revision=revision,
        title=title.strip(),
        summary=summary.strip(),
        evidence_membership_ids=evidence_ids,
        citation_membership_ids=list(citation_ids),
        evidence_hash=evidence_hash(event),
        source_ru_revision=source_ru_revision,
        change_note="AI/provider output; automatic publication policy applies",
    )


def draft_russian_card(*, event, slot_key, provider: CardDraftProvider, force=False):
    current_hash = evidence_hash(event)
    current = EventCardRevision.objects.filter(
        event=event, slot_key=slot_key, language="ru", is_current=True
    ).first()
    if current and current.evidence_hash == current_hash and not force:
        return current
    brief = build_event_brief(event)

    def invoke():
        result = provider.draft_russian(brief)
        _validate_russian_draft(event, result)
        return result

    draft = run_provider_call(
        operation="draft_ru",
        input_hash=current_hash,
        input_characters=sum(len(item.title) + len(item.body) for item in brief.evidence),
        provider=provider,
        callback=invoke,
    )
    card = _save_card(
        event=event,
        slot_key=slot_key,
        language="ru",
        title=draft.title,
        summary=draft.summary,
        citation_ids=draft.citation_membership_ids,
    )
    EventCardRevision.objects.filter(
        event=event, slot_key=slot_key, language="en", is_current=True
    ).update(is_current=False)
    return card


def translate_english_card(*, russian_card, provider: CardDraftProvider, force=False):
    if russian_card.language != "ru" or not russian_card.is_current:
        raise ValidationError("Перевод создаётся только из текущей русской редакции.")
    current = EventCardRevision.objects.filter(
        event=russian_card.event,
        slot_key=russian_card.slot_key,
        language="en",
        source_ru_revision=russian_card,
        is_current=True,
    ).first()
    if current and not force:
        return current

    def invoke():
        result = provider.translate_english(title=russian_card.title, summary=russian_card.summary)
        _validate_card_text(result.title, result.summary)
        return result

    draft = run_provider_call(
        operation="translate_en",
        input_hash=russian_card.content_hash,
        input_characters=len(russian_card.title) + len(russian_card.summary),
        provider=provider,
        callback=invoke,
    )
    return _save_card(
        event=russian_card.event,
        slot_key=russian_card.slot_key,
        language="en",
        title=draft.title,
        summary=draft.summary,
        citation_ids=russian_card.citation_membership_ids,
        source_ru_revision=russian_card,
    )
