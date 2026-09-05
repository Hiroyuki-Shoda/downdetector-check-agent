"""De-duplication tests.

This is the logic that decides whether Slack hears about an observation.
Getting it wrong means either alert spam every few minutes or a missed
outage, so each transition is pinned explicitly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dd_agent import state as st
from dd_agent.models import Incident, Level, ServiceReport, SourceResult

T0 = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def report(level: Level, incidents: list[Incident] | None = None) -> ServiceReport:
    return ServiceReport(
        key="chatgpt",
        name="ChatGPT",
        official=SourceResult(source="official", level=level, incidents=incidents or []),
    )


def incident(id_: str = "i1", status: str = "investigating") -> Incident:
    return Incident(id=id_, title="Elevated errors", status=status, impact="major")


class TestFirstDetection:
    def test_healthy_stays_silent(self):
        d = st.decide(report(Level.OK), st.State(), now=T0)
        assert not d.notify

    def test_new_warning_notifies(self):
        d = st.decide(report(Level.WARNING), st.State(), now=T0)
        assert d.notify and d.kind == "new"

    def test_new_outage_notifies(self):
        d = st.decide(report(Level.OUTAGE), st.State(), now=T0)
        assert d.notify and d.kind == "new"

    def test_warning_suppressed_when_threshold_is_outage(self):
        d = st.decide(report(Level.WARNING), st.State(), notify_level=Level.OUTAGE, now=T0)
        assert not d.notify


class TestOngoing:
    def _state_at(self, level: Level, report_obj: ServiceReport, when: datetime) -> st.State:
        state = st.State()
        state.services["chatgpt"] = st.ServiceState(
            level=int(level),
            fingerprint=report_obj.fingerprint(),
            since=when.isoformat(),
            notified_at=when.isoformat(),
        )
        return state

    def test_unchanged_is_silent(self):
        r = report(Level.OUTAGE, [incident()])
        state = self._state_at(Level.OUTAGE, r, T0)
        d = st.decide(r, state, now=T0 + timedelta(minutes=10))
        assert not d.notify
        assert d.reason == "unchanged since last notification"

    def test_escalation_notifies(self):
        warned = report(Level.WARNING)
        state = self._state_at(Level.WARNING, warned, T0)
        d = st.decide(report(Level.OUTAGE), state, now=T0 + timedelta(minutes=10))
        assert d.notify and d.kind == "escalation"

    def test_de_escalation_within_degraded_range_is_silent(self):
        # OUTAGE -> WARNING is still degraded. Announcing a partial
        # improvement every cycle is noise; the recovery notice covers it.
        out = report(Level.OUTAGE)
        state = self._state_at(Level.OUTAGE, out, T0)
        d = st.decide(report(Level.WARNING), state, now=T0 + timedelta(minutes=10))
        assert not d.notify

    def test_incident_status_change_notifies(self):
        before = report(Level.OUTAGE, [incident(status="investigating")])
        state = self._state_at(Level.OUTAGE, before, T0)
        after = report(Level.OUTAGE, [incident(status="identified")])
        d = st.decide(after, state, now=T0 + timedelta(minutes=10))
        assert d.notify and d.kind == "update"

    def test_additional_incident_notifies(self):
        before = report(Level.OUTAGE, [incident("i1")])
        state = self._state_at(Level.OUTAGE, before, T0)
        after = report(Level.OUTAGE, [incident("i1"), incident("i2")])
        d = st.decide(after, state, now=T0 + timedelta(minutes=10))
        assert d.notify and d.kind == "update"

    def test_reminder_after_interval(self):
        r = report(Level.OUTAGE, [incident()])
        state = self._state_at(Level.OUTAGE, r, T0)
        d = st.decide(r, state, reminder_hours=6, now=T0 + timedelta(hours=6, minutes=1))
        assert d.notify and d.kind == "reminder"

    def test_no_reminder_before_interval(self):
        r = report(Level.OUTAGE, [incident()])
        state = self._state_at(Level.OUTAGE, r, T0)
        d = st.decide(r, state, reminder_hours=6, now=T0 + timedelta(hours=5))
        assert not d.notify


class TestRecovery:
    def test_recovery_notifies(self):
        state = st.State()
        state.services["chatgpt"] = st.ServiceState(
            level=int(Level.OUTAGE), fingerprint="x", since=T0.isoformat(), notified_at=T0.isoformat()
        )
        d = st.decide(report(Level.OK), state, now=T0 + timedelta(hours=1))
        assert d.notify and d.kind == "recovery"

    def test_recovery_can_be_disabled(self):
        state = st.State()
        state.services["chatgpt"] = st.ServiceState(level=int(Level.OUTAGE), fingerprint="x")
        d = st.decide(report(Level.OK), state, notify_recovery=False, now=T0)
        assert not d.notify


class TestUnknown:
    """All sources failing is an agent fault, not a service outage."""

    def test_unknown_never_notifies(self):
        d = st.decide(report(Level.UNKNOWN), st.State(), now=T0)
        assert not d.notify
        assert d.reason == "all sources unknown"

    def test_unknown_does_not_clear_an_ongoing_outage(self):
        state = st.State()
        state.services["chatgpt"] = st.ServiceState(
            level=int(Level.OUTAGE), fingerprint="x", since=T0.isoformat(), notified_at=T0.isoformat()
        )
        d = st.decide(report(Level.UNKNOWN), state, now=T0 + timedelta(minutes=5))
        assert not d.notify
        st.record(report(Level.UNKNOWN), state, d, now=T0 + timedelta(minutes=5))
        # The recorded state must still say OUTAGE, so that a later genuine
        # recovery still produces a recovery notice.
        assert state.services["chatgpt"].level == int(Level.OUTAGE)


class TestRecord:
    def test_records_level_and_since(self):
        state = st.State()
        r = report(Level.OUTAGE, [incident()])
        d = st.decide(r, state, now=T0)
        st.record(r, state, d, now=T0)
        entry = state.services["chatgpt"]
        assert entry.level == int(Level.OUTAGE)
        assert entry.since == T0.isoformat()
        assert entry.notified_at == T0.isoformat()

    def test_since_is_preserved_across_ongoing_outage(self):
        state = st.State()
        r = report(Level.OUTAGE, [incident()])
        st.record(r, state, st.decide(r, state, now=T0), now=T0)
        later = T0 + timedelta(hours=2)
        st.record(r, state, st.Decision(False, "none"), now=later)
        assert state.services["chatgpt"].since == T0.isoformat()

    def test_since_cleared_on_recovery(self):
        state = st.State()
        state.services["chatgpt"] = st.ServiceState(
            level=int(Level.OUTAGE), fingerprint="x", since=T0.isoformat()
        )
        ok = report(Level.OK)
        st.record(ok, state, st.Decision(True, "recovery"), now=T0 + timedelta(hours=1))
        assert state.services["chatgpt"].since == ""

    def test_notified_at_not_advanced_when_silent(self):
        state = st.State()
        state.services["chatgpt"] = st.ServiceState(
            level=int(Level.OUTAGE), fingerprint="x", notified_at=T0.isoformat()
        )
        r = report(Level.OUTAGE)
        st.record(r, state, st.Decision(False, "none"), now=T0 + timedelta(hours=3))
        assert state.services["chatgpt"].notified_at == T0.isoformat()


class TestPersistence:
    def test_round_trip(self, tmp_path):
        state = st.State()
        state.services["x"] = st.ServiceState(level=2, fingerprint="abc", since=T0.isoformat())
        path = tmp_path / "state.json"
        state.save(path)
        loaded = st.State.load(path)
        assert loaded.services["x"].fingerprint == "abc"
        assert loaded.services["x"].level == 2

    def test_missing_file_is_empty_state(self, tmp_path):
        assert st.State.load(tmp_path / "nope.json").services == {}

    def test_corrupt_file_does_not_crash(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        assert st.State.load(path).services == {}

    def test_version_mismatch_starts_fresh(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"version": 99, "services": {"x": {"level": 2}}}', encoding="utf-8")
        assert st.State.load(path).services == {}

    def test_unknown_keys_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            '{"version": 1, "services": {"x": {"level": 1, "bogus": true}}}', encoding="utf-8"
        )
        assert st.State.load(path).services["x"].level == 1


class TestFingerprint:
    def test_stable_for_same_situation(self):
        a = report(Level.OUTAGE, [incident("i1", "identified")])
        b = report(Level.OUTAGE, [incident("i1", "identified")])
        assert a.fingerprint() == b.fingerprint()

    def test_order_independent(self):
        a = report(Level.OUTAGE, [incident("i1"), incident("i2")])
        b = report(Level.OUTAGE, [incident("i2"), incident("i1")])
        assert a.fingerprint() == b.fingerprint()

    def test_independent_of_level(self):
        # Level transitions are signalled by the escalation/recovery
        # branches; folding the level in here would make a service
        # oscillating around a threshold re-notify every cycle.
        a = report(Level.WARNING, [incident()])
        b = report(Level.OUTAGE, [incident()])
        assert a.fingerprint() == b.fingerprint()

    def test_changes_with_incident_set(self):
        a = report(Level.OUTAGE, [incident("i1")])
        b = report(Level.OUTAGE, [incident("i1"), incident("i2")])
        assert a.fingerprint() != b.fingerprint()


class TestServiceReportLevel:
    def test_worst_source_wins(self):
        r = ServiceReport(
            key="k",
            name="n",
            downdetector=SourceResult(source="downdetector", level=Level.WARNING),
            official=SourceResult(source="official", level=Level.OUTAGE),
        )
        assert r.level is Level.OUTAGE

    def test_unknown_source_is_ignored(self):
        # A blocked Downdetector must not mask a major official incident.
        r = ServiceReport(
            key="k",
            name="n",
            downdetector=SourceResult(source="downdetector", level=Level.UNKNOWN, error="blocked"),
            official=SourceResult(source="official", level=Level.OUTAGE),
        )
        assert r.level is Level.OUTAGE

    def test_all_unknown_is_unknown(self):
        r = ServiceReport(
            key="k",
            name="n",
            downdetector=SourceResult(source="downdetector", level=Level.UNKNOWN, error="x"),
            official=SourceResult(source="official", level=Level.UNKNOWN, error="y"),
        )
        assert r.level is Level.UNKNOWN

    def test_downdetector_alone_can_trigger(self):
        # The X case: no official status exists at all.
        r = ServiceReport(
            key="x",
            name="X",
            downdetector=SourceResult(source="downdetector", level=Level.OUTAGE),
            official=SourceResult(source="official", level=Level.UNKNOWN),
        )
        assert r.level is Level.OUTAGE
