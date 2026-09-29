import socket
from datetime import timedelta

from celery import current_app
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from digest_service.ai_gateway.openrouter_provider import OpenRouterProvider
from digest_service.catalog.models import Collection, Source
from digest_service.delivery.service import prepare_delivery
from digest_service.editorial.automatic import run_automatic_digest_cycle
from digest_service.editorial.grouping import suggest_groupings
from digest_service.editorial.models import Event
from digest_service.editorial.service import draft_russian_card, translate_english_card
from digest_service.editorial.workflow import build_selected_editions, discover_editorial_candidates
from digest_service.ingestion.models import PublicationVersion
from digest_service.scheduling.models import Schedule, ScheduleSlot
from digest_service.subscriptions.models import PreferenceRevision

from .models import BackgroundJob, OutboxEntry, ProcessLease

PARAMETER_KEYS = {
    "collect_source": {"source_id", "limit"},
    "suggest_groupings": {"hours"},
    "discover_editorial_candidates": {"slot_id"},
    "automatic_digest_cycle": {"slot_id"},
    "generate_event_cards": {"slot_id", "event_id"},
    "build_editorial_editions": {"slot_id", "collection_id"},
    "erase_subscriber": {"request_id"},
    "purge_privacy_records": set(),
    "create_database_backup": set(),
    "purge_backup_artifacts": set(),
    "export_erasure_guard": set(),
    "prepare_schedule_slot": {"slot_id"},
    "purge_source_text": {"batch_size"},
}


def _error_code(error):
    value = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return value[:100]


@transaction.atomic
def acquire_process_lease(*, name, owner, now=None):
    now = now or timezone.now()
    expires = now + timedelta(seconds=settings.SERVICE_PROCESS_LEASE_SECONDS)
    lease, created = ProcessLease.objects.select_for_update().get_or_create(
        name=name,
        defaults={"owner": owner, "heartbeat_at": now, "lease_expires_at": expires},
    )
    if created:
        return True
    if lease.owner != owner and lease.lease_expires_at > now:
        return False
    lease.owner = owner
    lease.heartbeat_at = now
    lease.lease_expires_at = expires
    lease.save()
    return True


@transaction.atomic
def release_process_lease(*, name, owner):
    return ProcessLease.objects.filter(pk=name, owner=owner).delete()[0] > 0


@transaction.atomic
def enqueue_job(*, kind, parameters, idempotency_key, next_attempt_at=None, max_attempts=5):
    """Persist a job and its broker intent in the same database transaction."""
    allowed = PARAMETER_KEYS.get(kind)
    if allowed is None:
        raise ValidationError("Неизвестный тип фонового задания.")
    if not isinstance(parameters, dict) or set(parameters) - allowed:
        raise ValidationError("Параметры задания не соответствуют его контракту.")
    job, created = BackgroundJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "kind": kind,
            "parameters": parameters,
            "next_attempt_at": next_attempt_at or timezone.now(),
            "max_attempts": max_attempts,
        },
    )
    if created:
        OutboxEntry.objects.create(job=job, next_attempt_at=job.next_attempt_at)
    elif job.kind != kind or job.parameters != parameters:
        raise ValidationError("Идемпотентный ключ уже относится к другому заданию.")
    return job, created


def broker_message(job):
    return {"job_id": str(job.pk), "contract_version": job.contract_version}


def publish_outbox(*, limit=100, now=None, producer=None):
    now = now or timezone.now()
    producer = producer or (
        lambda job: current_app.send_task(
            "digest.execute_job",
            kwargs=broker_message(job),
            task_id=str(job.pk),
            queue="maintenance",
        )
    )
    ids = list(
        OutboxEntry.objects.filter(status__in=["pending", "failed"], next_attempt_at__lte=now)
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )
    published = 0
    for pk in ids:
        with transaction.atomic():
            entry = OutboxEntry.objects.select_for_update().select_related("job").get(pk=pk)
            if entry.status not in ["pending", "failed"] or entry.next_attempt_at > now:
                continue
            entry.attempts += 1
            try:
                message = producer(entry.job)
            except Exception as error:
                entry.status = "failed"
                entry.last_error_code = _error_code(error)
                entry.next_attempt_at = now + timedelta(seconds=min(300, 2**entry.attempts))
            else:
                entry.status = "published"
                entry.broker_message_id = str(getattr(message, "id", entry.job_id))[:100]
                entry.last_error_code = ""
                published += 1
            entry.save()
    return published


@transaction.atomic
def claim_job(*, job_id, worker_id, now=None):
    now = now or timezone.now()
    job = BackgroundJob.objects.select_for_update().filter(pk=job_id).first()
    if not job or job.status in ["done", "failed", "cancelled"]:
        return None
    if job.status == "running" and job.lease_expires_at and job.lease_expires_at > now:
        return None
    if job.status in ["pending", "retry_wait"] and job.next_attempt_at > now:
        return None
    job.status = "running"
    job.attempts += 1
    job.lease_owner = worker_id[:120]
    lease_seconds = (
        settings.AUTOMATIC_DIGEST_JOB_LEASE_SECONDS
        if job.kind == "automatic_digest_cycle"
        else settings.BACKGROUND_JOB_LEASE_SECONDS
    )
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.save()
    OutboxEntry.objects.filter(job=job).update(status="consumed", updated_at=now)
    return job


def _prepare_schedule_slot(parameters):
    slot = ScheduleSlot.objects.select_related("schedule_revision").get(pk=parameters["slot_id"])
    if slot.state in ["prepared", "closed"]:
        return slot.preparation_summary
    ScheduleSlot.objects.filter(pk=slot.pk).update(state="preparing")
    prepared = skipped = 0
    preferences = (
        PreferenceRevision.objects.filter(
            is_current=True,
            schedule_revision=slot.schedule_revision,
            subscriber__is_current=True,
            subscriber__setup_status="complete",
            subscriber__subscription_status="active",
            subscriber__access_status="allowed",
            subscriber__privacy_state="normal",
        )
        .select_related("subscriber")
        .order_by("subscriber_id")
    )
    for preference in preferences:
        try:
            prepare_delivery(subscriber=preference.subscriber, scheduled_at=slot.scheduled_at)
        except ValidationError:
            skipped += 1
        else:
            prepared += 1
    summary = {"prepared": prepared, "skipped": skipped}
    ScheduleSlot.objects.filter(pk=slot.pk).update(
        state="prepared", preparation_summary=summary, updated_at=timezone.now()
    )
    return summary


def _purge_source_text(parameters):
    batch_size = int(parameters.get("batch_size", 500))
    now = timezone.now()
    purged = 0
    with transaction.atomic():
        versions = list(
            PublicationVersion.objects.select_for_update(skip_locked=True)
            .filter(expires_at__lte=now, purged_at__isnull=True)
            .order_by("expires_at")[:batch_size]
        )
        for version in versions:
            purged += int(version.purge_content(at=now))
    return {"purged": purged}


def run_handler(job):
    if job.kind == "collect_source":
        call_command("collect_source", job.parameters["source_id"], limit=job.parameters.get("limit", 10))
        return {"source_id": job.parameters["source_id"]}
    if job.kind == "suggest_groupings":
        hours = int(job.parameters.get("hours", 48))
        rows = suggest_groupings(since=timezone.now() - timedelta(hours=hours))
        return {"created": len(rows)}
    if job.kind == "discover_editorial_candidates":
        slot = ScheduleSlot.objects.get(pk=job.parameters["slot_id"])
        return {"created": discover_editorial_candidates(slot=slot)}
    if job.kind == "automatic_digest_cycle":
        slot = ScheduleSlot.objects.select_related("schedule_revision").get(
            pk=job.parameters["slot_id"]
        )
        return run_automatic_digest_cycle(slot=slot)
    if job.kind == "generate_event_cards":
        slot = ScheduleSlot.objects.select_related("schedule_revision").get(
            pk=job.parameters["slot_id"]
        )
        event = Event.objects.get(pk=job.parameters["event_id"], status="approved")
        slot_key = f"{slot.schedule_revision.schedule_id}:{slot.scheduled_at.isoformat()}"
        provider = OpenRouterProvider()
        russian = draft_russian_card(event=event, slot_key=slot_key, provider=provider)
        english = translate_english_card(russian_card=russian, provider=provider)
        return {"ru_revision_id": russian.pk, "en_revision_id": english.pk}
    if job.kind == "build_editorial_editions":
        slot = ScheduleSlot.objects.get(pk=job.parameters["slot_id"])
        collection = Collection.objects.get(pk=job.parameters["collection_id"])
        russian, english = build_selected_editions(slot=slot, collection=collection)
        return {"ru_revision_id": russian.pk, "en_revision_id": english.pk}
    if job.kind == "erase_subscriber":
        from digest_service.subscriptions.erasure import execute_profile_erasure

        return execute_profile_erasure(request_id=job.parameters["request_id"])
    if job.kind == "purge_privacy_records":
        from digest_service.subscriptions.erasure import purge_expired_privacy_records

        return purge_expired_privacy_records()
    if job.kind == "create_database_backup":
        from digest_service.operations.backup_service import create_database_backup

        backup = create_database_backup(request_key=str(job.pk))
        return {"backup_id": str(backup.pk), "sha256": backup.sha256}
    if job.kind == "purge_backup_artifacts":
        from digest_service.operations.backup_service import purge_expired_backup_artifacts
        from digest_service.operations.guard_service import purge_expired_guard_artifacts

        result = purge_expired_backup_artifacts()
        result["guards_purged"] = purge_expired_guard_artifacts()
        return result
    if job.kind == "export_erasure_guard":
        from digest_service.operations.guard_service import create_erasure_guard_export

        export = create_erasure_guard_export(request_key=str(job.pk))
        return {"guard_backup_id": str(export.pk), "sha256": export.sha256}
    if job.kind == "prepare_schedule_slot":
        return _prepare_schedule_slot(job.parameters)
    if job.kind == "purge_source_text":
        return _purge_source_text(job.parameters)
    raise ValueError("unknown_job_kind")


def execute_job(*, job_id, contract_version=1, worker_id=None, now=None, handler=None):
    if contract_version != 1:
        raise ValueError("unsupported_contract_version")
    now = now or timezone.now()
    worker_id = worker_id or f"{socket.gethostname()}:{__import__('os').getpid()}"
    job = claim_job(job_id=job_id, worker_id=worker_id, now=now)
    if not job:
        return {"status": "not_claimed"}
    try:
        result = (handler or run_handler)(job) or {}
    except Exception as error:
        completed_at = timezone.now()
        with transaction.atomic():
            locked = BackgroundJob.objects.select_for_update().get(pk=job.pk)
            locked.last_error_code = _error_code(error)
            locked.lease_owner = ""
            locked.lease_expires_at = None
            if locked.attempts >= locked.max_attempts:
                locked.status = "failed"
                locked.finished_at = completed_at
                if locked.kind == "erase_subscriber":
                    from digest_service.subscriptions.models import ErasureRequest

                    ErasureRequest.objects.filter(
                        pk=locked.parameters.get("request_id"),
                        status__in=["accepted", "processing"],
                    ).update(
                        status="failed",
                        error_code=locked.last_error_code,
                        updated_at=completed_at,
                    )
            else:
                locked.status = "retry_wait"
                requested_delay = max(0, int(getattr(error, "retry_after_seconds", 0)))
                exponential_delay = min(3600, 30 * (2 ** (locked.attempts - 1)))
                locked.next_attempt_at = completed_at + timedelta(
                    seconds=max(exponential_delay, min(7 * 24 * 3600, requested_delay + 5))
                )
                OutboxEntry.objects.filter(job=locked).update(
                    status="pending", next_attempt_at=locked.next_attempt_at, updated_at=completed_at
                )
            locked.save()
        return {"status": locked.status, "error_code": locked.last_error_code}
    completed_at = timezone.now()
    BackgroundJob.objects.filter(pk=job.pk).update(
        status="done",
        result=result,
        lease_owner="",
        lease_expires_at=None,
        last_error_code="",
        finished_at=completed_at,
        updated_at=completed_at,
    )
    return {"status": "done", "result": result}


def run_due_jobs(*, limit=100, now=None, handler=None, worker_id="local-worker"):
    now = now or timezone.now()
    ids = list(
        BackgroundJob.objects.filter(
            Q(status__in=["pending", "retry_wait"], next_attempt_at__lte=now)
            | Q(status="running", lease_expires_at__lte=now)
        )
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )
    return [
        execute_job(job_id=pk, worker_id=worker_id, now=now, handler=handler) for pk in ids
    ]


def plan_schedule_slots(*, count=10, now=None):
    now = now or timezone.now()
    created = 0
    for schedule in Schedule.objects.filter(delivery_enabled=True).order_by("id"):
        revision = schedule.revisions.get(number=schedule.revision)
        for row in schedule.preview(count=count, after=now):
            if row["window_start"] is None:
                continue
            slot, was_created = ScheduleSlot.objects.get_or_create(
                schedule_revision=revision,
                scheduled_at=row["scheduled_at"],
                defaults={
                    "window_start": row["window_start"],
                    "window_end": row["window_end"],
                    "preparation_at": row["window_end"],
                    "delivery_deadline": row["scheduled_at"]
                    + timedelta(minutes=schedule.late_delivery_minutes),
                },
            )
            enqueue_job(
                kind="automatic_digest_cycle",
                parameters={"slot_id": slot.pk},
                idempotency_key=f"automatic-digest:{slot.pk}",
                next_attempt_at=max(now, slot.preparation_at),
                max_attempts=5,
            )
            enqueue_job(
                kind="prepare_schedule_slot",
                parameters={"slot_id": slot.pk},
                idempotency_key=f"prepare-slot:{slot.pk}",
                next_attempt_at=max(now, slot.scheduled_at),
            )
            created += int(was_created)
    ScheduleSlot.objects.filter(
        delivery_deadline__lt=now, state__in=["planned", "prepared"]
    ).update(state="closed", updated_at=now)
    return created


def enqueue_source_collection(*, now=None, interval_minutes=None):
    now = now or timezone.now()
    interval = interval_minutes or settings.SOURCE_COLLECTION_INTERVAL_MINUTES
    bucket = int(now.timestamp()) // (interval * 60)
    created = 0
    for source_id in Source.objects.filter(is_active=True).values_list("pk", flat=True):
        _, was_created = enqueue_job(
            kind="collect_source",
            parameters={"source_id": source_id, "limit": 10},
            idempotency_key=f"collect:{source_id}:{interval}:{bucket}",
            next_attempt_at=now,
        )
        created += int(was_created)
    return created


def enqueue_maintenance(*, now=None):
    now = now or timezone.now()
    day = now.date().isoformat()
    hour_bucket = now.strftime("%Y-%m-%dT%H")
    grouping = enqueue_job(
        kind="suggest_groupings",
        parameters={"hours": 48},
        idempotency_key=f"groupings:{hour_bucket}",
        next_attempt_at=now,
    )[1]
    purge = enqueue_job(
        kind="purge_source_text",
        parameters={"batch_size": 500},
        idempotency_key=f"purge:{day}",
        next_attempt_at=now,
    )[1]
    privacy = enqueue_job(
        kind="purge_privacy_records",
        parameters={},
        idempotency_key=f"purge-privacy:{day}",
        next_attempt_at=now,
    )[1]
    backup = False
    backup_purge = False
    if settings.BACKUP_SCHEDULE_ENABLED:
        guard = enqueue_job(
            kind="export_erasure_guard",
            parameters={},
            idempotency_key=f"erasure-guard:{day}",
            next_attempt_at=now,
            max_attempts=5,
        )[1]
        backup = enqueue_job(
            kind="create_database_backup",
            parameters={},
            idempotency_key=f"database-backup:{day}",
            next_attempt_at=now,
            max_attempts=2,
        )[1]
        backup_purge = enqueue_job(
            kind="purge_backup_artifacts",
            parameters={},
            idempotency_key=f"purge-backups:{day}",
            next_attempt_at=now,
        )[1]
    else:
        guard = False
    return (
        int(grouping)
        + int(purge)
        + int(privacy)
        + int(guard)
        + int(backup)
        + int(backup_purge)
    )
