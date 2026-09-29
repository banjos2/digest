from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from digest_service.catalog.models import Collection, SourceCollection
from digest_service.editorial.automatic import run_automatic_digest_cycle
from digest_service.editorial.models import (
    EditionRevision,
    EditorialSelection,
    EventCollectionAssignment,
    GroupingSuggestion,
)
from digest_service.editorial.providers import EnglishCardDraft, RussianCardDraft
from digest_service.editorial.service import (
    create_event_from_versions,
    draft_russian_card,
    translate_english_card,
)
from digest_service.editorial.workflow import (
    accept_grouping_suggestion,
    approve_card_pair,
    approve_edition_pair,
    build_selected_editions,
    discover_editorial_candidates,
    publish_edition_pair,
)
from digest_service.ingestion.parsers import MaterialCandidate
from digest_service.ingestion.service import store_candidate
from digest_service.operations.models import BackgroundJob
from digest_service.operations.service import plan_schedule_slots
from digest_service.scheduling.models import Schedule, ScheduleSlot


class WorkflowProvider:
    provider_name = "workflow-fixture"

    def draft_russian(self, event):
        return RussianCardDraft(
            title="Проверенный сюжет",
            summary="Два материала подтверждают основные факты выбранного редактором сюжета.",
            citation_membership_ids=tuple(item.membership_id for item in event.evidence),
        )

    def translate_english(self, *, title, summary):
        return EnglishCardDraft(
            title="A verified story",
            summary="Two reports support the key facts in the story selected by the editor.",
        )


class EditorialWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())

    def setup_slot_and_collection(self):
        collection = Collection.objects.filter(release_target="v1").first()
        collection.is_active = True
        collection.save()
        membership = SourceCollection.objects.filter(
            collection=collection, source__kind="website"
        ).select_related("source").first()
        membership.is_active = True
        membership.save()
        source = membership.source
        source.access_review_status = "approved"
        source.collection_check_status = "verified"
        source.access_review_note = "Fixture route approved for workflow tests"
        source.preferred_adapter = "fixture_reader"
        source.is_active = True
        source.save()
        schedule = Schedule.objects.get(pk="daily")
        schedule.delivery_enabled = True
        schedule.save()
        now = timezone.now()
        plan_schedule_slots(count=1, now=now)
        return ScheduleSlot.objects.get(), collection, source

    def make_version(self, source, slot, suffix="one"):
        candidate = MaterialCandidate(
            external_id=f"workflow-{suffix}",
            url=f"https://example.com/workflow-{suffix}",
            title=f"Verified workflow story {suffix}",
            body="A complete fixture article with facts for editorial review.",
            published_at=slot.window_start + timedelta(minutes=30),
            language="en",
            content_scope="full_article",
            metadata={"fixture": True},
        )
        return store_candidate(source, candidate, fetched_at=timezone.now())[1]

    def approved_event(self):
        slot, collection, source = self.setup_slot_and_collection()
        version = self.make_version(source, slot)
        event = create_event_from_versions(
            working_title="Verified workflow story", version_ids=[version.pk]
        )
        event.review_note = "Single-source fixture manually attributed and approved."
        event.status = "approved"
        event.save()
        return slot, collection, event

    def test_accepted_grouping_creates_one_review_event_idempotently(self):
        slot, _, first_source = self.setup_slot_and_collection()
        second_membership = SourceCollection.objects.filter(
            source__kind="website"
        ).exclude(source=first_source).select_related("source").first()
        first = self.make_version(first_source, slot, "first")
        second = self.make_version(second_membership.source, slot, "second")
        left, right = sorted([first, second], key=lambda row: row.pk)
        suggestion = GroupingSuggestion.objects.create(
            first_version=left,
            second_version=right,
            score="0.8000",
            reasons={"fixture": True},
        )
        event = accept_grouping_suggestion(suggestion=suggestion)
        again = accept_grouping_suggestion(suggestion=suggestion)
        suggestion.refresh_from_db()
        self.assertEqual(event.pk, again.pk)
        self.assertEqual(event.status, "review")
        self.assertEqual(event.memberships.count(), 2)
        self.assertEqual(suggestion.status, "accepted")

    def test_single_origin_approval_requires_explanation(self):
        slot, _, source = self.setup_slot_and_collection()
        event = create_event_from_versions(
            working_title="Single source", version_ids=[self.make_version(source, slot).pk]
        )
        event.status = "approved"
        with self.assertRaises(ValidationError):
            event.save()
        event.review_note = "Primary-source exception checked by editor."
        event.save()

    def test_candidate_discovery_and_weighted_thresholds(self):
        slot, collection, event = self.approved_event()
        self.assertEqual(discover_editorial_candidates(slot=slot), 1)
        self.assertEqual(discover_editorial_candidates(slot=slot), 0)
        selection = EditorialSelection.objects.get(event=event)
        selection.status = "main"
        selection.impact = 2
        selection.relevance = selection.novelty = selection.timeliness = selection.corroboration = 5
        selection.rationale = "Fixture assessment"
        with self.assertRaises(ValidationError):
            selection.save()
        selection.impact = 5
        selection.save()
        self.assertEqual(selection.weighted_score, 100)
        self.assertEqual(selection.collection, collection)

    def test_manual_collection_assignment_routes_general_source_to_topic(self):
        slot, source_collection, source = self.setup_slot_and_collection()
        topic_collection = (
            Collection.objects.filter(release_target="v1")
            .exclude(pk=source_collection.pk)
            .first()
        )
        topic_collection.is_active = True
        topic_collection.save()
        event = create_event_from_versions(
            working_title="OpenAI changes its release plans",
            version_ids=[self.make_version(source, slot, "topic-route").pk],
        )
        event.status = "review"
        event.save()
        EventCollectionAssignment.objects.create(
            event=event,
            collection=topic_collection,
            assigned_by="editor",
            rationale="The story directly concerns the topic collection.",
        )

        self.assertEqual(discover_editorial_candidates(slot=slot), 2)
        self.assertTrue(
            EditorialSelection.objects.filter(event=event, collection=topic_collection).exists()
        )

    def test_bilingual_review_and_publication_are_explicit_pair_transitions(self):
        slot, collection, event = self.approved_event()
        discover_editorial_candidates(slot=slot)
        selection = EditorialSelection.objects.get()
        selection.status = "main"
        selection.impact = 5
        selection.relevance = 5
        selection.novelty = 5
        selection.timeliness = 5
        selection.corroboration = 5
        selection.rationale = "High-impact verified fixture story."
        selection.save()
        slot_key = f"{slot.schedule_revision.schedule_id}:{slot.scheduled_at.isoformat()}"
        russian_card = draft_russian_card(
            event=event, slot_key=slot_key, provider=WorkflowProvider()
        )
        english_card = translate_english_card(
            russian_card=russian_card, provider=WorkflowProvider()
        )
        approve_card_pair(revision=english_card)
        russian, english = build_selected_editions(slot=slot, collection=collection)
        with self.assertRaises(ValidationError):
            publish_edition_pair(revision=russian)
        approve_edition_pair(revision=english)
        publish_edition_pair(revision=russian)
        russian.refresh_from_db()
        english.refresh_from_db()
        self.assertEqual(russian.editorial_state, "published")
        self.assertEqual(english.editorial_state, "published")
        again_ru, again_en = build_selected_editions(slot=slot, collection=collection)
        self.assertEqual((again_ru.pk, again_en.pk), (russian.pk, english.pk))

    def test_slot_plans_automatic_digest_before_delivery_preparation(self):
        slot, _, _ = self.setup_slot_and_collection()
        discovery = BackgroundJob.objects.get(kind="automatic_digest_cycle")
        delivery = BackgroundJob.objects.get(kind="prepare_schedule_slot")
        self.assertEqual(discovery.next_attempt_at, slot.preparation_at)
        self.assertEqual(discovery.max_attempts, 5)
        self.assertEqual(delivery.next_attempt_at, slot.scheduled_at)

    def test_automatic_cycle_publishes_bilingual_edition_without_manual_actions(self):
        slot, collection, source = self.setup_slot_and_collection()
        self.make_version(source, slot, "automatic")

        result = run_automatic_digest_cycle(slot=slot, provider=WorkflowProvider())

        self.assertEqual(result["singletons"], 1)
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["cards"], 1)
        self.assertEqual(result["editions"], 1)
        self.assertEqual(
            set(EditionRevision.objects.values_list("language", "editorial_state")),
            {("ru", "published"), ("en", "published")},
        )
        russian = EditionRevision.objects.get(language="ru")
        self.assertIn("</b>\n\n", russian.rendered_parts[0])
        repeated = run_automatic_digest_cycle(slot=slot, provider=WorkflowProvider())
        self.assertEqual(repeated["selected"], 1)
        self.assertEqual(EditionRevision.objects.count(), 2)
