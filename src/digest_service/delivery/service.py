import hashlib
import html
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from digest_service.editorial.edition_service import render_parts, visible_utf16_units
from digest_service.editorial.models import EditionRevision
from digest_service.subscriptions.models import SubscriberGeneration

from .models import Delivery, DeliveryAttempt, DeliveryManifest, DeliveryPart

DELIVERY_CONTENT_LIMIT = 3750


def _automatic_eligibility(subscriber, preference, scheduled_at):
    if not subscriber.is_current or subscriber.setup_status != "complete":
        raise ValidationError("Профиль подписчика не готов к доставке.")
    if subscriber.subscription_status != "active":
        raise ValidationError("Автоматическая подписка не активна.")
    if subscriber.access_status != "allowed" or subscriber.privacy_state != "normal":
        raise ValidationError("Доставка этому профилю запрещена.")
    if subscriber.chat_reachability == "unreachable":
        raise ValidationError("Чат подписчика недоступен.")
    if not preference.is_current:
        raise ValidationError("Для доставки нужна текущая редакция настроек.")
    if not preference.schedule_revision.schedule.delivery_enabled:
        raise ValidationError("Рассылка по этому расписанию отключена.")
    if subscriber.automatic_delivery_after and scheduled_at <= subscriber.automatic_delivery_after:
        raise ValidationError("Этот слот предшествует сохранению настроек.")


def _part_marker(language, number, total):
    if total == 1:
        return ""
    if language == "ru":
        return f"📨 <b>Часть {number} из {total}</b>\n\n"
    return f"📨 <b>Part {number} of {total}</b>\n\n"


def _empty_collection_block(revision):
    edition = revision.edition
    name = edition.collection.name_ru if revision.language == "ru" else edition.collection.name_en
    if revision.language == "ru":
        note = "Все сюжеты этой подборки уже включены в другие разделы выпуска."
    else:
        note = "Every story in this section already appears elsewhere in the digest."
    return f"📰 <b>{html.escape(name)}</b>\n\n{note}"


def _missing_block(language, names):
    if language == "ru":
        return "⚠️ <b>Пока не готовы:</b> " + ", ".join(html.escape(name) for name in names)
    return "⚠️ <b>Not ready yet:</b> " + ", ".join(html.escape(name) for name in names)


def _delivery_key(subscriber, preference, scheduled_at, kind, request_key):
    if kind != "automatic" and not request_key:
        raise ValidationError("Для ручной отправки нужен идемпотентный ключ запроса.")
    raw = (
        f"{subscriber.pk}:{preference.pk}:{scheduled_at.isoformat()}:{kind}:{request_key or 'slot'}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def prepare_latest_manual_delivery(*, subscriber, request_key, now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise ValidationError("Текущий момент должен содержать часовой пояс.")
    preference = subscriber.preference_revisions.filter(is_current=True).first()
    if not preference:
        raise ValidationError("Сначала завершите настройку подписки.")
    collection_ids = list(preference.selected_collections.values_list("collection_id", flat=True))
    scheduled_at = (
        EditionRevision.objects.filter(
            edition__collection_id__in=collection_ids,
            edition__schedule_revision=preference.schedule_revision,
            edition__scheduled_at__lte=now,
            language=preference.digest_language,
            editorial_state="published",
            is_current=True,
        )
        .order_by("-edition__scheduled_at")
        .values_list("edition__scheduled_at", flat=True)
        .first()
    )
    if not scheduled_at:
        raise ValidationError("Для ваших настроек пока нет опубликованного выпуска.")
    return prepare_delivery(
        subscriber=subscriber,
        scheduled_at=scheduled_at,
        kind="manual",
        request_key=request_key,
    )


@transaction.atomic
def prepare_delivery(*, subscriber, scheduled_at, kind="automatic", request_key=""):
    if timezone.is_naive(scheduled_at):
        raise ValidationError("Дата выпуска должна содержать часовой пояс.")
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    preference = subscriber.preference_revisions.filter(is_current=True).first()
    if not preference:
        raise ValidationError("У подписчика нет сохранённых настроек.")
    if kind == "automatic":
        _automatic_eligibility(subscriber, preference, scheduled_at)
    elif subscriber.privacy_state != "normal" or subscriber.access_status != "allowed":
        raise ValidationError("Запрос выпуска этому профилю запрещён.")
    idempotency_key = _delivery_key(subscriber, preference, scheduled_at, kind, request_key)
    existing = Delivery.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing

    all_selections = list(
        preference.selected_collections.select_related("collection").order_by(
            "collection__sort_order", "collection_id"
        )
    )
    selections = [
        item
        for item in all_selections
        if item.collection.is_active
        and item.collection.release_target == "v1"
        and item.collection.kind != "video_digest"
    ]
    if not selections:
        raise ValidationError("Нет активных выбранных подборок.")
    collection_ids = [selection.collection_id for selection in selections]
    revisions = list(
        EditionRevision.objects.select_related("edition__collection")
        .filter(
            edition__collection_id__in=collection_ids,
            edition__schedule_revision=preference.schedule_revision,
            edition__scheduled_at=scheduled_at,
            language=preference.digest_language,
            editorial_state="published",
            is_current=True,
        )
        .order_by("edition__collection__sort_order", "edition__collection_id")
    )
    revision_by_collection = {item.edition.collection_id: item for item in revisions}
    inactive_ids = [item.collection_id for item in all_selections if item not in selections]
    missing_ids = inactive_ids + [
        item for item in collection_ids if item not in revision_by_collection
    ]
    if not revisions:
        raise ValidationError("Для выбранного периода нет готовых опубликованных выпусков.")

    occurrences = []
    for collection_order, selection in enumerate(selections):
        revision = revision_by_collection.get(selection.collection_id)
        if not revision:
            continue
        items = list(revision.items.select_related("card_revision").order_by("position"))
        for item in items:
            occurrences.append(
                {
                    "collection_order": collection_order,
                    "collection_id": selection.collection_id,
                    "revision": revision,
                    "item": item,
                    "event_id": str(item.card_revision.event_id),
                    "priority": (
                        0 if item.tier == "main" else 1,
                        collection_order,
                        item.position,
                    ),
                }
            )
    winners = {}
    for occurrence in occurrences:
        current = winners.get(occurrence["event_id"])
        if current is None or occurrence["priority"] < current["priority"]:
            winners[occurrence["event_id"]] = occurrence

    kept = []
    omitted = []
    raw_parts = []
    for selection in selections:
        revision = revision_by_collection.get(selection.collection_id)
        if not revision:
            continue
        selected_occurrences = [
            item
            for item in occurrences
            if item["revision"].pk == revision.pk and winners[item["event_id"]] is item
        ]
        ordered_cards = [
            (item["item"].tier, item["item"].card_revision) for item in selected_occurrences
        ]
        kept.extend(
            {
                "event_id": item["event_id"],
                "collection_id": item["collection_id"],
                "edition_revision_id": revision.pk,
                "card_revision_id": item["item"].card_revision_id,
                "tier": item["item"].tier,
                "position": item["item"].position,
            }
            for item in selected_occurrences
        )
        for item in occurrences:
            if item["revision"].pk != revision.pk or winners[item["event_id"]] is item:
                continue
            winner = winners[item["event_id"]]
            omitted.append(
                {
                    "event_id": item["event_id"],
                    "collection_id": item["collection_id"],
                    "kept_in_collection_id": winner["collection_id"],
                }
            )
        if ordered_cards:
            raw_parts.extend(
                render_parts(
                    revision.edition,
                    revision.language,
                    ordered_cards,
                    part_limit=DELIVERY_CONTENT_LIMIT,
                )
            )
        else:
            raw_parts.append(_empty_collection_block(revision))
    if missing_ids:
        names = [
            (
                selection.collection.name_ru
                if preference.digest_language == "ru"
                else selection.collection.name_en
            )
            for selection in all_selections
            if selection.collection_id in missing_ids
        ]
        raw_parts.append(_missing_block(preference.digest_language, names))

    bodies = [
        _part_marker(preference.digest_language, number, len(raw_parts)) + body
        for number, body in enumerate(raw_parts, start=1)
    ]
    if any(visible_utf16_units(body) > 3900 for body in bodies):
        raise ValidationError("Маркер части превысил резерв Telegram-сообщения.")
    delivery = Delivery.objects.create(
        subscriber_generation=subscriber,
        preference_revision=preference,
        schedule_revision=preference.schedule_revision,
        scheduled_at=scheduled_at,
        kind=kind,
        language=preference.digest_language,
        idempotency_key=idempotency_key,
    )
    DeliveryManifest.objects.create(
        delivery=delivery,
        edition_revision_ids=[item.pk for item in revisions],
        kept_occurrences=kept,
        omitted_occurrences=omitted,
        missing_collection_ids=missing_ids,
    )
    for number, body in enumerate(bodies, start=1):
        DeliveryPart.objects.create(delivery=delivery, part_number=number, body=body)
    return delivery


@transaction.atomic
def cancel_pending_automatic_deliveries(*, subscriber):
    deliveries = Delivery.objects.select_for_update().filter(
        subscriber_generation=subscriber,
        kind="automatic",
        status__in=["pending", "sending"],
    )
    DeliveryPart.objects.filter(
        delivery__in=deliveries, status__in=["pending", "retry_wait"]
    ).update(status="cancelled")
    deliveries.exclude(parts__status__in=["in_flight", "unknown"]).update(status="cancelled")


@transaction.atomic
def cancel_all_pending_deliveries(*, subscriber):
    deliveries = Delivery.objects.select_for_update().filter(
        subscriber_generation=subscriber,
        status__in=["pending", "sending"],
    )
    DeliveryPart.objects.filter(
        delivery__in=deliveries, status__in=["pending", "retry_wait"]
    ).update(status="cancelled")
    deliveries.exclude(parts__status__in=["in_flight", "unknown"]).update(status="cancelled")


def cancel_stale_automatic_deliveries(*, subscriber, current_preference):
    stale = Delivery.objects.filter(
        subscriber_generation=subscriber,
        kind="automatic",
        status__in=["pending", "sending"],
    ).exclude(preference_revision=current_preference)
    DeliveryPart.objects.filter(delivery__in=stale, status__in=["pending", "retry_wait"]).update(
        status="cancelled"
    )
    stale.exclude(parts__status__in=["in_flight", "unknown"]).update(status="cancelled")


@transaction.atomic
def claim_next_part(*, delivery, now=None):
    now = now or timezone.now()
    delivery = Delivery.objects.select_for_update().get(pk=delivery.pk)
    if delivery.status in {"sent", "failed", "cancelled", "unknown"}:
        return None
    if delivery.parts.filter(status__in=["in_flight", "unknown"]).exists():
        return None
    part = (
        delivery.parts.select_related("delivery__subscriber_generation")
        .filter(status__in=["pending", "retry_wait"], next_attempt_at__lte=now)
        .order_by("part_number")
        .first()
    )
    if not part:
        return None
    part.status = "in_flight"
    part.in_flight_at = now
    part.save()
    delivery.status = "sending"
    delivery.save()
    return part


@transaction.atomic
def record_part_result(
    *,
    part,
    outcome,
    telegram_message_id="",
    error_code="",
    provider_request_id="",
    retry_after_seconds=0,
):
    part = DeliveryPart.objects.select_for_update().select_related("delivery").get(pk=part.pk)
    if part.status != "in_flight":
        raise ValidationError("Результат можно записать только для переданной части.")
    next_attempt = (part.attempts.aggregate(value=Max("attempt_number"))["value"] or 0) + 1
    attempt = DeliveryAttempt.objects.create(
        part=part,
        attempt_number=next_attempt,
        outcome=outcome,
        telegram_message_id=telegram_message_id,
        error_code=error_code,
        provider_request_id=provider_request_id,
    )
    status_by_outcome = {
        "sent": "sent",
        "temporary_failure": "retry_wait",
        "permanent_failure": "failed",
        "unknown": "unknown",
    }
    if outcome not in status_by_outcome:
        raise ValidationError("Неизвестный результат попытки.")
    part.status = status_by_outcome[outcome]
    part.in_flight_at = None
    part.telegram_message_id = telegram_message_id
    if outcome == "temporary_failure":
        part.next_attempt_at = timezone.now() + timedelta(seconds=max(1, retry_after_seconds))
    part.save()
    delivery = part.delivery
    if outcome == "sent":
        remaining = delivery.parts.exclude(status__in=["sent", "cancelled"])
        if not remaining.exists():
            delivery.status = (
                "cancelled" if delivery.parts.filter(status="cancelled").exists() else "sent"
            )
        else:
            delivery.status = "sending"
    elif outcome == "permanent_failure":
        delivery.status = "failed"
    elif outcome == "unknown":
        delivery.status = "unknown"
    elif outcome == "temporary_failure":
        delivery.status = "sending"
    delivery.save()
    return attempt
