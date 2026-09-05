"""Configuration loading.

The service list lives in ``services.yaml`` rather than in code so that
adding a service, correcting a Downdetector slug after a site change, or
retuning a threshold needs no code edit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Level

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "services.yaml"


@dataclass
class Thresholds:
    """Downdetector scoring knobs.

    ``min_reports`` is the important one: it is the absolute report count a
    service must exceed before any ratio is trusted. Low-traffic services on
    Downdetector idle at 0-2 reports, where ratios are meaningless.
    """

    min_reports: int = 20
    warning_ratio: float = 2.5
    outage_ratio: float = 5.0
    baseline_floor: float = 1.0


@dataclass
class ServiceConfig:
    key: str
    name: str
    downdetector_url: str | None = None
    official: dict = field(default_factory=lambda: {"kind": "none"})
    thresholds: Thresholds | None = None
    enabled: bool = True


@dataclass
class Config:
    services: list[ServiceConfig]
    defaults: Thresholds = field(default_factory=Thresholds)
    notify_level: Level = Level.WARNING
    reminder_hours: float = 6.0
    notify_recovery: bool = True
    request_timeout: float = 20.0
    #: Seconds to wait between Downdetector requests, to stay polite and to
    #: avoid looking like a burst of automated traffic.
    downdetector_delay: float = 2.0
    summarize: bool = True
    claude_model: str = "claude-opus-5"

    def service(self, key: str) -> ServiceConfig | None:
        return next((s for s in self.services if s.key == key), None)

    def thresholds_for(self, svc: ServiceConfig) -> Thresholds:
        return svc.thresholds or self.defaults


def load(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("DD_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    defaults = Thresholds(**(raw.get("defaults") or {}))

    services: list[ServiceConfig] = []
    for entry in raw.get("services") or []:
        thresholds = entry.get("thresholds")
        services.append(
            ServiceConfig(
                key=entry["key"],
                name=entry.get("name", entry["key"]),
                downdetector_url=entry.get("downdetector_url"),
                official=entry.get("official") or {"kind": "none"},
                thresholds=Thresholds(**thresholds) if thresholds else None,
                enabled=entry.get("enabled", True),
            )
        )
    if not services:
        raise ValueError("config defines no services")

    dupes = {s.key for s in services if [x.key for x in services].count(s.key) > 1}
    if dupes:
        raise ValueError(f"duplicate service keys in config: {sorted(dupes)}")

    notify = raw.get("notify") or {}
    level_name = str(notify.get("level", "WARNING")).upper()
    try:
        notify_level = Level[level_name]
    except KeyError:
        raise ValueError(f"notify.level must be one of OK/WARNING/OUTAGE, got {level_name!r}")

    return Config(
        services=services,
        defaults=defaults,
        notify_level=notify_level,
        reminder_hours=float(notify.get("reminder_hours", 6.0)),
        notify_recovery=bool(notify.get("recovery", True)),
        request_timeout=float(raw.get("request_timeout", 20.0)),
        downdetector_delay=float(raw.get("downdetector_delay", 2.0)),
        summarize=bool((raw.get("summary") or {}).get("enabled", True)),
        claude_model=str((raw.get("summary") or {}).get("model", "claude-opus-5")),
    )
