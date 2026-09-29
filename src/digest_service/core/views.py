from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from digest_service.ai_gateway.models import AIBudgetPeriod
from digest_service.catalog.models import Collection, Source
from digest_service.delivery.models import Delivery
from digest_service.editorial.models import (
    EditionRevision,
    EditorialSelection,
    Event,
    EventCardRevision,
)
from digest_service.ingestion.models import IngestionRun, Publication
from digest_service.operations.models import (
    BackgroundJob,
    BackupRun,
    DeliverySafetyState,
    OutboxEntry,
    ProcessLease,
)
from digest_service.scheduling.models import Schedule, ScheduleSlot
from digest_service.subscriptions.models import ErasureRequest, SubscriberGeneration
from digest_service.telegram_bot.models import BotReply


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "scope": "web_and_database"})


@staff_member_required
def dashboard(request):
    # Staff membership alone does not grant access to the source catalogue.
    if not request.user.has_perms(
        ["catalog.view_collection", "catalog.view_source", "scheduling.view_schedule"]
    ):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    today = timezone.localdate()
    ai_budget = AIBudgetPeriod.objects.filter(period_start=today.replace(day=1)).first()
    return render(
        request,
        "dashboard.html",
        {
            "collection_count": Collection.objects.filter(release_target="v1").count(),
            "source_count": Source.objects.count(),
            "telegram_count": Source.objects.filter(kind="telegram").count(),
            "website_count": Source.objects.filter(kind="website").count(),
            "publication_count": Publication.objects.count(),
            "event_count": Event.objects.count(),
            "draft_card_count": EventCardRevision.objects.filter(
                editorial_state="draft", is_current=True
            ).count(),
            "draft_edition_count": EditionRevision.objects.filter(
                editorial_state="draft", is_current=True
            ).count(),
            "pending_selection_count": EditorialSelection.objects.filter(status="pending").count(),
            "ai_budget": ai_budget,
            "active_subscriber_count": SubscriberGeneration.objects.filter(
                is_current=True, subscription_status="active"
            ).count(),
            "pending_delivery_count": Delivery.objects.filter(
                status__in=["pending", "sending"]
            ).count(),
            "pending_bot_reply_count": BotReply.objects.filter(
                status__in=["pending", "retry_wait"]
            ).count(),
            "pending_job_count": BackgroundJob.objects.filter(
                status__in=["pending", "running", "retry_wait"]
            ).count(),
            "failed_job_count": BackgroundJob.objects.filter(status="failed").count(),
            "pending_outbox_count": OutboxEntry.objects.filter(
                status__in=["pending", "failed"]
            ).count(),
            "planned_slot_count": ScheduleSlot.objects.filter(
                state__in=["planned", "preparing"]
            ).count(),
            "active_processes": ProcessLease.objects.filter(
                lease_expires_at__gt=timezone.now()
            ).order_by("name"),
            "unknown_delivery_count": Delivery.objects.filter(status="unknown").count(),
            "overdue_erasure_count": ErasureRequest.objects.filter(
                status__in=["accepted", "processing"], deadline_at__lt=timezone.now()
            ).count(),
            "delivery_safety": DeliverySafetyState.objects.filter(key="global").first(),
            "latest_guard_export": BackupRun.objects.filter(
                kind="erasure_guard", status="succeeded"
            ).first(),
            "latest_runs": IngestionRun.objects.select_related("source__profile")[:5],
            "collections": Collection.objects.order_by("sort_order", "id"),
            "schedules": Schedule.objects.order_by("sort_order", "id"),
        },
    )
