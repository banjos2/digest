import calendar
import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

import feedparser
import trafilatura


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class MaterialCandidate:
    external_id: str
    url: str
    title: str
    body: str
    published_at: datetime | None
    language: str
    content_scope: str
    metadata: dict

    def with_article_body(self, body, article_url):
        return replace(
            self,
            body=body,
            url=article_url,
            content_scope="full_article",
            metadata={**self.metadata, "article_extraction": "trafilatura"},
        )


def canonicalize_url(url, base_url):
    absolute = urljoin(base_url, url.strip())
    return urldefrag(absolute).url


class _SafeTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def _text_from_html(value):
    if not value:
        return ""
    extracted = trafilatura.html2txt(str(value)) or ""
    if not extracted.strip():
        parser = _SafeTextParser()
        parser.feed(str(value))
        extracted = " ".join(parser.parts)
    return re.sub(r"\s+", " ", extracted).strip()


def _entry_datetime(entry):
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if not value:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)


def parse_feed(body, *, feed_url, language, limit):
    safety_sample = body.lower() if isinstance(body, bytes) else str(body).lower().encode()
    if b"<!doctype" in safety_sample or b"<!entity" in safety_sample:
        raise ParseError("unsafe_xml_declaration")
    parsed = feedparser.parse(body)
    if not parsed.entries:
        raise ParseError("feed_has_no_entries")
    candidates = []
    for entry in parsed.entries[:limit]:
        raw_url = entry.get("link", "").strip()
        if not raw_url:
            continue
        url = canonicalize_url(raw_url, feed_url)
        title = _text_from_html(entry.get("title", ""))[:1000]
        content_values = entry.get("content") or []
        raw_body = content_values[0].get("value", "") if content_values else ""
        raw_body = raw_body or entry.get("summary", "") or entry.get("description", "")
        excerpt = _text_from_html(raw_body)
        if not title:
            continue
        title_only = not excerpt
        excerpt = excerpt or title
        external_id = str(entry.get("id") or entry.get("guid") or url).strip()
        if len(external_id) > 1000:
            external_id = hashlib.sha256(external_id.encode()).hexdigest()
        candidates.append(
            MaterialCandidate(
                external_id=external_id,
                url=url,
                title=title,
                body=excerpt,
                published_at=_entry_datetime(entry),
                language=language,
                content_scope="feed_title" if title_only else "feed_excerpt",
                metadata={
                    "feed_url": feed_url,
                    "feed_content": "title_only" if title_only else "excerpt",
                },
            )
        )
    if not candidates:
        raise ParseError("feed_has_no_usable_entries")
    return candidates


def extract_article(body, *, url):
    text = trafilatura.extract(
        body,
        url=url,
        include_comments=False,
        include_tables=False,
        output_format="txt",
    )
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) < 80:
        raise ParseError("article_text_too_short")
    return text
