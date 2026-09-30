"""Extension contract only. No news fetcher or extra scheduler is registered."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class SourceItem:
    source: str
    source_url: str
    published_at: datetime | None
    text: str


@dataclass(frozen=True)
class SourceResult:
    source: str
    source_url: str
    published_at: datetime | None
    fetched_at: datetime
    status: Literal['current', 'stale', 'unavailable', 'disabled']
    items: tuple[SourceItem, ...] = field(default_factory=tuple)


class DataSource(Protocol):
    def fetch(self, now: datetime) -> SourceResult: ...
