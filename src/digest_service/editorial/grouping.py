import re
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from django.utils import timezone

from digest_service.ingestion.models import PublicationVersion

from .models import GroupingSuggestion

TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
STOPWORDS = {
    "and",
    "for",
    "from",
    "into",
    "the",
    "this",
    "with",
    "без",
    "для",
    "как",
    "что",
    "это",
}
GENERIC_LATIN_ANCHORS = {
    "china",
    "europe",
    "eu",
    "russia",
    "uk",
    "ukraine",
    "us",
    "usa",
}
RUSSIAN_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "алась",
    "илась",
    "ываются",
    "иваются",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "иях",
    "ах",
    "ях",
    "ую",
    "юю",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ов",
    "ев",
    "ей",
    "ой",
    "ий",
    "ый",
    "ам",
    "ям",
    "ом",
    "ем",
    "ым",
    "им",
    "лась",
    "лся",
    "ли",
    "ла",
)


def normalize_token(token):
    value = token.casefold().replace("ё", "е")
    if re.search("[а-я]", value) and len(value) >= 6:
        for suffix in RUSSIAN_SUFFIXES:
            if value.endswith(suffix) and len(value) - len(suffix) >= 4:
                return value[: -len(suffix)]
    return value


def title_tokens(value):
    return {
        normalized
        for token in TOKEN_PATTERN.findall(value.replace("Open AI", "OpenAI"))
        if len(token) >= 3
        and (normalized := normalize_token(token)) not in STOPWORDS
    }


def title_anchors(value):
    anchors = set()
    for token in TOKEN_PATTERN.findall(value.replace("Open AI", "OpenAI")):
        if len(token) < 3:
            continue
        if token.isupper() or token[:1].isupper() or (not token.isalpha() and token.isalnum()):
            anchors.add(normalize_token(token))
    return anchors


def lexical_similarity(first_title, second_title):
    first_tokens = title_tokens(first_title)
    second_tokens = title_tokens(second_title)
    if not first_tokens or not second_tokens:
        return 0.0, 0.0, 0.0, 0.0
    jaccard = len(first_tokens & second_tokens) / len(first_tokens | second_tokens)
    overlap = len(first_tokens & second_tokens) / min(len(first_tokens), len(second_tokens))
    sequence = SequenceMatcher(None, first_title.casefold(), second_title.casefold()).ratio()
    score = 0.45 * jaccard + 0.35 * overlap + 0.2 * sequence
    shared_anchors = title_anchors(first_title) & title_anchors(second_title)
    common_tokens = len(first_tokens & second_tokens)
    has_specific_latin_anchor = any(
        re.search("[a-z0-9]", anchor) and anchor not in GENERIC_LATIN_ANCHORS
        for anchor in shared_anchors
    )
    if common_tokens >= 2 and (len(shared_anchors) >= 2 or has_specific_latin_anchor):
        score = max(score, min(0.85, 0.42 + 0.11 * common_tokens + 0.08 * overlap))
    return score, jaccard, overlap, sequence


def suggest_groupings(*, since=None, limit=500, threshold=0.55):
    since = since or timezone.now() - timedelta(hours=48)
    versions = list(
        PublicationVersion.objects.filter(
            purged_at__isnull=True,
            publication__published_at__gte=since,
        )
        .select_related("publication__source__origin_group")
        .order_by("-fetched_at")[:limit]
    )
    latest_by_publication = {}
    for version in versions:
        latest_by_publication.setdefault(version.publication_id, version)
    versions = list(latest_by_publication.values())
    created = []
    for index, first in enumerate(versions):
        for second in versions[index + 1 :]:
            if first.publication.source_id == second.publication.source_id:
                continue
            score, jaccard, overlap, sequence = lexical_similarity(first.title, second.title)
            if score < threshold:
                continue
            left, right = sorted([first, second], key=lambda item: item.pk)
            suggestion, was_created = GroupingSuggestion.objects.get_or_create(
                first_version=left,
                second_version=right,
                algorithm_version="lexical-v2",
                defaults={
                    "score": Decimal(str(round(score, 4))),
                    "reasons": {
                        "title_jaccard": round(jaccard, 4),
                        "title_overlap": round(overlap, 4),
                        "title_sequence": round(sequence, 4),
                        "shared_anchors": sorted(title_anchors(first.title) & title_anchors(second.title)),
                        "same_origin_group": (
                            first.publication.source.origin_group_id
                            == second.publication.source.origin_group_id
                        ),
                        "decision": "editor_review_required",
                    },
                },
            )
            if was_created:
                created.append(suggestion)
    return created
