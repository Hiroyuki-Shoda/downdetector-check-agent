"""Run one check cycle over every configured service."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import notify, state as state_mod, summarize as summarize_mod
from .config import Config, ServiceConfig
from .models import Level, ServiceReport
from .sources import downdetector, official

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    report: ServiceReport
    decision: state_mod.Decision
    summary: str | None = None
    delivered: bool = False
    delivery_error: str | None = None


def collect(
    cfg: Config,
    svc: ServiceConfig,
    *,
    return_html: bool = False,
) -> ServiceReport:
    """Query every source for one service. Never raises."""
    report = ServiceReport(key=svc.key, name=svc.name)

    if svc.downdetector_url:
        th = cfg.thresholds_for(svc)
        report.downdetector = downdetector.check(
            svc.key,
            svc.name,
            svc.downdetector_url,
            min_reports=th.min_reports,
            warning_ratio=th.warning_ratio,
            outage_ratio=th.outage_ratio,
            baseline_floor=th.baseline_floor,
            timeout=cfg.request_timeout,
            return_html=return_html,
        )

    report.official = official.check(svc.official, timeout=cfg.request_timeout)
    return report


def run(
    cfg: Config,
    *,
    state: state_mod.State,
    webhook_url: str | None,
    only: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    now=None,
) -> list[Outcome]:
    """Check all services, notify on anything that is news, update state.

    ``force`` bypasses de-duplication so a degraded service is announced
    even if it was announced already — used by ``--force`` for testing the
    Slack wiring end to end.
    """
    outcomes: list[Outcome] = []
    services = [s for s in cfg.services if s.enabled]
    if only:
        wanted = set(only)
        services = [s for s in services if s.key in wanted]
        missing = wanted - {s.key for s in services}
        if missing:
            raise ValueError(f"unknown service key(s): {sorted(missing)}")

    for index, svc in enumerate(services):
        if index and svc.downdetector_url and cfg.downdetector_delay:
            time.sleep(cfg.downdetector_delay)

        report = collect(cfg, svc)
        for err in report.errors:
            log.warning("[%s] %s", svc.key, err)

        decision = state_mod.decide(
            report,
            state,
            notify_level=cfg.notify_level,
            reminder_hours=cfg.reminder_hours,
            notify_recovery=cfg.notify_recovery,
            now=now,
        )
        if force and report.level >= cfg.notify_level and not decision.notify:
            decision = state_mod.Decision(True, "reminder", "forced")

        outcome = Outcome(report=report, decision=decision)

        if decision.notify:
            if cfg.summarize and decision.kind != "recovery":
                outcome.summary = summarize_mod.summarize(report, model=cfg.claude_model)
            payload = notify.build_payload(
                report, kind=decision.kind, summary=outcome.summary, now=now
            )
            if dry_run:
                outcome.delivered = False
            else:
                try:
                    notify.post(webhook_url or "", payload)
                    outcome.delivered = True
                except notify.SlackError as exc:
                    outcome.delivery_error = str(exc)
                    log.error("[%s] Slack delivery failed: %s", svc.key, exc)

        # Only advance state once the message is actually out, otherwise a
        # Slack outage would silently swallow the one alert that mattered.
        if not decision.notify or outcome.delivered:
            state_mod.record(report, state, decision, notify_level=cfg.notify_level, now=now)

        outcomes.append(outcome)
        log.info(
            "[%s] level=%s decision=%s (%s)",
            svc.key,
            report.level.name,
            decision.kind,
            decision.reason,
        )

    return outcomes


def summarise_run(outcomes: list[Outcome]) -> str:
    """One-line-per-service digest for logs and the GitHub Actions summary."""
    lines = []
    for o in outcomes:
        r = o.report
        flag = "sent" if o.delivered else ("dry-run" if o.decision.notify else "-")
        if o.delivery_error:
            flag = "FAILED"
        lines.append(
            f"{r.level.emoji} {r.name:<12} {r.level.label_ja:<8} "
            f"{o.decision.kind:<10} {flag:<8} {'; '.join(r.errors)}"
        )
    return "\n".join(lines)


def exit_code(outcomes: list[Outcome]) -> int:
    """0 = clean run. 1 = a notification could not be delivered.

    A detected outage is *not* a failure exit — the agent did its job. Only
    agent-level failures (Slack unreachable) should turn the CI run red.
    """
    return 1 if any(o.delivery_error for o in outcomes) else 0


def has_level(outcomes: list[Outcome], level: Level) -> bool:
    return any(o.report.level >= level for o in outcomes)
