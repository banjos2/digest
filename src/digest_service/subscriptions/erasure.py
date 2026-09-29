from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from digest_service.delivery.models import (
    Delivery,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryPart,
)
from digest_service.delivery.service import cancel_all_pending_deliveries
from digest_service.telegram_bot.models import BotReply, TelegramUpdateReceipt

from .models import (
    ErasureRequest,
    ErasureTombstone,
    PreferenceDraft,
    PreferenceRevision,
    SubscriberGeneration,
    SubscriptionCollection,
)
from .privacy import erasure_subject_hash


def delete_subscriber_records(*, subscriber, delete_chat_records=True):
    original_chat_id = subscriber.telegram_chat_id
    delivery_ids = list(
        Delivery.objects.filter(subscriber_generation=subscriber).values_list("pk", flat=True)
    )
    attempts_deleted = DeliveryAttempt.objects.filter(part__delivery_id__in=delivery_ids).delete()[0]
    parts_deleted = DeliveryPart.objects.filter(delivery_id__in=delivery_ids).delete()[0]
    manifests_deleted = DeliveryManifest.objects.filter(delivery_id__in=delivery_ids).delete()[0]
    deliveries_deleted = Delivery.objects.filter(pk__in=delivery_ids).delete()[0]
    preference_ids = list(
        PreferenceRevision.objects.filter(subscriber=subscriber).values_list("pk", flat=True)
    )
    selections_deleted = SubscriptionCollection.objects.filter(
        preference_revision_id__in=preference_ids
    ).delete()[0]
    preferences_deleted = PreferenceRevision.objects.filter(pk__in=preference_ids).delete()[0]
    PreferenceDraft.objects.filter(subscriber=subscriber).delete()
    replies_deleted = 0
    receipts_deleted = 0
    if delete_chat_records:
        receipt_ids = list(
            BotReply.objects.filter(chat_id=original_chat_id).values_list("receipt_id", flat=True)
        )
        replies_deleted = BotReply.objects.filter(chat_id=original_chat_id).delete()[0]
        receipts_deleted = TelegramUpdateReceipt.objects.filter(pk__in=receipt_ids).delete()[0]
    subscriber.delete()
    return {
        "deliveries_deleted": deliveries_deleted,
        "delivery_parts_deleted": parts_deleted,
        "delivery_attempts_deleted": attempts_deleted,
        "delivery_manifests_deleted": manifests_deleted,
        "preferences_deleted": preferences_deleted,
        "selections_deleted": selections_deleted,
        "bot_replies_deleted": replies_deleted,
        "telegram_receipts_deleted": receipts_deleted,
    }


@transaction.atomic
def request_profile_erasure(*, subscriber, now=None):
    now = now or timezone.now()
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    if subscriber.privacy_state != "normal":
        raise ValidationError("Удаление этого профиля уже принято.")
    existing = ErasureRequest.objects.select_for_update().filter(
        subscriber=subscriber,
        status__in=["pending_confirmation", "accepted", "processing"],
    ).first()
    if existing and existing.status != "pending_confirmation":
        raise ValidationError("Удаление этого профиля уже выполняется.")
    if existing and existing.confirmation_expires_at > now:
        request = existing
    else:
        if existing:
            existing.status = "cancelled"
            existing.save()
        request = ErasureRequest.objects.create(
            subscriber=subscriber,
            confirmation_expires_at=now
            + timedelta(minutes=settings.ERASURE_CONFIRMATION_TTL_MINUTES),
        )
    subscriber.dialog_state = "confirm_data_erasure"
    subscriber.save()
    return request


@transaction.atomic
def cancel_erasure_confirmation(*, subscriber):
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    request = ErasureRequest.objects.select_for_update().filter(
        subscriber=subscriber, status="pending_confirmation"
    ).first()
    if not request:
        raise ValidationError("Нет ожидающего подтверждения удаления.")
    request.status = "cancelled"
    request.save()
    subscriber.dialog_state = "none" if subscriber.setup_status == "complete" else "choose_language"
    subscriber.save()
    return request


@transaction.atomic
def confirm_profile_erasure(*, subscriber, token, now=None):
    now = now or timezone.now()
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=subscriber.pk)
    request = ErasureRequest.objects.select_for_update().filter(
        subscriber=subscriber,
        confirmation_token=token,
        status="pending_confirmation",
    ).first()
    if not request or request.confirmation_expires_at <= now:
        raise ValidationError("Подтверждение удаления устарело. Создайте запрос заново.")
    subscriber.subscription_status = "inactive"
    subscriber.privacy_state = "erasure_pending"
    subscriber.dialog_state = "none"
    subscriber.save()
    PreferenceDraft.objects.filter(subscriber=subscriber).delete()
    cancel_all_pending_deliveries(subscriber=subscriber)
    request.status = "accepted"
    request.accepted_at = now
    request.deadline_at = now + timedelta(hours=24)
    request.save()

    from digest_service.operations.service import enqueue_job

    enqueue_job(
        kind="erase_subscriber",
        parameters={"request_id": str(request.pk)},
        idempotency_key=f"erase-subscriber:{request.pk}",
        next_attempt_at=now + timedelta(minutes=1),
        max_attempts=3,
    )
    return request


@transaction.atomic
def execute_profile_erasure(*, request_id, now=None):
    now = now or timezone.now()
    request = ErasureRequest.objects.select_for_update().select_related("subscriber").get(
        pk=request_id
    )
    if request.status == "completed":
        return request.result
    if request.status not in ["accepted", "processing"] or not request.subscriber_id:
        raise ValidationError("Запрос удаления не готов к выполнению.")
    request.status = "processing"
    request.save()
    subscriber = SubscriberGeneration.objects.select_for_update().get(pk=request.subscriber_id)
    original_user_id = subscriber.telegram_user_id
    from digest_service.operations.guard_service import mark_delivery_guard_blocked

    mark_delivery_guard_blocked(reason="erasure_guard_export_pending")
    subject_hash = erasure_subject_hash(original_user_id)
    ErasureTombstone.objects.update_or_create(
        subject_hash=subject_hash,
        defaults={
            "last_generation": subscriber.generation,
            "completed_at": now,
            "expires_at": now + timedelta(days=settings.ERASURE_GUARD_DAYS),
        },
    )
    request.subscriber = None
    result = delete_subscriber_records(subscriber=subscriber)
    request.status = "completed"
    request.completed_at = now
    request.result = result
    request.error_code = ""
    request.save()
    from digest_service.operations.service import enqueue_job

    enqueue_job(
        kind="export_erasure_guard",
        parameters={},
        idempotency_key=f"export-erasure-guard:{request.pk}",
        next_attempt_at=now,
        max_attempts=5,
    )
    return result


def confirmation_callback(request):
    return f"erase:{request.confirmation_token}:confirm"


@transaction.atomic
def purge_expired_privacy_records(*, now=None):
    now = now or timezone.now()
    expired_requests = list(
        ErasureRequest.objects.select_for_update().filter(
            status="pending_confirmation", confirmation_expires_at__lte=now
        )
    )
    for request in expired_requests:
        request.status = "cancelled"
        request.save()
        if request.subscriber_id:
            SubscriberGeneration.objects.filter(
                pk=request.subscriber_id, dialog_state="confirm_data_erasure"
            ).update(dialog_state="none", updated_at=now)
    guards_deleted = ErasureTombstone.objects.filter(expires_at__lte=now).delete()[0]
    return {"confirmations_cancelled": len(expired_requests), "guards_deleted": guards_deleted}
