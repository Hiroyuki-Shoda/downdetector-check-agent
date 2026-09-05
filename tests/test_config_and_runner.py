"""Config loading, end-to-end runner behaviour, and prompt construction."""

from __future__ import annotations

import pytest

from dd_agent import config as config_mod
from dd_agent import runner
from dd_agent import state as st
from dd_agent import summarize
from dd_agent.models import Incident, Level, ServiceReport, SourceResult
from dd_agent.sources import official

REQUESTED_SERVICES = [
    "x", "chatgpt", "gemini", "gmail",
    "aws", "claude", "slack", "backlog", "salesforce",
]


@pytest.fixture(scope="module")
def shipped_cfg():
    return config_mod.load()


class TestShippedConfig:
    """The bundled services.yaml must be valid and cover every request."""

    @pytest.fixture
    def cfg(self, shipped_cfg):
        return shipped_cfg

    def test_loads(self, cfg):
        assert cfg.services

    def test_covers_every_requested_service(self, cfg):
        assert sorted(s.key for s in cfg.services) == sorted(REQUESTED_SERVICES)

    def test_every_official_kind_has_an_adapter(self, cfg):
        for svc in cfg.services:
            kind = svc.official.get("kind")
            assert kind in official.ADAPTERS, f"{svc.key}: unknown kind {kind}"

    def test_downdetector_urls_use_the_jp_shougai_prefix(self, cfg):
        for svc in cfg.services:
            if svc.downdetector_url:
                assert svc.downdetector_url.startswith("https://downdetector.jp/shougai/"), svc.key
                assert svc.downdetector_url.endswith("/"), svc.key

    def test_adapters_have_their_required_keys(self, cfg):
        required = {
            "statuspage": ["base"],
            "statuspal": ["api"],
            "slack": ["api"],
            "google": ["api"],
            "salesforce": ["api"],
            "aws": ["apis"],
            "feed": ["api"],
            "none": [],
        }
        for svc in cfg.services:
            kind = svc.official["kind"]
            for key in required[kind]:
                assert key in svc.official, f"{svc.key}: {kind} needs {key!r}"

    def test_every_service_has_at_least_one_usable_source(self, cfg):
        for svc in cfg.services:
            has_official = svc.official.get("kind") != "none"
            assert svc.downdetector_url or has_official, svc.key


class TestConfigParsing:
    def base(self, **over):
        raw = {
            "services": [{"key": "a", "name": "A", "downdetector_url": "https://x.test/"}],
        }
        raw.update(over)
        return raw

    def test_defaults_applied(self):
        cfg = config_mod.from_dict(self.base())
        assert cfg.notify_level is Level.WARNING
        assert cfg.defaults.min_reports == 20
        assert cfg.services[0].official == {"kind": "none"}

    def test_per_service_thresholds_override(self):
        cfg = config_mod.from_dict(
            self.base(
                services=[
                    {"key": "a", "name": "A", "thresholds": {"min_reports": 100}},
                    {"key": "b", "name": "B"},
                ]
            )
        )
        assert cfg.thresholds_for(cfg.service("a")).min_reports == 100
        assert cfg.thresholds_for(cfg.service("b")).min_reports == 20

    def test_notify_level_parsed(self):
        cfg = config_mod.from_dict(self.base(notify={"level": "outage"}))
        assert cfg.notify_level is Level.OUTAGE

    def test_bad_notify_level_rejected(self):
        with pytest.raises(ValueError, match="notify.level"):
            config_mod.from_dict(self.base(notify={"level": "catastrophe"}))

    def test_duplicate_keys_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            config_mod.from_dict(
                self.base(services=[{"key": "a", "name": "A"}, {"key": "a", "name": "A2"}])
            )

    def test_empty_services_rejected(self):
        with pytest.raises(ValueError, match="no services"):
            config_mod.from_dict({"services": []})

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            config_mod.load("/nonexistent/services.yaml")


class TestRunner:
    """Runner behaviour with all network access stubbed out."""

    @pytest.fixture
    def cfg(self):
        return config_mod.from_dict(
            {
                "services": [
                    {"key": "a", "name": "A", "downdetector_url": "https://downdetector.jp/shougai/a/"},
                    {"key": "b", "name": "B", "downdetector_url": "https://downdetector.jp/shougai/b/"},
                ],
                "summary": {"enabled": False},
                "downdetector_delay": 0,
            }
        )

    @pytest.fixture
    def stub(self, monkeypatch):
        """Make collect() return a chosen level per service, and capture posts."""
        posted = []

        def install(levels: dict):
            def fake_collect(cfg, svc, return_html=False):
                return ServiceReport(
                    key=svc.key,
                    name=svc.name,
                    downdetector=SourceResult(
                        source="downdetector", level=levels[svc.key], detail="stub"
                    ),
                    official=SourceResult(source="official", level=Level.UNKNOWN),
                )

            monkeypatch.setattr(runner, "collect", fake_collect)
            monkeypatch.setattr(
                runner.notify, "post", lambda url, payload, **kw: posted.append(payload)
            )
            return posted

        return install

    def test_healthy_run_posts_nothing(self, cfg, stub):
        posted = stub({"a": Level.OK, "b": Level.OK})
        outcomes = runner.run(cfg, state=st.State(), webhook_url="https://h.test")
        assert posted == []
        assert runner.exit_code(outcomes) == 0

    def test_outage_posts_once_then_stays_quiet(self, cfg, stub):
        posted = stub({"a": Level.OUTAGE, "b": Level.OK})
        state = st.State()
        runner.run(cfg, state=state, webhook_url="https://h.test")
        assert len(posted) == 1
        runner.run(cfg, state=state, webhook_url="https://h.test")
        assert len(posted) == 1  # de-duplicated on the second cycle

    def test_dry_run_posts_nothing_but_reports(self, cfg, stub):
        posted = stub({"a": Level.OUTAGE, "b": Level.OK})
        outcomes = runner.run(cfg, state=st.State(), webhook_url=None, dry_run=True)
        assert posted == []
        assert [o.decision.kind for o in outcomes if o.decision.notify] == ["new"]

    def test_force_renotifies(self, cfg, stub):
        posted = stub({"a": Level.OUTAGE, "b": Level.OK})
        state = st.State()
        runner.run(cfg, state=state, webhook_url="https://h.test")
        runner.run(cfg, state=state, webhook_url="https://h.test", force=True)
        assert len(posted) == 2

    def test_only_filters_services(self, cfg, stub):
        posted = stub({"a": Level.OUTAGE, "b": Level.OUTAGE})
        outcomes = runner.run(cfg, state=st.State(), webhook_url="https://h.test", only=["b"])
        assert [o.report.key for o in outcomes] == ["b"]
        assert len(posted) == 1

    def test_unknown_only_key_raises(self, cfg, stub):
        stub({"a": Level.OK, "b": Level.OK})
        with pytest.raises(ValueError, match="unknown service key"):
            runner.run(cfg, state=st.State(), webhook_url="https://h.test", only=["nope"])

    def test_failed_delivery_does_not_advance_state(self, cfg, monkeypatch):
        """A Slack outage must not swallow the one alert that mattered."""

        def fake_collect(c, svc, return_html=False):
            return ServiceReport(
                key=svc.key,
                name=svc.name,
                downdetector=SourceResult(source="downdetector", level=Level.OUTAGE),
                official=SourceResult(source="official", level=Level.UNKNOWN),
            )

        monkeypatch.setattr(runner, "collect", fake_collect)

        attempts = []

        def flaky(url, payload, **kw):
            attempts.append(payload)
            raise runner.notify.SlackError("Slack down")

        monkeypatch.setattr(runner.notify, "post", flaky)
        state = st.State()
        outcomes = runner.run(cfg, state=state, webhook_url="https://h.test")
        assert runner.exit_code(outcomes) == 1
        assert state.services == {}  # nothing recorded

        # Next cycle, with Slack back, must retry rather than skip.
        monkeypatch.setattr(runner.notify, "post", lambda url, payload, **kw: attempts.append(payload))
        runner.run(cfg, state=state, webhook_url="https://h.test")
        assert len(attempts) == 4  # 2 services failed, then 2 succeeded

    def test_summariser_failure_still_delivers(self, cfg, monkeypatch, stub):
        posted = stub({"a": Level.OUTAGE, "b": Level.OK})
        cfg.summarize = True
        monkeypatch.setattr(runner.summarize_mod, "summarize", lambda *a, **k: None)
        runner.run(cfg, state=st.State(), webhook_url="https://h.test")
        assert len(posted) == 1
        # Falls back to the deterministic template rather than posting nothing.
        assert posted[0]["blocks"][1]["text"]["text"]

    def test_summarise_run_renders_all_services(self, cfg, stub):
        stub({"a": Level.OUTAGE, "b": Level.OK})
        outcomes = runner.run(cfg, state=st.State(), webhook_url=None, dry_run=True)
        text = runner.summarise_run(outcomes)
        assert "A" in text and "B" in text


class TestSummarizePrompt:
    def test_includes_both_sources(self):
        report = ServiceReport(
            key="chatgpt",
            name="ChatGPT",
            downdetector=SourceResult(
                source="downdetector",
                level=Level.OUTAGE,
                detail="報告数 4,231件",
                data={
                    "reports_current": 4231,
                    "reports_baseline": 230,
                    "reports_ratio": 18.4,
                    "breakdown": [{"label": "ログイン", "percent": 62}],
                },
            ),
            official=SourceResult(
                source="official",
                level=Level.OUTAGE,
                data={"description": "Partial Outage"},
                incidents=[
                    Incident(
                        id="i", title="Elevated error rates", status="investigating",
                        impact="major", body="We are investigating.",
                    )
                ],
            ),
        )
        prompt = summarize.build_prompt(report)
        assert "ChatGPT" in prompt
        assert "4231" in prompt
        assert "ログイン" in prompt
        assert "Elevated error rates" in prompt
        assert "We are investigating." in prompt

    def test_notes_missing_official_status(self):
        report = ServiceReport(
            key="x",
            name="X",
            downdetector=SourceResult(source="downdetector", level=Level.OUTAGE, detail="報告数 900件"),
            official=SourceResult(source="official", level=Level.UNKNOWN),
        )
        prompt = summarize.build_prompt(report)
        assert "機械可読な公式ステータスがありません" in prompt

    def test_reports_source_failure(self):
        report = ServiceReport(
            key="x",
            name="X",
            downdetector=SourceResult(source="downdetector", level=Level.UNKNOWN, error="blocked"),
            official=SourceResult(source="official", level=Level.OUTAGE, detail="major"),
        )
        prompt = summarize.build_prompt(report)
        assert "取得失敗" in prompt

    def test_summarize_returns_none_without_sdk(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_anthropic(name, *a, **kw):
            if name == "anthropic":
                raise ImportError("not installed")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", no_anthropic)
        report = ServiceReport(key="x", name="X")
        assert summarize.summarize(report) is None
