from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from digest_service.subscriptions.models import SubscriberGeneration

from .models import BotReply


@transaction.atomic
def claim_next_reply(*, now=None):
    now = now or timezone.now()
    reply = (
        BotReply.objects.select_for_update()
        .filter(status__in=["pending", "retry_wait"], next_attempt_at__lte=now)
        .order_by("created_at")
        .first()
    )
    if not reply:
        return None
    reply.status = "in_flight"
    reply.in_flight_at = now
    reply.attempt_count += 1
    reply.save()
    return reply


@transaction.atomic
def record_reply_result(
    *, reply, outcome, telegram_message_id="", error_code="", retry_after_seconds=0
):
    reply = BotReply.objects.select_for_update().get(pk=reply.pk)
    if reply.status != "in_flight":
        raise ValidationError("Результат можно записать только для переданного ответа.")
    status_by_outcome = {
        "sent": "sent",
        "temporary_failure": "retry_wait",
        "permanent_failure": "failed",
        "unknown": "unknown",
    }
    if outcome not in status_by_outcome:
        raise ValidationError("Неизвестный результат ответа Telegram.")
    reply.status = status_by_outcome[outcome]
    reply.in_flight_at = None
    reply.telegram_message_id = telegram_message_id
    reply.error_code = error_code
    if outcome == "temporary_failure":
        reply.next_attempt_at = timezone.now() + timedelta(seconds=max(1, retry_after_seconds))
    reply.save()
    if outcome == "permanent_failure" and error_code == "forbidden":
        SubscriberGeneration.objects.filter(telegram_chat_id=reply.chat_id, is_current=True).update(
            chat_reachability="unreachable"
        )
    elif outcome == "sent":
        SubscriberGeneration.objects.filter(telegram_chat_id=reply.chat_id, is_current=True).update(
            chat_reachability="reachable"
        )
    return reply
