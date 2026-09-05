"""Official status adapter tests.

None of these endpoints could be reached from the build environment, so
these tests pin each adapter against the payload shape documented by the
provider. They prove the mapping logic is right; `dd-agent diagnose`
proves the endpoint is right.
"""

from __future__ import annotations

import pytest

from dd_agent.http import FetchError
from dd_agent.models import Level
from dd_agent.sources import official


@pytest.fixture
def fake_json(monkeypatch):
    """Serve canned JSON per URL suffix."""

    def install(mapping: dict):
        def fake(url, **kw):
            for suffix, payload in mapping.items():
                if url.endswith(suffix):
                    if isinstance(payload, Exception):
                        raise payload
                    return payload
            raise FetchError(f"unexpected URL {url}")

        monkeypatch.setattr(official, "fetch_json", fake)

    return install


# --- Atlassian Statuspage (OpenAI, Claude) -----------------------------------


def summary_payload(indicator="none", description="All Systems Operational", incidents=()):
    return {
        "page": {"id": "abc", "name": "Test"},
        "status": {"indicator": indicator, "description": description},
        "incidents": list(incidents),
    }


class TestStatuspage:
    def test_all_operational(self, fake_json):
        fake_json({"summary.json": summary_payload()})
        res = official.check({"kind": "statuspage", "base": "https://status.openai.com"})
        assert res.ok and res.level is Level.OK
        assert res.detail == "All Systems Operational"

    @pytest.mark.parametrize(
        "indicator,expected",
        [
            ("none", Level.OK),
            ("maintenance", Level.OK),
            ("minor", Level.WARNING),
            ("major", Level.OUTAGE),
            ("critical", Level.OUTAGE),
        ],
    )
    def test_indicator_mapping(self, fake_json, indicator, expected):
        fake_json({"summary.json": summary_payload(indicator=indicator)})
        res = official.check({"kind": "statuspage", "base": "https://status.openai.com"})
        assert res.level is expected

    def test_incident_details_extracted(self, fake_json):
        fake_json(
            {
                "summary.json": summary_payload(
                    indicator="major",
                    description="Partial Outage",
                    incidents=[
                        {
                            "id": "inc1",
                            "name": "Elevated error rates on API",
                            "status": "investigating",
                            "impact": "major",
                            "shortlink": "https://stspg.io/x",
                            "updated_at": "2026-09-05T10:00:00Z",
                            "incident_updates": [
                                {"body": "We are investigating elevated errors.", "status": "investigating"}
                            ],
                        }
                    ],
                )
            }
        )
        res = official.check({"kind": "statuspage", "base": "https://status.openai.com"})
        assert res.level is Level.OUTAGE
        assert len(res.incidents) == 1
        inc = res.incidents[0]
        assert inc.title == "Elevated error rates on API"
        assert inc.body == "We are investigating elevated errors."
        assert inc.url == "https://stspg.io/x"
        assert "Elevated error rates" in res.detail

    def test_resolved_incidents_are_skipped(self, fake_json):
        fake_json(
            {
                "summary.json": summary_payload(
                    incidents=[{"id": "i", "name": "Old", "status": "resolved", "impact": "major"}]
                )
            }
        )
        res = official.check({"kind": "statuspage", "base": "https://status.openai.com"})
        assert res.incidents == []
        assert res.level is Level.OK

    def test_open_incident_with_clear_banner_becomes_warning(self, fake_json):
        # Providers routinely leave the top banner green while an incident
        # is still open. Trust the incident, not the banner.
        fake_json(
            {
                "summary.json": summary_payload(
                    indicator="none",
                    incidents=[
                        {"id": "i", "name": "Degraded search", "status": "monitoring", "impact": "minor"}
                    ],
                )
            }
        )
        res = official.check({"kind": "statuspage", "base": "https://status.openai.com"})
        assert res.level is Level.WARNING

    def test_fetch_failure_is_unknown_not_outage(self, fake_json):
        fake_json({"summary.json": FetchError("HTTP 503")})
        res = official.check({"kind": "statuspage", "base": "https://status.openai.com"})
        assert not res.ok and res.level is Level.UNKNOWN


# --- Slack -------------------------------------------------------------------


class TestSlack:
    def test_ok(self, fake_json):
        fake_json({"current": {"status": "ok", "active_incidents": []}})
        res = official.check({"kind": "slack", "api": "https://slack-status.com/api/v2.0.0/current"})
        assert res.ok and res.level is Level.OK

    def test_active_incident(self, fake_json):
        fake_json(
            {
                "current": {
                    "status": "active",
                    "active_incidents": [
                        {
                            "id": 42,
                            "title": "Trouble sending messages",
                            "type": "incident",
                            "status": "active",
                            "services": ["Messaging", "Connections"],
                            "url": "https://status.slack.com/2026-09/abc",
                            "date_updated": "2026-09-05T10:00:00Z",
                            "notes": [{"body": "We are investigating."}],
                        }
                    ],
                }
            }
        )
        res = official.check({"kind": "slack", "api": "https://slack-status.com/api/v2.0.0/current"})
        assert res.level is Level.OUTAGE
        assert res.incidents[0].title == "Trouble sending messages"
        assert "Messaging" in res.incidents[0].body

    def test_maintenance_is_not_an_incident(self, fake_json):
        fake_json(
            {
                "current": {
                    "status": "active",
                    "active_incidents": [
                        {"id": 1, "title": "Planned work", "type": "maintenance", "status": "active"}
                    ],
                }
            }
        )
        res = official.check({"kind": "slack", "api": "https://slack-status.com/api/v2.0.0/current"})
        assert res.incidents == []


# --- Statuspal (Backlog / Nulab) ---------------------------------------------


class TestStatuspal:
    BASE = "https://statuspal.io/api/v2/status_pages/nulab"

    def test_healthy_skips_the_summary_request(self, fake_json):
        # A healthy poll must cost one request, not two.
        fake_json({"/status": {"status_page": {"current_incident_type": None}}})
        res = official.check({"kind": "statuspal", "api": self.BASE})
        assert res.ok and res.level is Level.OK

    @pytest.mark.parametrize(
        "kind,expected",
        [("major", Level.OUTAGE), ("minor", Level.WARNING), ("scheduled", Level.OK)],
    )
    def test_incident_type_mapping(self, fake_json, kind, expected):
        fake_json(
            {
                "/status": {"status_page": {"current_incident_type": kind}},
                "/summary": {"services": [], "incidents": []},
            }
        )
        res = official.check({"kind": "statuspal", "api": self.BASE})
        assert res.level is expected

    def test_incident_detail_from_summary(self, fake_json):
        fake_json(
            {
                "/status": {"status_page": {"current_incident_type": "major"}},
                "/summary": {
                    "services": [{"name": "Backlog (Japan)", "current_incident_type": "major"}],
                    "incidents": [
                        {
                            "id": 7,
                            "title": "Backlog にアクセスできない障害",
                            "type": "major",
                            "description": "<p>調査中です</p>",
                            "services": [{"name": "Backlog (Japan)"}],
                            "starts_at": "2026-09-05T10:00:00Z",
                        }
                    ],
                },
            }
        )
        res = official.check({"kind": "statuspal", "api": self.BASE, "services": ["backlog"]})
        assert res.level is Level.OUTAGE
        assert res.incidents[0].title == "Backlog にアクセスできない障害"
        assert "調査中です" in res.incidents[0].body

    def test_unrelated_service_on_shared_page_does_not_alert(self, fake_json):
        # Nulab's page covers Cacoo and Nulab Apps too. A Cacoo outage must
        # not be reported as a Backlog outage.
        fake_json(
            {
                "/status": {"status_page": {"current_incident_type": "major"}},
                "/summary": {
                    "services": [
                        {"name": "Backlog (Japan)", "current_incident_type": None},
                        {"name": "Cacoo", "current_incident_type": "major"},
                    ],
                    "incidents": [
                        {"id": 1, "title": "Cacoo down", "services": [{"name": "Cacoo"}]}
                    ],
                },
            }
        )
        res = official.check({"kind": "statuspal", "api": self.BASE, "services": ["backlog"]})
        assert res.level is Level.OK
        assert "他サービス" in res.detail

    def test_summary_failure_keeps_page_level_verdict(self, fake_json):
        fake_json(
            {
                "/status": {"status_page": {"current_incident_type": "major"}},
                "/summary": FetchError("HTTP 500"),
            }
        )
        res = official.check({"kind": "statuspal", "api": self.BASE})
        assert res.level is Level.OUTAGE
        assert "詳細取得失敗" in res.detail


# --- Salesforce --------------------------------------------------------------


class TestSalesforce:
    API = "https://api.status.salesforce.com/v1/instances/status"

    def test_configured_instance_ok(self, fake_json):
        fake_json({"status": [{"key": "AP15", "status": "OK", "Incidents": []}]})
        res = official.check({"kind": "salesforce", "api": self.API, "instances": ["AP15"]})
        assert res.ok and res.level is Level.OK

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("OK", Level.OK),
            ("MAINTENANCE_CORE", Level.OK),
            ("INFORMATIONAL_CORE", Level.OK),
            ("MINOR_INCIDENT_CORE", Level.WARNING),
            ("MAJOR_INCIDENT_CORE", Level.OUTAGE),
            ("MAJOR_INCIDENT_NONCORE", Level.OUTAGE),
        ],
    )
    def test_status_enum_mapping(self, fake_json, status, expected):
        fake_json({"status": [{"key": "AP15", "status": status}]})
        res = official.check({"kind": "salesforce", "api": self.API, "instances": ["AP15"]})
        assert res.level is expected

    def test_other_instances_ignored_when_filtered(self, fake_json):
        fake_json(
            {
                "status": [
                    {"key": "AP15", "status": "OK"},
                    {"key": "NA224", "status": "MAJOR_INCIDENT_CORE"},
                ]
            }
        )
        res = official.check({"kind": "salesforce", "api": self.API, "instances": ["AP15"]})
        assert res.level is Level.OK

    def test_unfiltered_reports_worst_and_names_instances(self, fake_json):
        fake_json(
            {
                "status": [
                    {"key": "AP15", "status": "OK"},
                    {"key": "NA224", "status": "MAJOR_INCIDENT_CORE"},
                ]
            }
        )
        res = official.check({"kind": "salesforce", "api": self.API})
        assert res.level is Level.OUTAGE
        assert res.data["affected_instances"] == ["NA224"]

    def test_missing_configured_instance_is_an_error(self, fake_json):
        # A typo'd instance key would otherwise look like "all clear"
        # forever, which is the most dangerous possible failure mode.
        fake_json({"status": [{"key": "NA224", "status": "OK"}]})
        res = official.check({"kind": "salesforce", "api": self.API, "instances": ["AP99"]})
        assert not res.ok
        assert "AP99" in res.error

    def test_incident_text_extracted(self, fake_json):
        fake_json(
            {
                "status": [
                    {
                        "key": "AP15",
                        "status": "MAJOR_INCIDENT_CORE",
                        "Incidents": [
                            {
                                "id": "inc9",
                                "message": "Service degradation",
                                "IncidentImpacts": [{"severity": "major"}],
                                "IncidentEvents": [{"message": "<p>Investigating</p>"}],
                            }
                        ],
                    }
                ]
            }
        )
        res = official.check({"kind": "salesforce", "api": self.API, "instances": ["AP15"]})
        assert res.incidents[0].title == "Service degradation"
        assert "Investigating" in res.incidents[0].body

    def test_empty_payload_is_an_error(self, fake_json):
        fake_json({"status": []})
        res = official.check({"kind": "salesforce", "api": self.API})
        assert not res.ok


# --- Google ------------------------------------------------------------------


class TestGoogle:
    API = "https://www.google.com/appsstatus/dashboard/incidents.json"

    def test_no_open_incidents(self, fake_json):
        fake_json({"incidents.json": [{"service_name": "Gmail", "end": "2026-09-01T00:00:00Z"}]})
        res = official.check({"kind": "google", "api": self.API, "products": ["gmail"]})
        assert res.ok and res.level is Level.OK

    def test_open_incident_for_matching_product(self, fake_json):
        fake_json(
            {
                "incidents.json": [
                    {
                        "id": "g1",
                        "service_name": "Gmail",
                        "external_desc": "Users report delays sending mail",
                        "status_impact": "SERVICE_DISRUPTION",
                        "uri": "/appsstatus/dashboard/incidents/abc",
                        "begin": "2026-09-05T09:00:00Z",
                        "most_recent_update": {"text": "Our team is investigating.", "status": "active"},
                    }
                ]
            }
        )
        res = official.check({"kind": "google", "api": self.API, "products": ["gmail"]})
        assert res.level is Level.OUTAGE
        assert res.incidents[0].title == "Users report delays sending mail"
        assert res.incidents[0].url.startswith("https://www.google.com/")

    def test_other_product_filtered_out(self, fake_json):
        fake_json(
            {
                "incidents.json": [
                    {"id": "g2", "service_name": "Google Drive", "status_impact": "SERVICE_OUTAGE"}
                ]
            }
        )
        res = official.check({"kind": "google", "api": self.API, "products": ["gmail"]})
        assert res.level is Level.OK

    def test_affected_products_are_also_matched(self, fake_json):
        fake_json(
            {
                "incidents.json": [
                    {
                        "id": "g3",
                        "service_name": "Google Cloud",
                        "affected_products": [{"title": "Vertex AI"}],
                        "status_impact": "SERVICE_INFORMATION",
                    }
                ]
            }
        )
        res = official.check({"kind": "google", "api": self.API, "products": ["vertex ai"]})
        assert res.level is Level.WARNING

    def test_unexpected_shape_is_an_error(self, fake_json):
        fake_json({"incidents.json": {"not": "a list"}})
        res = official.check({"kind": "google", "api": self.API})
        assert not res.ok


# --- AWS ---------------------------------------------------------------------


class TestAWS:
    def test_legacy_data_json_current_events(self, fake_json):
        fake_json(
            {
                "data.json": {
                    "current": [
                        {
                            "service_name": "Amazon EC2 (Tokyo)",
                            "service": "ec2-ap-northeast-1",
                            "region": "ap-northeast-1",
                            "summary": "Increased API error rates",
                            "status_code": "service-disruption",
                            "date": "1757066400",
                        }
                    ],
                    "archive": [],
                }
            }
        )
        res = official.check(
            {"kind": "aws", "apis": ["https://status.aws.amazon.com/data.json"], "regions": ["ap-northeast-1"]}
        )
        assert res.ok and res.level is Level.OUTAGE
        assert "Increased API error rates" in res.incidents[0].body

    def test_region_filter_excludes_other_regions(self, fake_json):
        fake_json(
            {
                "data.json": {
                    "current": [
                        {"service": "s3-us-east-1", "region": "us-east-1", "summary": "x", "status_code": "service-disruption"}
                    ]
                }
            }
        )
        res = official.check(
            {"kind": "aws", "apis": ["https://status.aws.amazon.com/data.json"], "regions": ["ap-northeast-1"]}
        )
        assert res.level is Level.OK

    def test_falls_back_to_second_endpoint(self, fake_json):
        fake_json({"data.json": FetchError("HTTP 404"), "status": {"current": []}})
        res = official.check(
            {
                "kind": "aws",
                "apis": ["https://status.aws.amazon.com/data.json", "https://health.aws.amazon.com/health/status"],
            }
        )
        assert res.ok and res.level is Level.OK

    def test_all_endpoints_failing_is_unknown(self, fake_json):
        fake_json({"data.json": FetchError("HTTP 404")})
        res = official.check({"kind": "aws", "apis": ["https://status.aws.amazon.com/data.json"]})
        assert not res.ok and res.level is Level.UNKNOWN


# --- misc --------------------------------------------------------------------


class TestFeed:
    """RSS/Atom fallback — the documented path for the Google dashboards."""

    @pytest.fixture
    def fake_feed(self, monkeypatch):
        from dd_agent.http import Response

        def install(xml: str):
            monkeypatch.setattr(
                official, "fetch", lambda url, **kw: Response(url=url, status_code=200, text=xml)
            )

        return install

    def _rss(self, items: str) -> str:
        return f"<rss><channel>{items}</channel></rss>"

    def _item(self, title, body, link, age_hours) -> str:
        import email.utils
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        return (
            f"<item><title>{title}</title><description>{body}</description>"
            f"<link>{link}</link><pubDate>{email.utils.format_datetime(when)}</pubDate></item>"
        )

    def test_recent_entry_becomes_an_incident(self, fake_feed):
        fake_feed(self._rss(self._item("Gmail の送信遅延", "&lt;p&gt;調査中&lt;/p&gt;", "https://g.test/1", 1)))
        res = official.check({"kind": "feed", "api": "https://g.test/rss"})
        assert res.ok and res.level is Level.WARNING
        assert res.incidents[0].title == "Gmail の送信遅延"
        assert res.incidents[0].url == "https://g.test/1"
        assert "<" not in res.incidents[0].body  # HTML stripped

    def test_old_entries_are_ignored(self, fake_feed):
        # These feeds keep history forever; without the age filter every
        # historical outage would look current.
        fake_feed(self._rss(self._item("過去の障害", "解決済み", "https://g.test/0", 24 * 5)))
        res = official.check({"kind": "feed", "api": "https://g.test/rss", "recent_hours": 24})
        assert res.ok and res.level is Level.OK
        assert res.incidents == []

    def test_atom_feed_with_link_href(self, fake_feed):
        fake_feed(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<title>Service disruption</title><summary>Investigating</summary>"
            '<link href="https://g.test/a"/></entry></feed>'
        )
        res = official.check({"kind": "feed", "api": "https://g.test/atom"})
        assert res.incidents[0].url == "https://g.test/a"

    def test_malformed_xml_is_ok_not_a_crash(self, fake_feed):
        fake_feed("<rss><channel><item><title>broken")
        res = official.check({"kind": "feed", "api": "https://g.test/rss"})
        assert res.ok and res.level is Level.OK

    def test_fetch_failure_is_unknown(self, monkeypatch):
        def boom(url, **kw):
            raise FetchError("HTTP 404")

        monkeypatch.setattr(official, "fetch", boom)
        res = official.check({"kind": "feed", "api": "https://g.test/rss"})
        assert not res.ok and res.level is Level.UNKNOWN


class TestNullTolerance:
    """Null JSON values must not crash, nor leak the literal string "None".

    These payloads all carry keys that are *present but null* — common in
    real status feeds and the case that `dict.get(k, "")` does not cover.
    """

    CASES = [
        (
            {"kind": "statuspage", "base": "https://x.test"},
            {"summary.json": {"status": None, "incidents": [{"id": None, "name": None, "status": None, "impact": None}]}},
        ),
        (
            {"kind": "slack", "api": "https://x.test/current"},
            {"current": {"status": None, "active_incidents": [{"id": None, "title": None, "type": None, "notes": None}]}},
        ),
        (
            {"kind": "google", "api": "https://x.test/i.json"},
            {"i.json": [{"id": None, "service_name": None, "external_desc": None, "status_impact": None, "uri": None, "most_recent_update": None}]},
        ),
        (
            {"kind": "aws", "apis": ["https://x.test/data.json"]},
            {"data.json": {"current": [{"service": None, "region": None, "summary": None, "status_code": None}]}},
        ),
        (
            {"kind": "salesforce", "api": "https://x.test/instances/status"},
            {"status": [{"key": None, "status": None, "Incidents": [{"id": None, "message": None}]}]},
        ),
        (
            {"kind": "statuspal", "api": "https://x.test"},
            {
                "/status": {"status_page": {"current_incident_type": "major"}},
                "/summary": {"services": None, "incidents": [{"id": None, "title": None, "description": None}]},
            },
        ),
    ]

    @pytest.mark.parametrize("cfg,payload", CASES, ids=[c[0]["kind"] for c in CASES])
    def test_no_crash_and_no_none_string(self, fake_json, cfg, payload):
        fake_json(payload)
        res = official.check(cfg)
        assert not (res.error and "adapter error" in res.error), res.error
        assert "None" not in (res.detail or "")
        for inc in res.incidents:
            assert "None" not in inc.title
            assert "None" not in inc.impact

    def test_google_null_uri_falls_back_to_page_url(self, fake_json):
        fake_json({"i.json": [{"id": "g", "service_name": "Gmail", "status_impact": "SERVICE_OUTAGE", "uri": None}]})
        res = official.check(
            {"kind": "google", "api": "https://x.test/i.json", "url": "https://dash.test/", "products": ["gmail"]}
        )
        assert res.incidents[0].url == "https://dash.test/"


class TestDispatch:
    def test_none_kind_is_unknown_without_error(self):
        res = official.check({"kind": "none", "url": "https://docs.x.com/status"})
        assert res.level is Level.UNKNOWN
        assert res.error is None
        assert res.detail == "公式ステータスページなし"

    def test_unknown_kind_is_an_error(self):
        res = official.check({"kind": "nope"})
        assert not res.ok
        assert "unknown official source kind" in res.error

    def test_adapter_crash_is_contained(self, monkeypatch):
        def boom(cfg, **kw):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(official.ADAPTERS, "statuspage", boom)
        res = official.check({"kind": "statuspage", "base": "https://x.test"})
        assert not res.ok
        assert "kaboom" in res.error
