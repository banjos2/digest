from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class EvidenceItem:
    membership_id: int
    title: str
    body: str
    source_name: str
    source_url: str
    language: str
    role: str


@dataclass(frozen=True)
class EventBrief:
    event_id: str
    working_title: str
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class ProviderCallMetadata:
    request_id: str
    input_tokens: int
    output_tokens: int
    actual_cost_usd: Decimal


@dataclass(frozen=True)
class RussianCardDraft:
    title: str
    summary: str
    citation_membership_ids: tuple[int, ...]
    metadata: ProviderCallMetadata | None = None


@dataclass(frozen=True)
class EnglishCardDraft:
    title: str
    summary: str
    metadata: ProviderCallMetadata | None = None


class CardDraftProvider(Protocol):
    provider_name: str
    metered: bool

    def model_for(self, operation: str) -> str: ...

    def estimated_max_cost(self, operation: str, input_characters: int) -> Decimal: ...

    def draft_russian(self, event: EventBrief) -> RussianCardDraft: ...

    def translate_english(self, *, title: str, summary: str) -> EnglishCardDraft: ...
