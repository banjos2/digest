from datetime import UTC, datetime, timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from digest_service.catalog.models import Collection
from digest_service.operations.models import BackgroundJob, OutboxEntry, ProcessLease
from digest_service.operations.service import (
    broker_message,
    claim_job,
    enqueue_job,
    execute_job,
    plan_schedule_slots,
    publish_outbox,
    run_due_jobs,
)
from digest_service.scheduling.models import Schedule, ScheduleSlot
from digest_service.subscriptions.service import choose_language, save_preferences, start_subscriber


class BackgroundOperationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())

    def activate_daily_schedule(self):
        schedule = Schedule.objects.get(pk="daily")
        schedule.delivery_enabled = True
        schedule.visible_to_new_subscribers = True
        schedule.save()
        return schedule

    def test_slot_planning_is_idempotent_and_uses_saved_revision(self):
        schedule = self.activate_daily_schedule()
        now = datetime(2026, 9, 11, 8, tzinfo=UTC)
        self.assertEqual(plan_schedule_slots(count=3, now=now), 3)
        self.assertEqual(plan_schedule_slots(count=3, now=now), 0)
        self.assertEqual(ScheduleSlot.objects.count(), 3)
        self.assertEqual(BackgroundJob.objects.count(), 6)
        slot = ScheduleSlot.objects.first()
        self.assertEqual(slot.schedule_revision.number, schedule.revision)
        self.assertEqual(slot.preparation_at, slot.window_end)
        self.assertEqual(slot.delivery_deadline - slot.scheduled_at, timedelta(minutes=60))

    def test_enqueue_is_atomic_idempotent_and_rejects_unknown_parameters(self):
        first, created = enqueue_job(
            kind="suggest_groupings",
            parameters={"hours": 48},
            idempotency_key="grouping:test",
        )
        second, created_again = enqueue_job(
            kind="suggest_groupings",
            parameters={"hours": 48},
            idempotency_key="grouping:test",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(OutboxEntry.objects.count(), 1)
        with self.assertRaises(ValidationError):
            enqueue_job(
                kind="suggest_groupings",
                parameters={"hours": 48, "api_key": "must-not-be-stored"},
                idempotency_key="bad:test",
            )

    def test_outbox_broker_contract_contains_only_job_identity(self):
        job, _ = enqueue_job(
            kind="purge_source_text",
            parameters={"batch_size": 10},
            idempotency_key="purge:test",
        )
        seen = []

        class Message:
            id = "broker-id"

        self.assertEqual(publish_outbox(producer=lambda row: seen.append(broker_message(row)) or Message()), 1)
        self.assertEqual(seen, [{"job_id": str(job.pk), "contract_version": 1}])
        job.outbox.refresh_from_db()
        self.assertEqual(job.outbox.status, "published")

    def test_failed_job_retries_then_completes_without_duplicate_job(self):
        job, _ = enqueue_job(
            kind="suggest_groupings",
            parameters={"hours": 48},
            idempotency_key="retry:test",
        )
        first = execute_job(
            job_id=job.pk,
            worker_id="test-worker",
            handler=lambda row: (_ for _ in ()).throw(RuntimeError("temporary")),
        )
        self.assertEqual(first["status"], "retry_wait")
        job.refresh_from_db()
        retry_at = job.next_attempt_at
        self.assertGreater(retry_at, timezone.now())
        second = execute_job(
            job_id=job.pk,
            worker_id="test-worker",
            now=retry_at,
            handler=lambda row: {"created": 2},
        )
        self.assertEqual(second["status"], "done")
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.result, {"created": 2})
        self.assertEqual(BackgroundJob.objects.count(), 1)

    def test_retryable_error_respects_provider_wait(self):
        class ProviderWait(RuntimeError):
            retry_after_seconds = 600

        job, _ = enqueue_job(
            kind="suggest_groupings",
            parameters={"hours": 48},
            idempotency_key="provider-wait:test",
        )
        before = timezone.now()
        result = execute_job(
            job_id=job.pk,
            worker_id="test-worker",
            handler=lambda row: (_ for _ in ()).throw(ProviderWait("rate_limited")),
        )
        self.assertEqual(result["status"], "retry_wait")
        job.refresh_from_db()
        self.assertGreaterEqual(job.next_attempt_at, before + timedelta(seconds=605))

    def test_expired_lease_is_recovered(self):
        job, _ = enqueue_job(
            kind="purge_source_text",
            parameters={"batch_size": 10},
            idempotency_key="lease:test",
        )
        now = timezone.now()
        BackgroundJob.objects.filter(pk=job.pk).update(
            status="running", lease_owner="lost-worker", lease_expires_at=now - timedelta(seconds=1)
        )
        results = run_due_jobs(now=now, handler=lambda row: {"recovered": True})
        self.assertEqual(results, [{"status": "done", "result": {"recovered": True}}])

    def test_automatic_digest_has_a_long_generation_lease(self):
        job, _ = enqueue_job(
            kind="automatic_digest_cycle",
            parameters={"slot_id": 1},
            idempotency_key="automatic-lease:test",
        )
        now = timezone.now()
        claimed = claim_job(job_id=job.pk, worker_id="test-worker", now=now)
        self.assertEqual(
            claimed.lease_expires_at,
            now + timedelta(seconds=2100),
        )

    def test_slot_preparation_records_missing_editions_per_subscriber(self):
        schedule = self.activate_daily_schedule()
        collection = Collection.objects.filter(release_target="v1").first()
        collection.is_active = True
        collection.save()
        subscriber, _ = start_subscriber(telegram_user_id=9001, telegram_chat_id=9001)
        subscriber = choose_language(subscriber=subscriber, language="ru")
        save_preferences(
            subscriber=subscriber,
            collection_ids=[collection.pk],
            schedule_id=schedule.pk,
            effective_from=timezone.now() - timedelta(minutes=1),
        )
        now = timezone.now()
        plan_schedule_slots(count=1, now=now)
        slot = ScheduleSlot.objects.get()
        job = BackgroundJob.objects.get(kind="prepare_schedule_slot")
        result = execute_job(
            job_id=job.pk,
            worker_id="test-worker",
            now=slot.scheduled_at,
        )
        self.assertEqual(result["status"], "done")
        slot.refresh_from_db()
        self.assertEqual(slot.state, "prepared")
        self.assertEqual(slot.preparation_summary, {"prepared": 0, "skipped": 1})


class ProcessLeaseHealthTests(TestCase):
    def test_check_process_lease_accepts_a_live_lease(self):
        now = timezone.now()
        ProcessLease.objects.create(
            name="scheduler-test",
            owner="worker-1",
            heartbeat_at=now,
            lease_expires_at=now + timedelta(minutes=1),
        )

        output = StringIO()
        call_command("check_process_lease", "scheduler-test", stdout=output)

        self.assertIn("process_lease_ready=scheduler-test", output.getvalue())

    def test_check_process_lease_rejects_an_expired_lease(self):
        now = timezone.now()
        ProcessLease.objects.create(
            name="scheduler-test",
            owner="worker-1",
            heartbeat_at=now - timedelta(minutes=2),
            lease_expires_at=now - timedelta(minutes=1),
        )

        with self.assertRaisesMessage(CommandError, "process_lease_expired=scheduler-test"):
            call_command("check_process_lease", "scheduler-test")
