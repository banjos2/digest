import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from telethon.errors import FloodWaitError, RPCError

from .models import Publication, PublicationVersion
from .network import SafeHTTPClient, SourceHTTPError, UnsafeSourceURL
from .parsers import MaterialCandidate, ParseError, extract_article, parse_feed


class IngestionError(RuntimeError):
    def __init__(self, code, *, retry_after_seconds=0):
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class FetchBatch:
    http_status: int
    etag: str
    last_modified: str
    candidates: tuple[MaterialCandidate, ...]
    rejected_count: int = 0
    cursor: str = ""


def validate_source_ready(source):
    if not source.is_active:
        raise IngestionError("source_inactive")
    if (
        source.access_review_status != "approved"
        or source.collection_check_status != "verified"
        or not source.access_review_note.strip()
    ):
        raise IngestionError("source_access_not_approved")
    if source.kind != "website" or source.preferred_adapter != "rss_then_article_extraction":
        raise IngestionError("adapter_not_implemented")
    if not source.preferred_feed_url:
        raise IngestionError("feed_url_missing")


def validate_telegram_source_ready(source):
    if not source.is_active:
        raise IngestionError("source_inactive")
    if (
        source.kind != "telegram"
        or source.preferred_adapter != "telethon_public_channel"
        or source.access_review_status != "approved"
        or source.collection_check_status != "verified"
        or not source.access_review_note.strip()
    ):
        raise IngestionError("source_access_not_approved")


def create_telethon_client():
    if not settings.TELEGRAM_SOURCE_API_ID or not settings.TELEGRAM_SOURCE_API_HASH:
        raise IngestionError("telethon_credentials_missing")
    from telethon import TelegramClient

    return TelegramClient(
        str(settings.TELEGRAM_SOURCE_SESSION_PATH),
        settings.TELEGRAM_SOURCE_API_ID,
        settings.TELEGRAM_SOURCE_API_HASH,
    )


async def connect_telethon_client(client):
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise IngestionError("telethon_session_not_authorized")
    except FloodWaitError as error:
        raise IngestionError(
            "telethon_flood_wait", retry_after_seconds=max(1, int(error.seconds))
        ) from error
    except RPCError as error:
        raise IngestionError(f"telethon_rpc_{error.__class__.__name__.lower()}") from error


async def probe_telegram_source(source, *, client):
    """Resolve an allowlisted public channel and inspect message metadata without storing text."""
    if source.kind != "telegram" or source.preferred_adapter != "telethon_public_channel":
        raise IngestionError("probe_requires_telethon_source")
    try:
        entity = await client.get_entity(source.url)
        item_count = 0
        dated_item_count = 0
        async for message in client.iter_messages(entity, limit=1):
            item_count += 1
            dated_item_count += int(message.date is not None)
        return {
            "http_status": None,
            "final_url": source.url,
            "content_type": "application/x-telegram-channel",
            "response_bytes": 0,
            "item_count": item_count,
            "dated_item_count": dated_item_count,
            "article_attempted_count": 0,
            "article_success_count": 0,
        }
    except FloodWaitError as error:
        raise IngestionError(
            "telethon_flood_wait", retry_after_seconds=max(1, int(error.seconds))
        ) from error
    except RPCError as error:
        raise IngestionError(f"telethon_rpc_{error.__class__.__name__.lower()}") from error
    except IngestionError:
        raise
    except Exception as error:
        raise IngestionError("telethon_probe_failed") from error


async def fetch_telegram_source(source, *, limit, min_id=0, client=None):
    """Collect text posts from one allowlisted public channel through an isolated account."""
    validate_telegram_source_ready(source)
    owns_client = client is None
    if owns_client:
        client = create_telethon_client()
        await connect_telethon_client(client)
    rejected = 0
    candidates = []
    highest_id = int(min_id or 0)
    channel_name = source.url.rstrip("/").rsplit("/", 1)[-1]
    try:
        entity = await client.get_entity(source.url)
        messages = []
        if min_id:
            async for message in client.iter_messages(
                entity, limit=limit, min_id=int(min_id), reverse=True
            ):
                messages.append(message)
            async for message in client.iter_messages(
                entity, limit=settings.TELEGRAM_SOURCE_EDIT_LOOKBACK
            ):
                if int(message.id) <= int(min_id):
                    messages.append(message)
        else:
            async for message in client.iter_messages(entity, limit=limit):
                messages.append(message)
        for message in messages:
            message_id = int(message.id)
            highest_id = max(highest_id, message_id)
            body = (message.message or "").strip()
            if not body:
                rejected += 1
                continue
            first_line = next((line.strip() for line in body.splitlines() if line.strip()), body)
            candidates.append(
                MaterialCandidate(
                    external_id=str(message_id),
                    url=f"https://t.me/{channel_name}/{message_id}",
                    title=first_line[:1000],
                    body=body,
                    published_at=message.date,
                    language=source.input_language,
                    content_scope="telegram_post",
                    metadata={
                        "adapter": "telethon_public_channel",
                        "edit_date": message.edit_date.isoformat()
                        if getattr(message, "edit_date", None)
                        else None,
                        "views": getattr(message, "views", None),
                    },
                )
            )
        return FetchBatch(200, "", "", tuple(candidates), rejected, str(highest_id))
    except FloodWaitError as error:
        raise IngestionError(
            "telethon_flood_wait", retry_after_seconds=max(1, int(error.seconds))
        ) from error
    except RPCError as error:
        raise IngestionError(f"telethon_rpc_{error.__class__.__name__.lower()}") from error
    except IngestionError:
        raise
    except Exception as error:
        raise IngestionError("telethon_collection_failed") from error
    finally:
        if owns_client and client is not None and client.is_connected():
            await client.disconnect()


async def fetch_source(source, *, limit, etag="", last_modified="", client=None):
    validate_source_ready(source)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    owns_client = client is None
    http = client or SafeHTTPClient()
    try:
        feed = await http.fetch(source.preferred_feed_url, headers=headers)
        if feed.status == 304:
            return FetchBatch(304, etag, last_modified, ())
        candidates = parse_feed(
            feed.body,
            feed_url=feed.url,
            language=source.input_language,
            limit=limit,
        )
        enriched = []
        rejected = 0
        for candidate in candidates:
            try:
                article = await http.fetch(candidate.url)
                text = extract_article(article.body, url=article.url)
                enriched.append(candidate.with_article_body(text, article.url))
            except (ParseError, SourceHTTPError, UnsafeSourceURL):
                # A useful feed excerpt remains valid evidence, while metadata states the fallback.
                rejected += 1
                if candidate.content_scope == "feed_title":
                    continue
                enriched.append(
                    MaterialCandidate(
                        **{
                            **candidate.__dict__,
                            "metadata": {
                                **candidate.metadata,
                                "article_extraction": "failed_used_feed_excerpt",
                            },
                        }
                    )
                )
        return FetchBatch(
            feed.status,
            feed.headers.get("ETag", "")[:500],
            feed.headers.get("Last-Modified", "")[:500],
            tuple(enriched),
            rejected,
        )
    except ParseError as error:
        raise IngestionError(str(error)) from error
    except UnsafeSourceURL as error:
        raise IngestionError(str(error)) from error
    except SourceHTTPError as error:
        raise IngestionError(error.code) from error
    finally:
        if owns_client:
            await http.close()


async def probe_source_feed(source, *, limit=3, article_limit=0, client=None):
    """Check a configured feed without activating the source or storing its content."""
    if source.kind != "website":
        raise IngestionError("probe_requires_website")
    if source.preferred_adapter != "rss_then_article_extraction":
        raise IngestionError("adapter_not_implemented")
    if not source.preferred_feed_url:
        raise IngestionError("feed_url_missing")
    owns_client = client is None
    http = client or SafeHTTPClient()
    try:
        document = await http.fetch(source.preferred_feed_url)
        candidates = parse_feed(
            document.body,
            feed_url=document.url,
            language=source.input_language,
            limit=limit,
        )
        attempted = min(article_limit, len(candidates))
        article_successes = 0
        for candidate in candidates[:attempted]:
            try:
                article = await http.fetch(candidate.url)
                extract_article(article.body, url=article.url)
            except (ParseError, SourceHTTPError, UnsafeSourceURL):
                continue
            article_successes += 1
        return {
            "http_status": document.status,
            "final_url": document.url,
            "content_type": document.content_type,
            "response_bytes": len(document.body),
            "item_count": len(candidates),
            "dated_item_count": sum(item.published_at is not None for item in candidates),
            "article_attempted_count": attempted,
            "article_success_count": article_successes,
        }
    except ParseError as error:
        raise IngestionError(str(error)) from error
    except UnsafeSourceURL as error:
        raise IngestionError(str(error)) from error
    except SourceHTTPError as error:
        raise IngestionError(error.code) from error
    finally:
        if owns_client:
            await http.close()


def _content_hash(candidate):
    normalized = json.dumps(
        {
            "title": candidate.title.strip(),
            "body": candidate.body.strip(),
            "language": candidate.language,
            "scope": candidate.content_scope,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


@transaction.atomic
def store_candidate(source, candidate, *, fetched_at=None):
    fetched_at = fetched_at or timezone.now()
    publication, created = Publication.objects.select_for_update().get_or_create(
        source=source,
        external_id=candidate.external_id,
        defaults={
            "canonical_url": candidate.url,
            "title": candidate.title,
            "published_at": candidate.published_at,
            "first_seen_at": fetched_at,
            "last_seen_at": fetched_at,
        },
    )
    if not created:
        publication.canonical_url = candidate.url
        publication.title = candidate.title
        publication.published_at = candidate.published_at or publication.published_at
        publication.last_seen_at = fetched_at
        publication.save()
    digest = _content_hash(candidate)
    existing = publication.versions.filter(content_hash=digest).first()
    if existing:
        return publication, existing, created, False
    last_number = publication.versions.aggregate(value=Max("number"))["value"] or 0
    version = PublicationVersion.objects.create(
        publication=publication,
        number=last_number + 1,
        content_hash=digest,
        title=candidate.title,
        body=candidate.body,
        language=candidate.language,
        content_scope=candidate.content_scope,
        fetched_at=fetched_at,
        expires_at=fetched_at + timedelta(days=settings.SOURCE_TEXT_RETENTION_DAYS),
        extraction_metadata=candidate.metadata,
    )
    return publication, version, created, True
