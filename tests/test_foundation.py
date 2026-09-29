import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from digest_service.catalog.admin import CollectionForm
from digest_service.catalog.models import Collection, PublisherProfile, Source, SourceCollection
from digest_service.core.management.commands.import_seed import FILENAMES
from digest_service.core.models import SeedSnapshot
from digest_service.scheduling.admin import ScheduleForm
from digest_service.scheduling.models import Schedule, ScheduleRevision


class FoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())
        cls.owner = get_user_model().objects.create_superuser(
            "test-owner", password="test-only-password"
        )

    def test_complete_seed_catalogue_and_policies(self):
        self.assertEqual(Collection.objects.count(), 11)
        self.assertEqual(Collection.objects.filter(release_target="v1").count(), 10)
        self.assertEqual(PublisherProfile.objects.count(), 75)
        self.assertEqual(Source.objects.filter(kind="website").count(), 58)
        self.assertEqual(Source.objects.filter(kind="telegram").count(), 43)
        self.assertEqual(SourceCollection.objects.count(), 207)
        self.assertEqual(SeedSnapshot.objects.count(), 6)
        self.assertEqual(Collection.objects.get(pk="ai").name_ru, "Искусственный интеллект")
        self.assertEqual(Source.objects.exclude(retention_policy_id="news_default_v1").count(), 0)

    def test_import_never_activates_collection_source_or_schedule(self):
        self.assertFalse(Collection.objects.filter(is_active=True).exists())
        self.assertFalse(Source.objects.filter(is_active=True).exists())
        self.assertFalse(SourceCollection.objects.filter(is_active=True).exists())
        self.assertFalse(Schedule.objects.filter(delivery_enabled=True).exists())

    def test_repeated_import_preserves_admin_changes(self):
        collection = Collection.objects.get(pk="ai")
        collection.name_ru = "Новости ИИ"
        collection.save()
        source = Source.objects.get(pk="meduza_website")
        source.preferred_feed_url = "https://example.org/changed-feed"
        source.save()
        schedule = Schedule.objects.get(pk="daily")
        schedule.local_times = ["14:00"]
        schedule.save()
        revisions = ScheduleRevision.objects.count()
        call_command("import_seed", stdout=StringIO())
        self.assertEqual(Collection.objects.get(pk="ai").name_ru, "Новости ИИ")
        self.assertEqual(
            Source.objects.get(pk=source.pk).preferred_feed_url, source.preferred_feed_url
        )
        self.assertEqual(Schedule.objects.get(pk="daily").local_times, ["14:00"])
        self.assertEqual(Source.objects.count(), 101)
        self.assertEqual(ScheduleRevision.objects.count(), revisions)
        self.assertEqual(SeedSnapshot.objects.count(), 6)

    def test_invalid_import_rolls_back_new_rows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            for name in FILENAMES:
                data = json.loads((settings.BASE_DIR / "config" / name).read_text(encoding="utf8"))
                if name == "source_catalog.seed.json":
                    data["collections"].append(
                        {**data["collections"][0], "id": "new-before-failure"}
                    )
                    data["sources"].append(
                        {
                            **data["sources"][0],
                            "id": "bad-source",
                            "profile_id": "missing-profile",
                            "url": "https://example.org/bad",
                        }
                    )
                (path / name).write_text(json.dumps(data), encoding="utf8")
            with self.assertRaises(CommandError):
                call_command("import_seed", directory=path, stdout=StringIO())
        self.assertFalse(Collection.objects.filter(pk="new-before-failure").exists())
        self.assertEqual(Source.objects.count(), 101)

    def test_source_requires_review_and_verified_adapter(self):
        source = Source.objects.filter(kind="website").first()
        source.is_active = True
        with self.assertRaises(ValidationError):
            source.save()
        source.access_review_status = "approved"
        source.collection_check_status = "verified"
        source.access_review_note = "Approved test fixture route"
        source.preferred_adapter = "fixture_reader"
        source.save()
        self.assertTrue(Source.objects.get(pk=source.pk).is_active)

    def test_database_also_rejects_unreviewed_source_activation(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Source.objects.filter(pk=Source.objects.first().pk).update(is_active=True)

    def test_telegram_source_requires_public_url_and_isolated_adapter(self):
        source = Source.objects.filter(kind="telegram").first()
        source.is_active = True
        source.access_review_status = "approved"
        source.collection_check_status = "verified"
        source.access_review_note = "Internal public-channel allowlist; platform risk accepted."
        source.preferred_adapter = "fixture_reader"
        with self.assertRaises(ValidationError):
            source.save()
        source.preferred_adapter = "telethon_public_channel"
        source.save()
        self.assertTrue(Source.objects.get(pk=source.pk).is_active)

    def test_youtube_cannot_be_activated_in_v1(self):
        youtube = Collection.objects.get(pk="youtube")
        youtube.is_active = True
        with self.assertRaises(ValidationError):
            youtube.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Collection.objects.filter(pk="youtube").update(is_active=True)

    def test_duplicate_source_membership_rejected(self):
        existing = SourceCollection.objects.first()
        with self.assertRaises(ValidationError):
            SourceCollection.objects.create(source=existing.source, collection=existing.collection)

    def test_seed_schedules_keep_confirmed_times(self):
        self.assertEqual(Schedule.objects.get(pk="twice_daily").local_times, ["10:00", "19:00"])
        self.assertEqual(Schedule.objects.get(pk="daily").local_times, ["13:00"])
        weekly = Schedule.objects.get(pk="weekly")
        self.assertEqual((weekly.local_times, weekly.iso_weekdays), (["13:00"], [1]))
        self.assertEqual(weekly.timezone, "Europe/Moscow")

    def test_next_weekly_and_daily_have_different_windows(self):
        after = datetime(2026, 9, 13, 12, tzinfo=UTC)
        weekly = Schedule.objects.get(pk="weekly").preview(after=after)[0]
        daily = Schedule.objects.get(pk="daily").preview(after=after)[0]
        self.assertEqual(weekly["scheduled_at"], datetime(2026, 9, 14, 10, tzinfo=UTC))
        self.assertEqual(daily["scheduled_at"], weekly["scheduled_at"])
        self.assertEqual((weekly["window_end"] - weekly["window_start"]).days, 7)
        self.assertEqual((daily["window_end"] - daily["window_start"]).days, 1)

    def test_intraday_preview_boundary_and_no_side_effects(self):
        schedule = Schedule.objects.get(pk="twice_daily")
        rows = schedule.preview(after=datetime(2026, 9, 10, 7, tzinfo=UTC))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["scheduled_at"], datetime(2026, 9, 10, 16, tzinfo=UTC))
        self.assertEqual(rows[0]["window_start"], datetime(2026, 9, 10, 6, 50, tzinfo=UTC))
        self.assertEqual(rows[0]["window_end"], datetime(2026, 9, 10, 15, 50, tzinfo=UTC))
        for left, right in zip(rows, rows[1:]):
            self.assertEqual(left["window_end"], right["window_start"])
        self.assertEqual(ScheduleRevision.objects.count(), 3)

    def test_new_supported_frequency_without_code(self):
        schedule = Schedule.objects.create(
            id="weekend",
            name_ru="Выходные",
            name_en="Weekend",
            recurrence_kind="weekly",
            iso_weekdays=[6, 7],
            local_times=["18:30"],
        )
        rows = schedule.preview(after=datetime(2026, 9, 10, tzinfo=UTC))
        self.assertTrue(all(row["local_time"].isoweekday() in [6, 7] for row in rows))

    def test_invalid_calendar_settings(self):
        for field, value in [
            ("local_times", ["25:00"]),
            ("local_times", ["13:00", "13:00"]),
            ("timezone", "Mars/City"),
            ("recurrence_kind", "monthly"),
        ]:
            with self.subTest(field=field, value=value):
                schedule = Schedule.objects.get(pk="daily")
                setattr(schedule, field, value)
                with self.assertRaises(ValidationError):
                    schedule.save()
        weekly = Schedule.objects.get(pk="weekly")
        weekly.iso_weekdays = []
        with self.assertRaises(ValidationError):
            weekly.save()

    def test_spring_gap_skipped_and_autumn_fold_not_duplicated(self):
        schedule = Schedule(
            id="dst-fixture",
            name_ru="Проверка",
            name_en="Test",
            timezone="Europe/Berlin",
            local_times=["02:30"],
        )
        spring = schedule.preview(count=2, after=datetime(2026, 3, 28, 5, tzinfo=UTC))
        self.assertEqual(spring[0]["local_time"].day, 30)
        autumn = schedule.preview(count=2, after=datetime(2026, 10, 24, 5, tzinfo=UTC))
        self.assertEqual(autumn[0]["scheduled_at"], datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        self.assertEqual(autumn[1]["local_time"].day, 26)

    def test_schedule_revision_history_and_stale_write(self):
        first = Schedule.objects.get(pk="daily")
        stale = Schedule.objects.get(pk="daily")
        first.local_times = ["15:00"]
        first.save()
        self.assertEqual(first.revision, 2)
        self.assertEqual(first.revisions.get(number=1).rule["local_times"], ["13:00"])
        first.save()
        self.assertEqual(first.revisions.count(), 2)
        stale.local_times = ["16:00"]
        with self.assertRaises(ValidationError):
            stale.save()
        old = first.revisions.get(number=1)
        old.rule["local_times"] = ["00:00"]
        with self.assertRaises(ValidationError):
            old.save()

    def test_effective_date_and_aware_datetime_required(self):
        schedule = Schedule.objects.get(pk="daily")
        schedule.effective_from = datetime(2026, 10, 1, 10, tzinfo=UTC)
        self.assertEqual(
            schedule.preview(after=datetime(2026, 9, 10, tzinfo=UTC))[0]["scheduled_at"],
            schedule.effective_from,
        )
        with self.assertRaises(ValueError):
            schedule.preview(after=datetime(2026, 9, 10))

    def schedule_form_data(self, schedule):
        return {
            "id": schedule.pk,
            "name_ru": schedule.name_ru,
            "name_en": schedule.name_en,
            "timezone": schedule.timezone,
            "recurrence_kind": schedule.recurrence_kind,
            "times_text": "14:00",
            "weekdays": [],
            "expected_revision": schedule.revision,
            "preparation_lead_minutes": 10,
            "late_delivery_minutes": 60,
            "sort_order": 20,
        }

    def test_schedule_admin_form_valid_and_invalid_times(self):
        schedule = Schedule.objects.get(pk="daily")
        data = self.schedule_form_data(schedule)
        form = ScheduleForm(data=data, instance=schedule)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Schedule.objects.get(pk="daily").local_times, ["14:00"])
        for bad in ["99:00", "13:00,13:00", ""]:
            instance = Schedule.objects.get(pk="daily")
            data = self.schedule_form_data(instance)
            data["times_text"] = bad
            form = ScheduleForm(data=data, instance=instance)
            self.assertFalse(form.is_valid())
            self.assertIn("times_text", form.errors)

    def test_schedule_admin_stale_form_rejected(self):
        schedule = Schedule.objects.get(pk="daily")
        data = self.schedule_form_data(schedule)
        schedule.local_times = ["17:00"]
        schedule.save()
        form = ScheduleForm(data=data, instance=Schedule.objects.get(pk="daily"))
        self.assertFalse(form.is_valid())

    def test_dashboard_admin_and_preview_pages(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/health/").json()["status"], "ok")
        self.client.force_login(self.owner)
        response = self.client.get("/")
        self.assertContains(response, "Искусственный интеллект")
        self.assertContains(response, "Редакционный конвейер")
        self.assertContains(response, "101")
        for path in [
            "/admin/",
            "/admin/catalog/source/",
            "/admin/catalog/collection/ai/change/",
            "/admin/scheduling/schedule/daily/change/",
            "/admin/scheduling/schedule/add/",
            "/admin/ingestion/publication/",
            "/admin/ingestion/ingestionrun/",
            "/admin/ingestion/sourceproberun/",
            "/admin/editorial/event/",
            "/admin/editorial/eventcardrevision/",
            "/admin/editorial/groupingsuggestion/",
            "/admin/editorial/edition/",
            "/admin/editorial/editionrevision/",
            "/admin/ai_gateway/aibudgetperiod/",
            "/admin/ai_gateway/airequest/",
            "/admin/subscriptions/subscribergeneration/",
            "/admin/subscriptions/preferencerevision/",
            "/admin/delivery/delivery/",
            "/admin/delivery/deliverymanifest/",
            "/admin/delivery/deliverypart/",
            "/admin/delivery/deliveryattempt/",
            "/admin/telegram_bot/telegramupdatereceipt/",
            "/admin/telegram_bot/botreply/",
            "/admin/operations/backuprun/",
            "/admin/operations/restoredrill/",
            "/admin/operations/deliverysafetystate/",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_staff_without_permissions_cannot_read_catalogue(self):
        limited = get_user_model().objects.create_user("limited", is_staff=True)
        self.client.force_login(limited)
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/admin/catalog/source/").status_code, 403)

    def test_admin_can_create_collection_and_preserve_stable_id(self):
        self.client.force_login(self.owner)
        data = {
            "id": "science",
            "name_ru": "Наука",
            "name_en": "Science",
            "kind": "news",
            "release_target": "v1",
            "sort_order": 150,
            "sourcecollection_set-TOTAL_FORMS": "0",
            "sourcecollection_set-INITIAL_FORMS": "0",
            "sourcecollection_set-MIN_NUM_FORMS": "0",
            "sourcecollection_set-MAX_NUM_FORMS": "1000",
        }
        response = self.client.post("/admin/catalog/collection/add/", data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Collection.objects.filter(pk="science").exists())
        data.update(id="changed-id", name_ru="Наука и исследования")
        response = self.client.post("/admin/catalog/collection/science/change/", data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Collection.objects.filter(pk="changed-id").exists())
        self.assertEqual(Collection.objects.get(pk="science").name_ru, "Наука и исследования")

    def test_collection_admin_accepts_one_routing_keyword_per_line(self):
        collection = Collection.objects.get(pk="ai")
        data = {
            "id": collection.pk,
            "name_ru": collection.name_ru,
            "name_en": collection.name_en,
            "description_ru": collection.description_ru,
            "description_en": collection.description_en,
            "kind": collection.kind,
            "release_target": collection.release_target,
            "sort_order": collection.sort_order,
            "routing_keywords_text": "нейросеть\n OpenAI \n\nLLM",
        }
        form = CollectionForm(data=data, instance=collection)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.routing_keywords, ["нейросеть", "OpenAI", "LLM"])

    def test_admin_can_activate_source_without_probe(self):
        self.client.force_login(self.owner)
        source = Source.objects.filter(kind="website").first()
        source.preferred_adapter = ""
        source.preferred_feed_url = ""
        source.access_review_note = ""
        source.save()

        response = self.client.post(
            "/admin/catalog/source/",
            {"action": "activate_selected", "_selected_action": [source.pk]},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        source.refresh_from_db()
        self.assertTrue(source.is_active)
        self.assertEqual(source.preferred_adapter, "rss_then_article_extraction")
        self.assertEqual(source.preferred_feed_url, source.url)
        self.assertFalse(source.probe_runs.exists())
