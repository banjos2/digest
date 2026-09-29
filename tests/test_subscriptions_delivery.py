from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from digest_service.catalog.models import Collection, Source
from digest_service.delivery.models import Delivery, DeliveryPart
from digest_service.delivery.service import (
    claim_next_part,
    prepare_delivery,
    record_part_result,
)
from digest_service.editorial.edition_service import (
    build_russian_edition,
    get_or_create_edition,
    visible_utf16_units,
)
from digest_service.editorial.providers import EnglishCardDraft, RussianCardDraft
from digest_service.editorial.service import create_event_from_versions, draft_russian_card
from digest_service.ingestion.parsers import MaterialCandidate
from digest_service.ingestion.service import store_candidate
from digest_service.scheduling.models import Schedule
from digest_service.subscriptions.service import (
    choose_language,
    pause_subscriber,
    save_preferences,
    start_subscriber,
)
from digest_service.telegram_bot.recovery import recover_stale_telegram_claims


class SubscriberFixtureProvider:
    provider_name = "subscriber-fixture"

    def draft_russian(self, event):
        return RussianCardDraft(
            title=event.working_title,
            summary=f"Подтверждённое описание события «{event.working_title}».",
            citation_membership_ids=tuple(item.membership_id for item in event.evidence),
        )

    def translate_english(self, *, title, summary):
        return EnglishCardDraft(title=title, summary=summary)


class SubscriptionAndDeliveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())
        cls.collections = list(
            Collection.objects.filter(release_target="v1").order_by("sort_order", "id")[:2]
        )
        for collection in cls.collections:
            collection.is_active = True
            collection.save()
        cls.schedule = Schedule.objects.get(pk="daily")
        cls.schedule.visible_to_new_subscribers = True
        cls.schedule.delivery_enabled = True
        cls.schedule.save()

    def make_version(self, source, external_id, title):
        candidate = MaterialCandidate(
            external_id=external_id,
            url=f"https://example.com/{external_id}",
            title=title,
            body=f"Independent source text for {title}.",
            published_at=timezone.now() - timedelta(hours=2),
            language="en",
            content_scope="full_article",
            metadata={"fixture": True},
        )
        return store_candidate(source, candidate, fetched_at=timezone.now())[1]

    def make_event(self, slug, title, source_offset):
        sources = list(Source.objects.filter(kind="website")[source_offset : source_offset + 2])
        versions = [
            self.make_version(source, f"{slug}-{number}", title)
            for number, source in enumerate(sources, start=1)
        ]
        return create_event_from_versions(
            working_title=title,
            version_ids=[version.pk for version in versions],
        )

    def subscriber(self):
        subscriber, created = start_subscriber(telegram_user_id=10001, telegram_chat_id=10001)
        self.assertTrue(created)
        subscriber = choose_language(subscriber=subscriber, language="ru")
        effective_from = timezone.now() - timedelta(minutes=1)
        preference = save_preferences(
            subscriber=subscriber,
            collection_ids=[item.pk for item in self.collections],
            schedule_id=self.schedule.pk,
            effective_from=effective_from,
        )
        subscriber.refresh_from_db()
        return subscriber, preference

    def published_editions(self):
        provider = SubscriberFixtureProvider()
        first_event = self.make_event("first-event", "Первое событие", 0)
        second_event = self.make_event("second-event", "Второе событие", 2)
        slot_key = f"daily:{timezone.localdate():%Y-%m-%d}"
        first_card = draft_russian_card(event=first_event, slot_key=slot_key, provider=provider)
        second_card = draft_russian_card(event=second_event, slot_key=slot_key, provider=provider)
        timing = self.schedule.preview(after=timezone.now())[0]
        schedule_revision = self.schedule.revisions.get(number=self.schedule.revision)
        first_edition = get_or_create_edition(
            collection=self.collections[0],
            schedule_revision=schedule_revision,
            scheduled_at=timing["scheduled_at"],
            window_start=timing["window_start"],
            window_end=timing["window_end"],
        )
        first_revision = build_russian_edition(
            edition=first_edition,
            main_cards=[first_card],
            additional_cards=[second_card],
        )
        first_revision.editorial_state = "published"
        first_revision.save()
        second_edition = get_or_create_edition(
            collection=self.collections[1],
            schedule_revision=schedule_revision,
            scheduled_at=timing["scheduled_at"],
            window_start=timing["window_start"],
            window_end=timing["window_end"],
        )
        second_revision = build_russian_edition(
            edition=second_edition,
            main_cards=[second_card],
            additional_cards=[],
        )
        second_revision.editorial_state = "published"
        second_revision.save()
        return timing["scheduled_at"], first_event, second_event

    def test_onboarding_preferences_pause_and_start_resume(self):
        subscriber, preference = self.subscriber()
        self.assertEqual(subscriber.setup_status, "complete")
        self.assertEqual(subscriber.subscription_status, "active")
        self.assertEqual(preference.selected_collections.count(), 2)
        paused = pause_subscriber(subscriber=subscriber)
        self.assertEqual(paused.subscription_status, "paused")
        resumed, created = start_subscriber(telegram_user_id=10001, telegram_chat_id=10001)
        self.assertFalse(created)
        self.assertEqual(resumed.pk, subscriber.pk)
        self.assertEqual(resumed.subscription_status, "active")

    def test_preferences_are_versioned_and_pause_is_preserved(self):
        subscriber, first = self.subscriber()
        subscriber = pause_subscriber(subscriber=subscriber)
        second = save_preferences(
            subscriber=subscriber,
            collection_ids=[self.collections[0].pk],
            schedule_id=self.schedule.pk,
        )
        first.refresh_from_db()
        subscriber.refresh_from_db()
        self.assertFalse(first.is_current)
        self.assertEqual(second.revision, 2)
        self.assertEqual(subscriber.subscription_status, "paused")
        first.digest_language = "en"
        with self.assertRaises(ValidationError):
            first.save()

    def test_invalid_or_empty_collection_selection_is_rejected(self):
        subscriber, _ = start_subscriber(telegram_user_id=20002, telegram_chat_id=20002)
        with self.assertRaises(ValidationError):
            save_preferences(
                subscriber=subscriber,
                collection_ids=[],
                schedule_id=self.schedule.pk,
            )
        with self.assertRaises(ValidationError):
            save_preferences(
                subscriber=subscriber,
                collection_ids=["youtube"],
                schedule_id=self.schedule.pk,
            )

    def test_database_rejects_active_incomplete_or_erasing_subscriber(self):
        subscriber, _ = start_subscriber(telegram_user_id=30003, telegram_chat_id=30003)
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(subscriber).objects.filter(pk=subscriber.pk).update(subscription_status="active")
        subscriber, _ = self.subscriber()
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(subscriber).objects.filter(pk=subscriber.pk).update(
                privacy_state="erasure_pending"
            )

    def test_manifest_deduplicates_by_event_and_is_idempotent(self):
        subscriber, _ = self.subscriber()
        scheduled_at, first_event, second_event = self.published_editions()
        delivery = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        again = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        self.assertEqual(again.pk, delivery.pk)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(len(delivery.manifest.edition_revision_ids), 2)
        self.assertEqual(len(delivery.manifest.kept_occurrences), 2)
        self.assertEqual(len(delivery.manifest.omitted_occurrences), 1)
        self.assertEqual(delivery.manifest.omitted_occurrences[0]["event_id"], str(second_event.pk))
        bodies = "\n".join(delivery.parts.values_list("body", flat=True))
        self.assertEqual(bodies.count(">Первое событие</b>"), 1)
        self.assertEqual(bodies.count(">Второе событие</b>"), 1)
        self.assertIn(
            str(first_event.pk), {item["event_id"] for item in delivery.manifest.kept_occurrences}
        )
        self.assertTrue(
            all(
                visible_utf16_units(body) <= 3900
                for body in delivery.parts.values_list("body", flat=True)
            )
        )

    def test_new_preferences_cancel_unsubmitted_automatic_delivery(self):
        subscriber, _ = self.subscriber()
        scheduled_at, _, _ = self.published_editions()
        delivery = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        save_preferences(
            subscriber=subscriber,
            collection_ids=[self.collections[0].pk],
            schedule_id=self.schedule.pk,
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "cancelled")
        self.assertFalse(delivery.parts.exclude(status="cancelled").exists())

    def test_disabled_selected_collection_is_reported_as_missing(self):
        subscriber, _ = self.subscriber()
        scheduled_at, _, _ = self.published_editions()
        disabled = self.collections[1]
        disabled.is_active = False
        disabled.save()
        delivery = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        self.assertEqual(delivery.manifest.missing_collection_ids, [disabled.pk])
        bodies = "\n".join(delivery.parts.values_list("body", flat=True))
        self.assertIn("Пока не готовы", bodies)
        self.assertIn(disabled.name_ru, bodies)

    def test_pause_during_in_flight_part_finishes_as_cancelled(self):
        subscriber, _ = self.subscriber()
        scheduled_at, _, _ = self.published_editions()
        delivery = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        first = claim_next_part(delivery=delivery)
        pause_subscriber(subscriber=subscriber)
        record_part_result(part=first, outcome="sent", telegram_message_id="601")
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "cancelled")
        self.assertEqual(delivery.parts.get(part_number=1).status, "sent")
        self.assertFalse(delivery.parts.exclude(status__in=["sent", "cancelled"]).exists())

    def test_part_state_machine_stops_after_unknown_result(self):
        subscriber, _ = self.subscriber()
        scheduled_at, _, _ = self.published_editions()
        delivery = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        first = claim_next_part(delivery=delivery)
        self.assertIsNotNone(first)
        self.assertIn("delivery", first._state.fields_cache)
        self.assertIn("subscriber_generation", first.delivery._state.fields_cache)
        record_part_result(part=first, outcome="sent", telegram_message_id="501")
        second = claim_next_part(delivery=delivery)
        self.assertIsNotNone(second)
        record_part_result(part=second, outcome="unknown", error_code="connection_lost")
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "unknown")
        self.assertIsNone(claim_next_part(delivery=delivery))
        self.assertEqual(DeliveryPart.objects.filter(status="unknown").count(), 1)

    def test_stale_in_flight_delivery_part_is_not_retried(self):
        subscriber, _ = self.subscriber()
        scheduled_at, _, _ = self.published_editions()
        delivery = prepare_delivery(subscriber=subscriber, scheduled_at=scheduled_at)
        claimed_at = timezone.now()
        part = claim_next_part(delivery=delivery, now=claimed_at)
        result = recover_stale_telegram_claims(
            now=claimed_at + timedelta(seconds=601)
        )
        part.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(result["parts_unknown"], 1)
        self.assertEqual(part.status, "unknown")
        self.assertEqual(delivery.status, "unknown")
        self.assertEqual(part.attempts.get().error_code, "sender_lost_after_claim")
