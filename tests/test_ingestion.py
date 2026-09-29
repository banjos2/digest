import asyncio
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from digest_service.catalog.models import Source
from digest_service.ingestion.models import (
    IngestionRun,
    Publication,
    PublicationVersion,
    SourceProbeRun,
)
from digest_service.ingestion.network import (
    HTTPDocument,
    UnsafeSourceURL,
    validate_resolved_addresses,
    validate_source_url,
)
from digest_service.ingestion.parsers import MaterialCandidate, extract_article, parse_feed
from digest_service.ingestion.service import (
    FetchBatch,
    fetch_source,
    fetch_telegram_source,
    probe_source_feed,
    probe_telegram_source,
    store_candidate,
)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fixture</title>
<item><guid>news-1</guid><title>First &amp; useful</title>
<link>https://example.com/news/1#fragment</link>
<pubDate>Thu, 10 Sep 2026 08:00:00 GMT</pubDate>
<description><![CDATA[<p>A short <strong>feed</strong> excerpt.</p><script>bad()</script>]]></description>
</item>
<item><guid>news-2</guid><title>Second item</title>
<link>/news/2</link><description>A second excerpt.</description></item>
</channel></rss>"""

ARTICLE = b"""<!doctype html><html><head><title>Fixture</title></head><body>
<nav>Navigation links</nav><main><article><h1>First useful story</h1>
<p>This is the main article text with enough detail for the extraction threshold.</p>
<p>It provides a second sentence with context and a verifiable description.</p>
</article></main><footer>Footer</footer></body></html>"""

TITLE_ONLY_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fixture</title><item><guid>title-1</guid>
<title>Title without an excerpt</title><link>https://example.com/title-1</link>
<pubDate>Thu, 10 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>"""


class FakeHTTPClient:
    def __init__(self, documents):
        self.documents = documents
        self.requests = []

    async def fetch(self, url, *, headers=None):
        self.requests.append((url, headers or {}))
        value = self.documents[url]
        if isinstance(value, Exception):
            raise value
        return value


class FakeTelethonClient:
    def __init__(self, messages):
        self.messages = messages
        self.requests = []
        self.connected = True

    async def get_entity(self, value):
        return SimpleNamespace(username=value.rstrip("/").rsplit("/", 1)[-1])

    def iter_messages(self, entity, *, limit, min_id=0, reverse=False):
        self.requests.append((entity.username, limit, min_id, reverse))

        async def rows():
            selected = [message for message in self.messages if message.id > min_id]
            if reverse:
                selected = list(reversed(selected))
            for message in selected[:limit]:
                yield message

        return rows()

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False


class ParserAndNetworkTests(SimpleTestCase):
    def test_only_public_http_urls_are_accepted(self):
        validate_source_url("https://example.com/world?q=1")
        validate_source_url("http://8.8.8.8/feed")
        for url in [
            "file:///etc/passwd",
            "http://localhost/feed",
            "http://127.0.0.1/feed",
            "http://[::1]/feed",
            "https://user:secret@example.com/feed",
            "https://example.com:8080/feed",
        ]:
            with self.subTest(url=url), self.assertRaises(UnsafeSourceURL):
                validate_source_url(url)
        with self.assertRaises(UnsafeSourceURL):
            validate_resolved_addresses(["93.184.216.34", "10.0.0.8"])

    def test_feed_parse_canonicalizes_and_sanitizes(self):
        candidates = parse_feed(
            FEED, feed_url="https://example.com/feed.xml", language="en", limit=10
        )
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].url, "https://example.com/news/1")
        self.assertEqual(candidates[1].url, "https://example.com/news/2")
        self.assertNotIn("script", candidates[0].body.lower())
        self.assertEqual(candidates[0].published_at, datetime(2026, 9, 10, 8, tzinfo=UTC))

    def test_article_extraction(self):
        text = extract_article(ARTICLE, url="https://example.com/news/1")
        self.assertIn("main article text", text)
        self.assertNotIn("Navigation links", text)

    def test_title_only_feed_entry_is_available_for_article_enrichment(self):
        candidates = parse_feed(
            TITLE_ONLY_FEED,
            feed_url="https://example.com/feed.xml",
            language="en",
            limit=1,
        )
        self.assertEqual(candidates[0].content_scope, "feed_title")
        self.assertEqual(candidates[0].body, "Title without an excerpt")


class IngestionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_seed", stdout=StringIO())

    def ready_source(self):
        source = Source.objects.filter(
            kind="website", preferred_adapter="rss_then_article_extraction"
        ).first()
        source.is_active = True
        source.access_review_status = "approved"
        source.access_review_note = "Test fixture is explicitly allowed."
        source.collection_check_status = "verified"
        source.preferred_feed_url = "https://example.com/feed.xml"
        source.save()
        return source

    def candidate(self, *, body="The publication body has enough stable content."):
        return MaterialCandidate(
            external_id="item-1",
            url="https://example.com/item-1",
            title="A title",
            body=body,
            published_at=datetime(2026, 9, 10, 8, tzinfo=UTC),
            language="en",
            content_scope="full_article",
            metadata={"fixture": True},
        )

    def ready_telegram_source(self):
        source = Source.objects.filter(kind="telegram").first()
        source.is_active = True
        source.access_review_status = "approved"
        source.access_review_note = "Internal allowlist fixture."
        source.collection_check_status = "verified"
        source.preferred_adapter = "telethon_public_channel"
        source.save()
        return source

    def test_telethon_fetch_uses_cursor_and_returns_text_posts_only(self):
        source = self.ready_telegram_source()
        posted_at = datetime(2026, 9, 11, 8, tzinfo=UTC)
        fake = FakeTelethonClient(
            [
                SimpleNamespace(
                    id=103, message="Headline\nDetails", date=posted_at, edit_date=None, views=10
                ),
                SimpleNamespace(id=102, message="", date=posted_at, edit_date=None, views=5),
                SimpleNamespace(id=100, message="Old", date=posted_at, edit_date=None, views=1),
            ]
        )
        batch = asyncio.run(fetch_telegram_source(source, limit=10, min_id=101, client=fake))
        channel_name = source.url.rstrip("/").rsplit("/", 1)[-1]
        self.assertEqual(
            fake.requests,
            [(channel_name, 10, 101, True), (channel_name, 20, 0, False)],
        )
        self.assertEqual(len(batch.candidates), 2)
        self.assertEqual(batch.candidates[0].external_id, "103")
        self.assertEqual(batch.candidates[0].content_scope, "telegram_post")
        self.assertEqual(batch.candidates[0].url, f"{source.url}/103")
        self.assertEqual(batch.candidates[1].external_id, "100")
        self.assertEqual(batch.rejected_count, 1)
        self.assertEqual(batch.cursor, "103")

    def test_telethon_probe_reads_metadata_without_storing_publication(self):
        source = Source.objects.filter(kind="telegram").first()
        posted_at = datetime(2026, 9, 11, 8, tzinfo=UTC)
        fake = FakeTelethonClient(
            [SimpleNamespace(id=103, message="Secret text", date=posted_at)]
        )
        result = asyncio.run(probe_telegram_source(source, client=fake))
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(result["dated_item_count"], 1)
        self.assertEqual(result["response_bytes"], 0)
        self.assertEqual(Publication.objects.count(), 0)

    def test_telethon_probe_command_records_only_diagnostics(self):
        source = Source.objects.filter(kind="telegram").first()
        fake = FakeTelethonClient(
            [
                SimpleNamespace(
                    id=103,
                    message="Not persisted",
                    date=datetime(2026, 9, 11, 8, tzinfo=UTC),
                )
            ]
        )
        command_path = (
            "digest_service.ingestion.management.commands.probe_telegram_sources"
        )
        with (
            patch(f"{command_path}.create_telethon_client", return_value=fake),
            patch(f"{command_path}.connect_telethon_client", new=AsyncMock()),
        ):
            call_command(
                "probe_telegram_sources",
                source_ids=[source.pk],
                stdout=StringIO(),
            )
        probe = SourceProbeRun.objects.get()
        self.assertEqual((probe.status, probe.item_count), ("success", 1))
        self.assertEqual(Publication.objects.count(), 0)

    def test_version_lifetime_is_not_extended_by_unchanged_fetch(self):
        source = self.ready_source()
        first_time = datetime(2026, 9, 10, 9, tzinfo=UTC)
        publication, first, created, new_version = store_candidate(
            source, self.candidate(), fetched_at=first_time
        )
        self.assertTrue(created)
        self.assertTrue(new_version)
        self.assertEqual(first.expires_at, first_time + timedelta(days=90))
        _, same, created, new_version = store_candidate(
            source, self.candidate(), fetched_at=first_time + timedelta(days=5)
        )
        self.assertFalse(created)
        self.assertFalse(new_version)
        self.assertEqual(same.expires_at, first.expires_at)
        _, second, _, new_version = store_candidate(
            source,
            self.candidate(body="The publisher changed this body and added substantial detail."),
            fetched_at=first_time + timedelta(days=6),
        )
        self.assertTrue(new_version)
        self.assertEqual(second.number, 2)
        self.assertEqual(publication.versions.count(), 2)

    def test_expired_text_is_purged_without_deleting_reference(self):
        source = self.ready_source()
        fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
        _, version, _, _ = store_candidate(source, self.candidate(), fetched_at=fetched_at)
        with patch(
            "digest_service.ingestion.management.commands.purge_expired_source_text.timezone.now",
            return_value=fetched_at + timedelta(days=91),
        ):
            call_command("purge_expired_source_text", stdout=StringIO())
        version.refresh_from_db()
        self.assertEqual(version.body, "")
        self.assertEqual(version.purge_reason, "retention_expired")
        self.assertTrue(Publication.objects.filter(pk=version.publication_id).exists())

    def test_fetch_enriches_article_and_sends_conditional_headers(self):
        source = self.ready_source()
        fake = FakeHTTPClient(
            {
                source.preferred_feed_url: HTTPDocument(
                    source.preferred_feed_url,
                    200,
                    {"ETag": '"v1"', "Last-Modified": "Thu, 10 Sep 2026 08:05:00 GMT"},
                    FEED,
                    "application/rss+xml",
                ),
                "https://example.com/news/1": HTTPDocument(
                    "https://example.com/news/1", 200, {}, ARTICLE, "text/html"
                ),
                "https://example.com/news/2": HTTPDocument(
                    "https://example.com/news/2", 200, {}, ARTICLE, "text/html"
                ),
            }
        )
        batch = asyncio.run(
            fetch_source(source, limit=2, etag='"old"', last_modified="yesterday", client=fake)
        )
        self.assertEqual(batch.etag, '"v1"')
        self.assertEqual(batch.rejected_count, 0)
        self.assertTrue(all(item.content_scope == "full_article" for item in batch.candidates))
        self.assertEqual(
            fake.requests[0][1],
            {"If-None-Match": '"old"', "If-Modified-Since": "yesterday"},
        )

    def test_probe_checks_inactive_feed_without_storing_publications(self):
        source = Source.objects.filter(
            kind="website", preferred_adapter="rss_then_article_extraction"
        ).first()
        self.assertFalse(source.is_active)
        fake = FakeHTTPClient(
            {
                source.preferred_feed_url: HTTPDocument(
                    source.preferred_feed_url,
                    200,
                    {},
                    FEED,
                    "application/rss+xml",
                ),
                "https://example.com/news/1": HTTPDocument(
                    "https://example.com/news/1", 200, {}, ARTICLE, "text/html"
                ),
            }
        )
        result = asyncio.run(
            probe_source_feed(source, limit=2, article_limit=1, client=fake)
        )
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(result["dated_item_count"], 1)
        self.assertEqual(result["article_attempted_count"], 1)
        self.assertEqual(result["article_success_count"], 1)
        self.assertEqual(Publication.objects.count(), 0)
        self.assertEqual(SourceProbeRun.objects.count(), 0)

    def test_management_command_records_success_and_refuses_inactive_source(self):
        source = self.ready_source()
        batch = FetchBatch(200, '"v1"', "now", (self.candidate(),))
        with patch(
            "digest_service.ingestion.management.commands.collect_source.fetch_source",
            return_value=batch,
        ):
            call_command("collect_source", source.pk, stdout=StringIO())
        run = IngestionRun.objects.get()
        self.assertEqual((run.status, run.created_count, run.new_version_count), ("success", 1, 1))
        self.assertEqual(Publication.objects.count(), 1)
        self.assertEqual(PublicationVersion.objects.count(), 1)

        source.is_active = False
        source.save()
        with self.assertRaises(CommandError):
            call_command("collect_source", source.pk, stdout=StringIO(), stderr=StringIO())
        self.assertEqual(IngestionRun.objects.filter(status="failed").count(), 1)
