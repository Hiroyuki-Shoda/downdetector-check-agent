"""Core data model shared by every source, detector and notifier."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class Level(IntEnum):
    """Severity of a service's health.

    Ordered so that combining signals from several sources is just ``max()``.
    """

    UNKNOWN = -1
    OK = 0
    WARNING = 1  # 障害の兆候 (elevated reports / minor official incident)
    OUTAGE = 2  # 障害 (confirmed spike / major or critical official incident)

    @property
    def label_ja(self) -> str:
        return {
            Level.UNKNOWN: "不明",
            Level.OK: "正常",
            Level.WARNING: "障害の兆候",
            Level.OUTAGE: "障害",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Level.UNKNOWN: "⚪",
            Level.OK: "🟢",
            Level.WARNING: "🟡",
            Level.OUTAGE: "🔴",
        }[self]


@dataclass
class Incident:
    """A single incident published on an official status page."""

    id: str
    title: str
    status: str
    impact: str
    body: str = ""
    url: str = ""
    updated_at: datetime | None = None

    def fingerprint(self) -> str:
        """Identity used for de-duplication.

        Includes ``status`` so that a state change on the same incident
        (``investigating`` -> ``identified`` -> ``resolved``) is treated as
        news worth re-notifying, while repeated polls of an unchanged
        incident are not.
        """
        raw = f"{self.id}|{self.status}|{self.title}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class SourceResult:
    """Outcome of querying one source for one service.

    A source that could not be reached returns ``level=UNKNOWN`` with
    ``error`` set, rather than raising. A single broken source must never
    stop the other services from being checked.
    """

    source: str
    level: Level = Level.UNKNOWN
    url: str = ""
    #: Human-readable one-liner, e.g. "報告数 4,231件 (基準比 18.4倍)".
    detail: str = ""
    #: Structured extras used by the summarizer (report counts, breakdown...).
    data: dict = field(default_factory=dict)
    incidents: list[Incident] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ServiceReport:
    """Merged view of one service across all of its sources."""

    key: str
    name: str
    downdetector: SourceResult | None = None
    official: SourceResult | None = None

    @property
    def sources(self) -> list[SourceResult]:
        return [s for s in (self.downdetector, self.official) if s is not None]

    @property
    def level(self) -> Level:
        """Worst level reported by any source that answered.

        ``UNKNOWN`` sources are ignored so that an unreachable Downdetector
        cannot mask a major incident the official status page is reporting.
        """
        known = [s.level for s in self.sources if s.level != Level.UNKNOWN]
        return max(known) if known else Level.UNKNOWN

    @property
    def incidents(self) -> list[Incident]:
        return [i for s in self.sources for i in s.incidents]

    @property
    def errors(self) -> list[str]:
        return [f"{s.source}: {s.error}" for s in self.sources if s.error]

    def fingerprint(self) -> str:
        """Identity of the published incident set, used for de-duplication.

        Deliberately excludes the level and the report counts. Level
        transitions are handled explicitly by the escalation and recovery
        branches in ``state.decide``, and a service whose report ratio
        hovers around a threshold would otherwise flip its fingerprint —
        and re-notify — on every single cycle.
        """
        parts = sorted(i.fingerprint() for i in self.incidents)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
