from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from digest_service.delivery.models import Delivery, DeliveryAttempt, DeliveryPart

from .models import BotReply


@transaction.atomic
def recover_stale_telegram_claims(*, now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.TELEGRAM_IN_FLIGHT_TIMEOUT_SECONDS)
    replies = list(
        BotReply.objects.select_for_update().filter(
            status="in_flight", in_flight_at__lte=cutoff
        )
    )
    for reply in replies:
        reply.status = "unknown"
        reply.error_code = "sender_lost_after_claim"
        reply.in_flight_at = None
        reply.save()
    parts = list(
        DeliveryPart.objects.select_for_update()
        .select_related("delivery")
        .filter(status="in_flight", in_flight_at__lte=cutoff)
    )
    for part in parts:
        attempt_number = (part.attempts.aggregate(value=Max("attempt_number"))["value"] or 0) + 1
        DeliveryAttempt.objects.create(
            part=part,
            attempt_number=attempt_number,
            outcome="unknown",
            error_code="sender_lost_after_claim",
        )
        part.status = "unknown"
        part.in_flight_at = None
        part.save()
        Delivery.objects.filter(pk=part.delivery_id).update(status="unknown", updated_at=now)
    return {"replies_unknown": len(replies), "parts_unknown": len(parts)}
