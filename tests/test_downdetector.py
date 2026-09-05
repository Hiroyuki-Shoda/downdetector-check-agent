"""Parsing and scoring tests for the Downdetector source.

The live HTML could not be fetched from the build environment, so these
tests pin the *behaviour* of each extraction strategy against synthetic
pages in the shapes Downdetector is known to use. If Downdetector changes
its markup, `dd-agent diagnose` is what detects it — these tests guarantee
that the parser handles each shape it claims to.
"""

from __future__ import annotations

import pytest

from dd_agent.models import Level
from dd_agent.sources import downdetector as dd


class TestExtractSeries:
    def test_xy_object_shape(self):
        html = """
        <script>
          var chart = {"series": [{"data": [
            {"x": "2026-09-05 09:00:00", "y": 3},
            {"x": "2026-09-05 09:15:00", "y": 5},
            {"x": "2026-09-05 09:30:00", "y": 4},
            {"x": "2026-09-05 09:45:00", "y": 120}
          ]}]};
        </script>
        """
        assert dd.extract_series(html) == [3.0, 5.0, 4.0, 120.0]

    def test_highcharts_numeric_pairs(self):
        html = """
        <script>
          data: [[1757062800000, 7], [1757063700000, 9],
                 [1757064600000, 8], [1757065500000, 210]]
        </script>
        """
        assert dd.extract_series(html) == [7.0, 9.0, 8.0, 210.0]

    def test_embedded_json_script_tag(self):
        html = """
        <script type="application/json">
          {"chart": {"counts": [2, 3, 2, 4, 90]}}
        </script>
        """
        assert dd.extract_series(html) == [2.0, 3.0, 2.0, 4.0, 90.0]

    def test_single_quoted_keys(self):
        html = "<script>[{'x': '10:00', 'y': 11},{'x':'10:15','y':12},"
        html += "{'x':'10:30','y':13},{'x':'10:45','y':14}]</script>"
        assert dd.extract_series(html) == [11.0, 12.0, 13.0, 14.0]

    def test_returns_empty_when_nothing_matches(self):
        assert dd.extract_series("<html><body>no chart here</body></html>") == []

    def test_ignores_too_short_series(self):
        # Fewer than 4 points is not enough to establish a baseline, so the
        # strategy must be rejected rather than scored on noise.
        html = '<script>[{"x":"a","y":1},{"x":"b","y":2}]</script>'
        assert dd.extract_series(html) == []


class TestLevelFromSeries:
    def score(self, series, **kw):
        params = dict(min_reports=20, warning_ratio=2.5, outage_ratio=5.0, baseline_floor=1.0)
        params.update(kw)
        return dd._level_from_series(series, **params)

    def test_flat_low_traffic_is_ok(self):
        level, _, _ = self.score([2, 3, 2, 3, 2, 3])
        assert level is Level.OK

    def test_big_spike_is_outage(self):
        level, detail, data = self.score([5, 4, 6, 5, 4, 300, 320])
        assert level is Level.OUTAGE
        assert data["reports_current"] == 320
        assert data["reports_ratio"] >= 5.0
        assert "320" in detail

    def test_moderate_rise_is_warning(self):
        # Baseline 10, current 30 -> 3x: above warning (2.5) below outage (5).
        level, _, data = self.score([10, 10, 10, 10, 10, 28, 30])
        assert level is Level.WARNING
        assert data["reports_ratio"] == pytest.approx(3.0)

    def test_small_absolute_counts_never_alert(self):
        # 1 -> 8 is an 8x ratio but only 8 reports: must stay OK.
        level, _, data = self.score([1, 1, 1, 1, 1, 8])
        assert level is Level.OK
        assert data["reports_current"] == 8

    def test_uses_higher_of_last_two_buckets(self):
        # The newest bucket is still filling; the spike in the previous one
        # must not be missed.
        level, _, data = self.score([4, 5, 4, 5, 4, 400, 12])
        assert data["reports_current"] == 400
        assert level is Level.OUTAGE

    def test_baseline_uses_median_not_mean(self):
        # A long tail of spike values must not inflate the baseline enough
        # to hide the outage.
        level, _, data = self.score([3, 3, 3, 3, 3, 3, 3, 900, 950])
        assert data["reports_baseline"] == 3.0
        assert level is Level.OUTAGE

    def test_unknown_when_series_too_short(self):
        level, detail, data = self.score([5, 5])
        assert level is Level.UNKNOWN
        assert detail == ""
        assert data == {}


class TestLevelFromText:
    @pytest.mark.parametrize(
        "text",
        [
            "現在、Xで問題は発生していません",
            "ユーザーの報告によると、問題はありません",
            "問題は検出されませんでした",
            "No current problems at X",
        ],
    )
    def test_ok_sentences(self, text):
        assert dd._level_from_text(f"<html><body><h1>{text}</h1></body></html>") is Level.OK

    @pytest.mark.parametrize(
        "text",
        [
            "ユーザーの報告により、Xで障害が発生している可能性が示されています",
            "Xで問題が発生している可能性があります",
            "Problems at Slack",
        ],
    )
    def test_outage_sentences(self, text):
        assert dd._level_from_text(f"<html><body><h1>{text}</h1></body></html>") is Level.OUTAGE

    def test_ok_sentence_wins_over_later_mentions_of_shougai(self):
        # User comments further down the page routinely say 障害 regardless
        # of the actual status; the verdict sentence must win.
        html = (
            "<html><body><h1>現在、問題は発生していません</h1>"
            + "<div class='comments'>" + ("<p>障害が起きている</p>" * 50) + "</div>"
            "</body></html>"
        )
        assert dd._level_from_text(html) is Level.OK

    def test_unknown_when_no_pattern_matches(self):
        assert dd._level_from_text("<html><body><p>hello</p></body></html>") is Level.UNKNOWN


class TestCheck:
    def _page(self, status_text: str, series: list[int]) -> str:
        points = ",".join(f'{{"x":"t{i}","y":{v}}}' for i, v in enumerate(series))
        return f"<html><body><h1>{status_text}</h1><script>[{points}]</script></body></html>"

    def test_worst_of_text_and_series_wins(self, monkeypatch):
        # The page still claims all is well, but reports have spiked — this
        # is exactly the "障害の兆候" case the agent exists to catch.
        html = self._page("現在、問題は発生していません", [4, 4, 5, 4, 300, 310])
        monkeypatch.setattr(
            dd, "fetch", lambda url, **kw: type("R", (), {"text": html, "status_code": 200, "url": url})()
        )
        res = dd.check("x", "X", "https://downdetector.jp/shougai/twitter/")
        assert res.ok
        assert res.level is Level.OUTAGE
        assert res.data["page_status"] == "正常"

    def test_blocked_is_reported_as_error_not_outage(self, monkeypatch):
        def boom(url, **kw):
            raise dd.BlockedError("anti-bot challenge")

        monkeypatch.setattr(dd, "fetch", boom)
        res = dd.check("x", "X", "https://downdetector.jp/shougai/twitter/")
        assert not res.ok
        assert res.level is Level.UNKNOWN
        assert "blocked" in res.error

    def test_unparseable_page_is_an_error_not_a_verdict(self, monkeypatch):
        html = "<html><body>totally different layout</body></html>"
        monkeypatch.setattr(
            dd, "fetch", lambda url, **kw: type("R", (), {"text": html, "status_code": 200, "url": url})()
        )
        res = dd.check("x", "X", "https://downdetector.jp/shougai/twitter/")
        assert not res.ok
        assert res.level is Level.UNKNOWN
        assert "diagnose" in res.error
