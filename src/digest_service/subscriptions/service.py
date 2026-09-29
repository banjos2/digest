import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from digest_service.catalog.models import Collection
from digest_service.scheduling.models import Schedule

from .models import (
    ErasureTombstone,
    PreferenceDraft,
    PreferenceRevision,
    SubscriberGeneration,
    SubscriptionCollection,
)
from .privacy import erasure_subject_hash


def _draft_expiry():
    return timezone.now() + timedelta(minutes=settings.PREFERENCE_DRAFT_TTL_MINUTES)


def _get_live_draft(subscriber):
    draft = PreferenceDraft.objects.select_for_update().filter(subscriber=subscriber).first()
    if not draft or draft.expires_at <= timezone.now():
        if draft:
            draft.delete()
        raise ValidationError("Черновик настроек устарел. Откройте настройки заново.")
    return draft


@transaction.atomic
def start_subscriber(*, telegram_user_id, telegram_chat_id):
    subscriber = (
        SubscriberGeneration.objects.select_for_update()
        .filter(telegram_user_id=telegram_user_id, is_current=True)
        .first()
    )
    created = subscriber is None
    if created:
        last_generation = (
            SubscriberGeneration.objects.filter(telegram_user_id=telegram_user_id).aggregate(
                value=Max("generation")
            )["value"]
            or 0
        )
        tombstone = ErasureTombstone.objects.filter(
            subject_hash=erasure_subject_hash(telegram_user_id), expires_at__gt=timezone.now()
        ).first()
        if tombstone:
            last_generation = max(last_generation, tombstone.last_generation)
        subscriber = SubscriberGeneration.objects.create(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            generation=last_generation + 1,
        )
        return subscriber, True
    subscriber.telegram_chat_id = telegram_chat_id
    if subscriber.privacy_state == "erasure_pending":
        subscriber.save()
        return subscriber, False
    if subscriber.setup_status == "complete" and subscriber.subscription_status == "paused":
        subscriber.subscription_status = "active"
        subscriber.dialog_state = "none"
        subscriber.automatic_delivery_after = timezone.now()
    elif subscriber.setup_status == "incomplete":
        subscriber.dialog_state = "choose_language"
    subscriber.save()
    return subscriber, False


@transaction.atomic
def choose_language(*, subscriber, language):
    if language not in {"ru", "en"}:
        raise ValidationError("Поддерживаются русский и английский языки.")
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    if subscriber.privacy_state != "normal" or subscriber.access_status != "allowed":
        raise ValidationError("Настройки этого профиля сейчас недоступны.")
    subscriber.interface_language = language
    subscriber.digest_language = language
    subscriber.dialog_state = "choose_collections"
    subscriber.save()
    PreferenceDraft.objects.update_or_create(
        subscriber=subscriber,
        defaults={
            "interface_language": language,
            "digest_language": language,
            "collection_ids": [],
            "schedule_id": "",
            "expires_at": _draft_expiry(),
            "token": uuid.uuid4(),
        },
    )
    return subscriber


@transaction.atomic
def begin_settings_edit(*, subscriber):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    if subscriber.setup_status != "complete" or subscriber.privacy_state != "normal":
        raise ValidationError("Сохранённые настройки пока недоступны.")
    current = subscriber.preference_revisions.filter(is_current=True).first()
    if not current:
        raise ValidationError("Текущая редакция настроек не найдена.")
    PreferenceDraft.objects.update_or_create(
        subscriber=subscriber,
        defaults={
            "interface_language": current.interface_language,
            "digest_language": current.digest_language,
            "collection_ids": list(
                current.selected_collections.values_list("collection_id", flat=True)
            ),
            "schedule_id": current.schedule_revision.schedule_id,
            "expires_at": _draft_expiry(),
            "token": uuid.uuid4(),
        },
    )
    subscriber.dialog_state = "choose_collections"
    subscriber.save()
    return subscriber.preference_draft


@transaction.atomic
def toggle_draft_collection(*, subscriber, collection_id):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    draft = _get_live_draft(subscriber)
    if (
        not Collection.objects.filter(pk=collection_id, is_active=True, release_target="v1")
        .exclude(kind="video_digest")
        .exists()
    ):
        raise ValidationError("Эта подборка недоступна.")
    selected = list(draft.collection_ids)
    if collection_id in selected:
        selected.remove(collection_id)
    else:
        selected.append(collection_id)
    draft.collection_ids = selected
    draft.expires_at = _draft_expiry()
    draft.save()
    return draft


@transaction.atomic
def set_draft_collection(*, subscriber, collection_id, selected):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    draft = _get_live_draft(subscriber)
    if type(selected) is not bool:
        raise ValidationError("Состояние подборки должно быть логическим значением.")
    if (
        not Collection.objects.filter(pk=collection_id, is_active=True, release_target="v1")
        .exclude(kind="video_digest")
        .exists()
    ):
        raise ValidationError("Эта подборка недоступна.")
    collection_ids = list(draft.collection_ids)
    if selected and collection_id not in collection_ids:
        collection_ids.append(collection_id)
    elif not selected and collection_id in collection_ids:
        collection_ids.remove(collection_id)
    draft.collection_ids = collection_ids
    draft.expires_at = _draft_expiry()
    draft.save()
    return draft


@transaction.atomic
def finish_collection_selection(*, subscriber):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    draft = _get_live_draft(subscriber)
    if not draft.collection_ids:
        raise ValidationError("Выберите хотя бы одну подборку.")
    subscriber.dialog_state = "choose_schedule"
    subscriber.save()
    draft.expires_at = _draft_expiry()
    draft.save()
    return draft


@transaction.atomic
def choose_draft_schedule(*, subscriber, schedule_id):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    draft = _get_live_draft(subscriber)
    schedule = Schedule.objects.filter(pk=schedule_id, visible_to_new_subscribers=True).first()
    if not schedule:
        raise ValidationError("Эта частота сейчас недоступна.")
    draft.schedule_id = schedule.pk
    draft.expires_at = _draft_expiry()
    draft.save()
    subscriber.dialog_state = "review_preferences"
    subscriber.save()
    return draft


@transaction.atomic
def save_preference_draft(*, subscriber):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    draft = _get_live_draft(subscriber)
    if not draft.schedule_id:
        raise ValidationError("Выберите частоту доставки.")
    preference = save_preferences(
        subscriber=subscriber,
        collection_ids=draft.collection_ids,
        schedule_id=draft.schedule_id,
        language=draft.digest_language,
    )
    draft.delete()
    return preference


@transaction.atomic
def cancel_preference_draft(*, subscriber):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    PreferenceDraft.objects.filter(subscriber=subscriber).delete()
    subscriber.dialog_state = "none" if subscriber.setup_status == "complete" else "choose_language"
    subscriber.save()
    return subscriber


@transaction.atomic
def save_preferences(
    *, subscriber, collection_ids, schedule_id, language=None, effective_from=None
):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    if not subscriber.is_current or subscriber.privacy_state != "normal":
        raise ValidationError("Настройки можно сохранить только для действующего профиля.")
    if subscriber.access_status != "allowed":
        raise ValidationError("Доступ к сервису заблокирован.")
    language = language or subscriber.digest_language
    if language not in {"ru", "en"}:
        raise ValidationError("Поддерживаются русский и английский языки.")
    collection_ids = list(collection_ids)
    if not collection_ids or len(collection_ids) != len(set(collection_ids)):
        raise ValidationError("Выберите хотя бы одну подборку без повторов.")
    collections = list(
        Collection.objects.filter(
            pk__in=collection_ids,
            is_active=True,
            release_target="v1",
        )
        .exclude(kind="video_digest")
        .order_by("sort_order", "id")
    )
    if {item.pk for item in collections} != set(collection_ids):
        raise ValidationError("Одна из подборок недоступна в текущей версии.")
    try:
        schedule = Schedule.objects.select_for_update().get(pk=schedule_id)
    except Schedule.DoesNotExist as error:
        raise ValidationError("Неизвестная частота доставки.") from error
    if subscriber.setup_status == "incomplete" and not schedule.visible_to_new_subscribers:
        raise ValidationError("Эта частота пока не доступна новым подписчикам.")
    effective_from = effective_from or timezone.now()
    if timezone.is_naive(effective_from):
        raise ValidationError("Дата начала действия должна содержать часовой пояс.")
    current = subscriber.preference_revisions.filter(is_current=True).first()
    next_revision = (current.revision if current else 0) + 1
    if current:
        current.is_current = False
        current.save()
    preference = PreferenceRevision.objects.create(
        subscriber=subscriber,
        revision=next_revision,
        interface_language=language,
        digest_language=language,
        schedule_revision=schedule.revisions.get(number=schedule.revision),
        effective_from=effective_from,
    )
    SubscriptionCollection.objects.bulk_create(
        [
            SubscriptionCollection(preference_revision=preference, collection=collection)
            for collection in collections
        ]
    )
    subscriber.interface_language = language
    subscriber.digest_language = language
    subscriber.setup_status = "complete"
    if subscriber.subscription_status != "paused":
        subscriber.subscription_status = "active"
    subscriber.dialog_state = "none"
    subscriber.automatic_delivery_after = effective_from
    subscriber.save()

    from digest_service.delivery.service import cancel_stale_automatic_deliveries

    cancel_stale_automatic_deliveries(subscriber=subscriber, current_preference=preference)
    return preference


@transaction.atomic
def pause_subscriber(*, subscriber):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    if subscriber.setup_status != "complete" or subscriber.privacy_state != "normal":
        raise ValidationError("Эту подписку нельзя поставить на паузу.")
    subscriber.subscription_status = "paused"
    subscriber.dialog_state = "none"
    subscriber.save()
    from digest_service.delivery.service import cancel_pending_automatic_deliveries

    cancel_pending_automatic_deliveries(subscriber=subscriber)
    return subscriber
