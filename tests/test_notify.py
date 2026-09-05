"""Slack payload and delivery tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dd_agent import notify
from dd_agent.models import Incident, Level, ServiceReport, SourceResult

NOW = datetime(2026, 9, 5, 3, 30, tzinfo=timezone.utc)  # 12:30 JST


def make_report(level=Level.OUTAGE, *, dd_error=None, incidents=None) -> ServiceReport:
    return ServiceReport(
        key="chatgpt",
        name="ChatGPT",
        downdetector=SourceResult(
            source="downdetector",
            level=Level.UNKNOWN if dd_error else level,
            url="https://downdetector.jp/shougai/openai/",
            detail="" if dd_error else "報告数 4,231件 (平常時 230件 / 比 18.4倍)",
            error=dd_error,
        ),
        official=SourceResult(
            source="official",
            level=level,
            url="https://status.openai.com/",
            detail="[major] Elevated error rates on API",
            data={"description": "Partial Outage"},
            incidents=incidents or [],
        ),
    )


class TestPayload:
    def test_fallback_text_stands_alone(self):
        # `text` is what shows in the sidebar and the mobile push, where
        # blocks are not rendered.
        p = notify.build_payload(make_report(), kind="new", now=NOW)
        assert p["text"] == "🔴 障害検知: ChatGPT (障害)"

    def test_header_and_summary_present(self):
        p = notify.build_payload(make_report(), kind="new", summary="APIでエラー増加中。", now=NOW)
        assert p["blocks"][0]["type"] == "header"
        assert "ChatGPT" in p["blocks"][0]["text"]["text"]
        assert p["blocks"][1]["text"]["text"] == "APIでエラー増加中。"

    def test_both_source_fields_rendered(self):
        p = notify.build_payload(make_report(), kind="new", now=NOW)
        fields = next(b for b in p["blocks"] if b.get("fields"))["fields"]
        blob = json.dumps(fields, ensure_ascii=False)
        assert "Downdetector" in blob and "4,231" in blob
        assert "公式ステータス" in blob and "Elevated error rates" in blob

    def test_source_error_is_shown_not_hidden(self):
        p = notify.build_payload(make_report(dd_error="blocked: challenge"), kind="new", now=NOW)
        blob = json.dumps(p["blocks"], ensure_ascii=False)
        assert "取得失敗" in blob and "blocked" in blob

    def test_jst_timestamp_in_context(self):
        p = notify.build_payload(make_report(), kind="new", now=NOW)
        ctx = p["blocks"][-1]["elements"][0]["text"]
        assert "2026-09-05 12:30 JST" in ctx

    def test_links_are_deduplicated(self):
        inc = Incident(id="i", title="Elevated errors", status="investigating", impact="major",
                       url="https://status.openai.com/")
        p = notify.build_payload(make_report(incidents=[inc]), kind="new", now=NOW)
        ctx = p["blocks"][-1]["elements"][0]["text"]
        assert ctx.count("https://status.openai.com/") == 1

    def test_recovery_uses_its_own_wording(self):
        p = notify.build_payload(make_report(level=Level.OK), kind="recovery", now=NOW)
        assert p["text"].startswith("✅ 復旧")
        assert "正常な状態に戻りました" in p["blocks"][1]["text"]["text"]

    @pytest.mark.parametrize(
        "kind,heading",
        [
            ("new", "障害検知"),
            ("escalation", "障害レベル上昇"),
            ("update", "障害情報 更新"),
            ("reminder", "障害継続中"),
            ("recovery", "復旧"),
        ],
    )
    def test_headings(self, kind, heading):
        p = notify.build_payload(make_report(), kind=kind, now=NOW)
        assert heading in p["blocks"][0]["text"]["text"]

    def test_warning_uses_warning_emoji(self):
        p = notify.build_payload(make_report(level=Level.WARNING), kind="new", now=NOW)
        assert p["text"].startswith("🟡")

    def test_payload_is_json_serialisable(self):
        p = notify.build_payload(make_report(), kind="new", now=NOW)
        assert json.loads(json.dumps(p, ensure_ascii=False)) == p


class TestTemplateSummary:
    def test_prefers_official_incident_text(self):
        inc = Incident(
            id="i", title="Elevated error rates", status="investigating", impact="major",
            body="We are investigating elevated API errors.",
        )
        text = notify.template_summary(make_report(incidents=[inc]))
        assert "Elevated error rates" in text
        assert "We are investigating" in text

    def test_notes_when_only_user_reports_exist(self):
        r = make_report(level=Level.WARNING)
        r.official.level = Level.OK
        r.official.incidents = []
        text = notify.template_summary(r)
        assert "利用者からの報告のみ" in text

    def test_never_returns_empty(self):
        r = ServiceReport(key="x", name="X")
        assert notify.template_summary(r)


class TestPost:
    def test_missing_webhook_raises(self):
        with pytest.raises(notify.SlackError, match="webhook"):
            notify.post("", {"text": "hi"})

    def test_success(self, monkeypatch):
        sent = {}

        class Resp:
            status = 200

            def read(self):
                return b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["body"] = json.loads(req.data.decode("utf-8"))
            return Resp()

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        notify.post("https://hooks.slack.test/abc", {"text": "hi"})
        assert sent["body"] == {"text": "hi"}

    def test_client_error_is_not_retried(self, monkeypatch):
        import urllib.error

        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, __import__("io").BytesIO(b"no_service")
            )

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(notify.SlackError, match="404"):
            notify.post("https://hooks.slack.test/abc", {"text": "hi"})
        assert len(calls) == 1  # a revoked webhook must fail fast, not retry

    def test_non_ok_body_raises(self, monkeypatch):
        class Resp:
            status = 200

            def read(self):
                return b"invalid_payload"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=None: Resp())
        with pytest.raises(notify.SlackError, match="invalid_payload"):
            notify.post("https://hooks.slack.test/abc", {"text": "hi"})
