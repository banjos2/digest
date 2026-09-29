from datetime import UTC, datetime
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from digest_service.catalog.models import Source
from digest_service.editorial.edition_service import (
    build_english_edition,
    build_russian_edition,
    get_or_create_edition,
    visible_utf16_units,
)
from digest_service.editorial.grouping import lexical_similarity, suggest_groupings
from digest_service.editorial.models import EditionItem, EventCardRevision, GroupingSuggestion
from digest_service.editorial.providers import EnglishCardDraft, RussianCardDraft
from digest_service.editorial.service import (
    create_event_from_versions,
    draft_russian_card,
    translate_english_card,
)
from digest_service.ingestion.parsers import MaterialCandidate
from digest_service.ingestion.service import store_candidate
from digest_service.scheduling.models import Schedule


class FixtureProvider:
    provider_name = "test-fixture"

    def __init__(self):
        self.russian_summary = (
            "Две независимые публикации сообщили о выпуске открытой модели. "
            "В материалах описаны лицензия и доступность весов."
        )

    def draft_russian(self, event):
        return RussianCardDraft(
            title="Опубликована новая открытая модель ИИ",
            summary=self.russian_summary,
            citation_membership_ids=tuple(item.membership_id for item in event.evidence),
        )

    def translate_english(self, *, title, summary):
        return EnglishCardDraft(
            title="A new open AI model has been released",
            summary=(
                "Two independent reports announced the release of an open model. "
                "They describe its licence and the availability of its weights."
            ),
        )


class EditorialPipelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())

    def make_version(self, source, external_id, title, body):
        candidate = MaterialCandidate(
            external_id=external_id,
            url=f"https://example.com/{external_id}",
            title=title,
            body=body,
            published_at=datetime(2026, 9, 10, 8, tzinfo=UTC),
            language="en",
            content_scope="full_article",
            metadata={"fixture": True},
        )
        return store_candidate(source, candidate, fetched_at=datetime(2026, 9, 10, 9, tzinfo=UTC))[
            1
        ]

    def event(self):
        sources = list(Source.objects.filter(kind="website")[:2])
        first = self.make_version(
            sources[0],
            "open-model-one",
            "A new open model was released",
            "The publisher released model weights under a documented open licence.",
        )
        second = self.make_version(
            sources[1],
            "open-model-two",
            "Independent coverage of the model release",
            "A separate publication confirmed the release and described availability.",
        )
        return create_event_from_versions(
            working_title="Open model release", version_ids=[first.pk, second.pk]
        )

    def test_ru_then_en_are_linked_to_exact_evidence_and_revision(self):
        event = self.event()
        provider = FixtureProvider()
        russian = draft_russian_card(event=event, slot_key="daily:2026-09-10", provider=provider)
        english = translate_english_card(russian_card=russian, provider=provider)
        self.assertEqual(russian.editorial_state, "draft")
        self.assertEqual(english.source_ru_revision, russian)
        self.assertEqual(english.evidence_hash, russian.evidence_hash)
        self.assertEqual(english.citation_membership_ids, russian.citation_membership_ids)
        self.assertNotEqual(english.content_hash, russian.content_hash)

    def test_new_russian_revision_invalidates_current_translation(self):
        event = self.event()
        provider = FixtureProvider()
        first_ru = draft_russian_card(event=event, slot_key="daily:2026-09-10", provider=provider)
        first_en = translate_english_card(russian_card=first_ru, provider=provider)
        provider.russian_summary += " Редактор добавил существенное уточнение."
        second_ru = draft_russian_card(
            event=event, slot_key="daily:2026-09-10", provider=provider, force=True
        )
        first_ru.refresh_from_db()
        first_en.refresh_from_db()
        self.assertFalse(first_ru.is_current)
        self.assertFalse(first_en.is_current)
        self.assertEqual(second_ru.revision, 2)
        with self.assertRaises(ValidationError):
            translate_english_card(russian_card=first_ru, provider=provider)

    def test_new_evidence_invalidates_cards_without_changing_old_snapshot(self):
        event = self.event()
        provider = FixtureProvider()
        russian = draft_russian_card(event=event, slot_key="daily:2026-09-10", provider=provider)
        old_evidence = list(russian.evidence_membership_ids)
        source = Source.objects.filter(kind="website")[2]
        third = self.make_version(
            source,
            "open-model-three",
            "A third report",
            "A third publication added a material detail about the model release.",
        )
        event.memberships.create(publication_version=third, role="context")
        russian.refresh_from_db()
        self.assertFalse(russian.is_current)
        self.assertEqual(russian.evidence_membership_ids, old_evidence)

    def test_unknown_evidence_reference_is_rejected(self):
        event = self.event()

        class BadProvider(FixtureProvider):
            def draft_russian(self, event):
                return RussianCardDraft("Title", "Summary", (999999,))

        with self.assertRaises(ValidationError):
            draft_russian_card(event=event, slot_key="daily:2026-09-10", provider=BadProvider())
        self.assertFalse(EventCardRevision.objects.exists())

    def test_card_text_is_immutable_but_editorial_state_can_change(self):
        event = self.event()
        card = draft_russian_card(
            event=event, slot_key="daily:2026-09-10", provider=FixtureProvider()
        )
        card.editorial_state = "review"
        card.save()
        card.summary = "Silently replaced text"
        with self.assertRaises(ValidationError):
            card.save()

    def test_duplicate_or_missing_versions_do_not_create_event(self):
        event = self.event()
        version_id = event.memberships.first().publication_version_id
        with self.assertRaises(ValidationError):
            create_event_from_versions(working_title="Duplicate", version_ids=[version_id] * 2)
        with self.assertRaises(ValidationError):
            create_event_from_versions(working_title="Missing", version_ids=[999999])

    def test_full_ru_and_en_edition_preserve_order_and_links(self):
        provider = FixtureProvider()
        first_event = self.event()
        second_event = self.event()
        slot_key = "daily:2026-09-10"
        first_ru = draft_russian_card(event=first_event, slot_key=slot_key, provider=provider)
        second_ru = draft_russian_card(event=second_event, slot_key=slot_key, provider=provider)
        first_en = translate_english_card(russian_card=first_ru, provider=provider)
        translate_english_card(russian_card=second_ru, provider=provider)
        schedule = Schedule.objects.get(pk="daily")
        timing = schedule.preview(after=datetime(2026, 9, 9, tzinfo=UTC))[0]
        edition = get_or_create_edition(
            collection=schedule_source_collection(first_event),
            schedule_revision=schedule.revisions.get(number=schedule.revision),
            scheduled_at=timing["scheduled_at"],
            window_start=timing["window_start"],
            window_end=timing["window_end"],
        )
        russian = build_russian_edition(
            edition=edition, main_cards=[first_ru], additional_cards=[second_ru]
        )
        english = build_english_edition(russian_revision=russian)
        self.assertEqual(english.source_ru_revision, russian)
        self.assertEqual(
            [item["event_id"] for item in english.item_snapshot],
            [item["event_id"] for item in russian.item_snapshot],
        )
        self.assertEqual(EditionItem.objects.filter(edition_revision=english).count(), 2)
        self.assertIn("News digest", english.rendered_parts[0])
        self.assertIn("Top stories", english.rendered_parts[0])
        self.assertIn("Also worth a look", english.rendered_parts[0])
        self.assertIn("https://example.com/", russian.rendered_parts[0])
        self.assertLessEqual(visible_utf16_units(russian.rendered_parts[0]), 3900)
        self.assertEqual(first_en.source_ru_revision, first_ru)

    def test_edition_rejects_duplicate_event(self):
        event = self.event()
        card = draft_russian_card(
            event=event, slot_key="daily:2026-09-10", provider=FixtureProvider()
        )
        schedule = Schedule.objects.get(pk="daily")
        timing = schedule.preview(after=datetime(2026, 9, 9, tzinfo=UTC))[0]
        edition = get_or_create_edition(
            collection=schedule_source_collection(event),
            schedule_revision=schedule.revisions.get(number=schedule.revision),
            scheduled_at=timing["scheduled_at"],
            window_start=timing["window_start"],
            window_end=timing["window_end"],
        )
        with self.assertRaises(ValidationError):
            build_russian_edition(edition=edition, main_cards=[card, card], additional_cards=[])

    def test_grouping_creates_review_suggestion_only_for_similar_titles(self):
        sources = list(Source.objects.filter(kind="website")[:3])
        self.make_version(
            sources[0],
            "group-one",
            "Open AI model released with downloadable weights",
            "The first report describes a model release and its downloadable weights.",
        )
        self.make_version(
            sources[1],
            "group-two",
            "Open AI model released with downloadable model weights",
            "The second report independently describes the same announced release.",
        )
        self.make_version(
            sources[2],
            "unrelated",
            "Central bank changes its interest rate",
            "An unrelated economics report covers a central bank decision.",
        )
        created = suggest_groupings(since=datetime(2026, 9, 9, tzinfo=UTC), threshold=0.55)
        self.assertEqual(len(created), 1)
        suggestion = GroupingSuggestion.objects.get()
        self.assertEqual(suggestion.status, "pending")
        self.assertIn("same_origin_group", suggestion.reasons)
        self.assertFalse(suggestion.first_version.events.exists())
        self.assertGreater(lexical_similarity("same model release", "same model release")[0], 0.9)

    def test_grouping_handles_russian_inflections_and_different_title_lengths(self):
        score, jaccard, overlap, _ = lexical_similarity(
            "Трамп призвал Украину прекратить удары по российским производителям дизельного топлива",
            "Президент США Дональд Трамп объявил, что призвал президента Украины Владимира "
            "Зеленского прекратить удары по российским предприятиям, занимающимся "
            "производством дизельного топлива",
        )
        self.assertGreaterEqual(score, 0.55)
        self.assertGreater(overlap, jaccard)

    def test_grouping_uses_shared_named_anchor_for_rephrased_headline(self):
        score, _, _, _ = lexical_similarity(
            "OpenAI объяснила отказ от выхода на биржу сомнениями в безопасности развития ИИ",
            "OpenAI отказалась от IPO в 2026 году и поддержала замедление ИИ вместе с "
            "другими разработчиками",
        )
        self.assertGreaterEqual(score, 0.55)

    def test_grouping_keeps_unrelated_headlines_below_threshold(self):
        score, _, _, _ = lexical_similarity(
            "OpenAI отказалась от IPO в 2026 году",
            "Центробанк сохранил ключевую ставку",
        )
        self.assertLess(score, 0.55)

    def test_grouping_does_not_boost_a_single_geographic_anchor(self):
        score, _, _, _ = lexical_similarity(
            "Десятилетиями гособлигации США считались безопасным активом",
            "Бывший министр армии США впервые появился после отставки",
        )
        self.assertLess(score, 0.55)


def schedule_source_collection(event):
    source = event.memberships.first().publication_version.publication.source
    return source.collections.filter(release_target="v1").first()
