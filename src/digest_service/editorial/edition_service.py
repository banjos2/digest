import html
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from .models import Edition, EditionItem, EditionRevision, EventCardRevision, EventMembership

TELEGRAM_PART_LIMIT = 3900
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


class _VisibleTextCounter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


def visible_utf16_units(value):
    parser = _VisibleTextCounter()
    parser.feed(value)
    return len("".join(parser.text).encode("utf-16-le")) // 2


def _period_label(edition, language):
    zone = ZoneInfo(edition.schedule_revision.rule["timezone"])
    start = edition.window_start.astimezone(zone)
    end = edition.window_end.astimezone(zone)
    if language == "ru":
        if start.date() == end.date():
            return f"{start:%d.%m %H:%M}–{end:%H:%M} МСК"
        return f"{start:%d.%m %H:%M}–{end:%d.%m %H:%M} МСК"
    if start.date() == end.date():
        return f"{start:%d %b %H:%M}–{end:%H:%M} MSK"
    return f"{start:%d %b %H:%M}–{end:%d %b %H:%M} MSK"


def _heading(edition, language):
    name = edition.collection.name_ru if language == "ru" else edition.collection.name_en
    label = "Дайджест" if language == "ru" else "News digest"
    return f"📰 <b>{label}</b> · {html.escape(name)} · {_period_label(edition, language)}"


def _citations(card):
    memberships = {
        item.pk: item
        for item in EventMembership.objects.select_related(
            "publication_version__publication__source__profile",
            "publication_version__publication",
        ).filter(pk__in=card.citation_membership_ids)
    }
    if len(memberships) != len(card.citation_membership_ids):
        raise ValidationError("Одна из ссылок карточки больше недоступна.")
    result = []
    for membership_id in card.citation_membership_ids:
        membership = memberships[membership_id]
        publication = membership.publication_version.publication
        result.append(
            (
                publication.source.profile.name,
                publication.canonical_url,
            )
        )
    return result


def _story_block(card, tier, main_number):
    citations = _citations(card)
    title = html.escape(card.title)
    summary = html.escape(card.summary)
    if tier == "additional":
        source_url = html.escape(citations[0][1], quote=True)
        return f'• <a href="{source_url}"><b>{title}</b></a>\n\n{summary}'
    number = NUMBER_EMOJI[main_number - 1] if main_number <= 10 else f"{main_number}."
    links = ", ".join(
        f'<a href="{html.escape(url, quote=True)}"><i>{html.escape(name)}</i></a>'
        for name, url in citations
    )
    return f"{number} <b>{title}</b>\n\n{summary}\n\n{links}"


def _section_heading(tier, language):
    if tier == "main":
        return "🏆 <b>Главные темы</b>" if language == "ru" else "🏆 <b>Top stories</b>"
    return (
        "✨ <b>Ещё несколько интересных сюжетов</b>"
        if language == "ru"
        else "✨ <b>Also worth a look</b>"
    )


def render_parts(edition, language, ordered_cards, part_limit=TELEGRAM_PART_LIMIT):
    heading = _heading(edition, language)
    prepared = []
    main_number = 0
    for tier, card in ordered_cards:
        if tier == "main":
            main_number += 1
        prepared.append((tier, _story_block(card, tier, main_number)))
    parts = []
    current = ""
    current_tier = None
    for tier, story in prepared:
        section = _section_heading(tier, language) if tier != current_tier else ""
        prefix = heading if not current else ""
        additions = [value for value in [prefix, section, story] if value]
        candidate = "\n\n".join([value for value in [current, *additions] if value])
        if visible_utf16_units(candidate) <= part_limit:
            current = candidate
            current_tier = tier
            continue
        if current:
            parts.append(current)
        current = "\n\n".join([heading, _section_heading(tier, language), story])
        current_tier = tier
        if visible_utf16_units(current) > part_limit:
            raise ValidationError("Одна история не помещается в сообщение Telegram.")
    if current:
        parts.append(current)
    return parts


def _snapshot(ordered_cards):
    return [
        {
            "event_id": str(card.event_id),
            "card_revision_id": card.pk,
            "tier": tier,
            "position": position,
        }
        for position, (tier, card) in enumerate(ordered_cards, start=1)
    ]


@transaction.atomic
def _create_revision(*, edition, language, ordered_cards, source_ru_revision=None):
    event_ids = [card.event_id for _, card in ordered_cards]
    if not ordered_cards or len(event_ids) != len(set(event_ids)):
        raise ValidationError("В выпуске события не должны повторяться.")
    if any(card.language != language or not card.is_current for _, card in ordered_cards):
        raise ValidationError("В выпуск можно добавить только текущие карточки нужного языка.")
    EditionRevision.objects.select_for_update().filter(
        edition=edition, language=language, is_current=True
    ).update(is_current=False)
    revision = (
        EditionRevision.objects.filter(edition=edition, language=language).aggregate(
            value=Max("revision")
        )["value"]
        or 0
    ) + 1
    item_snapshot = _snapshot(ordered_cards)
    result = EditionRevision.objects.create(
        edition=edition,
        language=language,
        revision=revision,
        source_ru_revision=source_ru_revision,
        item_snapshot=item_snapshot,
        rendered_parts=render_parts(edition, language, ordered_cards),
        change_note="Generated edition; automatic publication policy applies",
    )
    for item, (_, card) in zip(item_snapshot, ordered_cards):
        EditionItem.objects.create(
            edition_revision=result,
            card_revision=card,
            tier=item["tier"],
            position=item["position"],
        )
    return result


def get_or_create_edition(*, collection, schedule_revision, scheduled_at, window_start, window_end):
    if not all(
        isinstance(value, datetime) and value.tzinfo
        for value in [scheduled_at, window_start, window_end]
    ):
        raise ValidationError("Времена выпуска должны содержать часовой пояс.")
    edition, created = Edition.objects.get_or_create(
        collection=collection,
        schedule_revision=schedule_revision,
        scheduled_at=scheduled_at,
        defaults={"window_start": window_start, "window_end": window_end},
    )
    if not created and (edition.window_start != window_start or edition.window_end != window_end):
        raise ValidationError("Для слота уже сохранено другое временное окно.")
    return edition


def build_russian_edition(*, edition, main_cards, additional_cards):
    ordered = [("main", card) for card in main_cards] + [
        ("additional", card) for card in additional_cards
    ]
    return _create_revision(edition=edition, language="ru", ordered_cards=ordered)


def build_english_edition(*, russian_revision):
    if russian_revision.language != "ru" or not russian_revision.is_current:
        raise ValidationError("EN выпуск строится из текущей RU-редакции.")
    ordered = []
    for item in russian_revision.item_snapshot:
        english = EventCardRevision.objects.filter(
            event_id=item["event_id"],
            slot_key=russian_revision.items.get(position=item["position"]).card_revision.slot_key,
            language="en",
            source_ru_revision_id=item["card_revision_id"],
            is_current=True,
        ).first()
        if not english:
            raise ValidationError("Для одной из RU-карточек нет актуального EN-перевода.")
        ordered.append((item["tier"], english))
    return _create_revision(
        edition=russian_revision.edition,
        language="en",
        ordered_cards=ordered,
        source_ru_revision=russian_revision,
    )
