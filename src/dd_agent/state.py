"""Notification state — the thing that stops Slack from being spammed.

The agent runs every few minutes, but an outage lasts hours. Without state
every run would re-post the same incident. This module records what was
last announced per service and decides whether the current observation is
*news*.

An observation is news when:

* the service just became degraded (``new``)
* it got worse, e.g. 兆候 -> 障害 (``escalation``)
* the incident set changed — a new incident, or a status change on an
  existing one such as investigating -> identified (``update``)
* it recovered (``recovery``)
* it is still broken and the reminder interval has elapsed (``reminder``)

Anything else is silence.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Level, ServiceReport

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class ServiceState:
    level: int = int(Level.OK)
    fingerprint: str = ""
    #: When the service first became degraded in this episode.
    since: str = ""
    #: When we last posted to Slack about it.
    notified_at: str = ""


@dataclass
class State:
    version: int = STATE_VERSION
    services: dict[str, ServiceState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> State:
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("state file %s unreadable (%s); starting fresh", path, exc)
            return cls()
        if raw.get("version") != STATE_VERSION:
            log.info("state version mismatch; starting fresh")
            return cls()
        services = {
            key: ServiceState(**{k: v for k, v in val.items() if k in ServiceState.__annotations__})
            for key, val in (raw.get("services") or {}).items()
            if isinstance(val, dict)
        }
        return cls(version=STATE_VERSION, services=services)

    def save(self, path: Path | str) -> None:
        """Write atomically, so an interrupted run cannot corrupt the file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "services": {k: asdict(v) for k, v in self.services.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


@dataclass
class Decision:
    notify: bool
    kind: str  # new | escalation | update | recovery | reminder | none
    reason: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decide(
    report: ServiceReport,
    state: State,
    *,
    notify_level: Level = Level.WARNING,
    reminder_hours: float = 6.0,
    notify_recovery: bool = True,
    now: datetime | None = None,
) -> Decision:
    """Decide whether ``report`` should be announced, given prior ``state``."""
    now = now or _now()
    prev = state.services.get(report.key)
    prev_level = Level(prev.level) if prev else Level.OK
    level = report.level

    # UNKNOWN means every source failed. That is an agent problem, not a
    # service problem: never announce it as an outage and never let it clear
    # an ongoing one.
    if level == Level.UNKNOWN:
        return Decision(False, "none", "all sources unknown")

    degraded = level >= notify_level
    was_degraded = prev_level >= notify_level

    if degraded and not was_degraded:
        return Decision(True, "new", f"{prev_level.name} -> {level.name}")

    if degraded and was_degraded:
        if level > prev_level:
            return Decision(True, "escalation", f"{prev_level.name} -> {level.name}")
        fingerprint = report.fingerprint()
        if prev and fingerprint != prev.fingerprint:
            return Decision(True, "update", "incident details changed")
        last = _parse(prev.notified_at) if prev else None
        if last is None or now - last >= timedelta(hours=reminder_hours):
            return Decision(True, "reminder", f"still degraded after {reminder_hours}h")
        return Decision(False, "none", "unchanged since last notification")

    if was_degraded and not degraded:
        if notify_recovery:
            return Decision(True, "recovery", f"{prev_level.name} -> {level.name}")
        return Decision(False, "none", "recovered, recovery notices disabled")

    return Decision(False, "none", "healthy")


def record(
    report: ServiceReport,
    state: State,
    decision: Decision,
    *,
    notify_level: Level = Level.WARNING,
    now: datetime | None = None,
) -> None:
    """Fold the outcome of this run back into ``state``."""
    now = now or _now()
    if report.level == Level.UNKNOWN:
        return  # keep the previous known state rather than overwriting it

    prev = state.services.get(report.key)
    entry = ServiceState(
        level=int(report.level),
        fingerprint=report.fingerprint(),
        since=prev.since if prev else "",
        notified_at=prev.notified_at if prev else "",
    )
    if report.level >= notify_level:
        if not entry.since or (prev and Level(prev.level) < notify_level):
            entry.since = now.isoformat()
    else:
        entry.since = ""
    if decision.notify:
        entry.notified_at = now.isoformat()
    state.services[report.key] = entry
