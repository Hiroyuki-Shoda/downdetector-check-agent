"""Downdetector (downdetector.jp) source.

Downdetector publishes no public API, so this reads the public service page.
Two independent signals are extracted, and the worse of the two wins:

1. **The status sentence** the page renders ("問題が発生しています" vs
   "現在問題はありません"). Cheap and unambiguous when present.
2. **The report time-series** behind the page's chart, compared against its
   own baseline. This is what catches an *early sign* of trouble — reports
   climbing before Downdetector itself flips the headline to "障害".

Both are scraped, therefore both are brittle by nature. Every extraction
step is written to degrade to ``None`` rather than raise, several
alternative shapes are attempted for the series, and ``dd-agent diagnose``
dumps the raw HTML so a layout change can be diagnosed in one command
instead of guessed at.
"""

from __future__ import annotations

import json
import logging
import re
import statistics

from ..http import BlockedError, FetchError, fetch
from ..models import Level, SourceResult

log = logging.getLogger(__name__)

SOURCE = "downdetector"

# --- status sentence patterns -------------------------------------------------
# Ordered by precedence: an explicit "no problems" sentence should not be
# overridden by the word 障害 appearing elsewhere in the page chrome, so the
# OK patterns are checked first and anchored to the report wording.
_OK_PATTERNS = (
    # 「問題は発生していません」「問題は検出されませんでした」「問題は報告されていません」
    r"問題[はが]?(?:発生|報告|検出)(?:され|し)?て?い?ま?せん",
    # 「問題はありません」「問題ございません」
    r"問題[はが]?(?:あ|ご)?りま?せん",
    r"no\s+(?:current\s+)?problems?\s+at",
    r"問題なし",
)
_OUTAGE_PATTERNS = (
    r"障害が発生している(?:可能性|こと)",
    r"問題が発生している(?:可能性|こと)",
    r"障害(?:が|を)?(?:発生|報告)",
    r"problems?\s+at",
    r"障害情報",
)
_WARNING_PATTERNS = (
    r"問題の(?:兆候|可能性)",
    r"(?:報告|レポート)が(?:増加|急増)",
    r"possible\s+problems?",
)


def check(
    key: str,
    name: str,
    url: str,
    *,
    min_reports: int = 20,
    warning_ratio: float = 2.5,
    outage_ratio: float = 5.0,
    baseline_floor: float = 1.0,
    timeout: float = 20.0,
    return_html: bool = False,
) -> SourceResult:
    """Check one service on Downdetector.

    ``min_reports`` guards against ratio noise: a jump from 1 report to 4 is
    a 4x ratio but means nothing. A service only counts as degraded once the
    absolute report count clears this floor *and* the ratio clears a
    threshold.
    """
    result = SourceResult(source=SOURCE, url=url)
    try:
        resp = fetch(url, timeout=timeout, impersonate=True)
    except BlockedError as exc:
        result.error = f"blocked: {exc}"
        return result
    except FetchError as exc:
        result.error = str(exc)
        return result

    html = resp.text
    if return_html:
        result.data["html"] = html

    text_level = _level_from_text(html)
    series = extract_series(html)
    series_level, series_detail, series_data = _level_from_series(
        series,
        min_reports=min_reports,
        warning_ratio=warning_ratio,
        outage_ratio=outage_ratio,
        baseline_floor=baseline_floor,
    )

    breakdown = extract_breakdown(html)
    if breakdown:
        result.data["breakdown"] = breakdown

    levels = [lv for lv in (text_level, series_level) if lv != Level.UNKNOWN]
    if not levels:
        result.error = (
            "could not parse status text or report series from the page "
            "(layout may have changed — run `dd-agent diagnose` to inspect)"
        )
        return result

    result.level = max(levels)
    result.data.update(series_data)
    if text_level != Level.UNKNOWN:
        result.data["page_status"] = text_level.label_ja

    details = [d for d in (series_detail,) if d]
    if breakdown:
        top = " / ".join(f"{b['label']}({b['percent']}%)" for b in breakdown[:3])
        details.append(f"最多報告: {top}")
    result.detail = " ・ ".join(details) or f"ページ表示: {result.level.label_ja}"
    return result


# --- status sentence ---------------------------------------------------------


def _level_from_text(html: str) -> Level:
    text = _visible_text(html)
    if not text:
        return Level.UNKNOWN
    # Only the leading part of the page carries the verdict; further down
    # come user comments that routinely contain the word 障害 regardless of
    # the actual status.
    head = text[:1200]
    for pat in _OK_PATTERNS:
        if re.search(pat, head, re.IGNORECASE):
            return Level.OK
    for pat in _WARNING_PATTERNS:
        if re.search(pat, head, re.IGNORECASE):
            return Level.WARNING
    for pat in _OUTAGE_PATTERNS:
        if re.search(pat, head, re.IGNORECASE):
            return Level.OUTAGE
    return Level.UNKNOWN


def _visible_text(html: str) -> str:
    """Strip tags/scripts. Uses BeautifulSoup when available, regex if not."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    except ImportError:
        stripped = re.sub(
            r"<(script|style|noscript)\b.*?</\1>", " ", html, flags=re.S | re.I
        )
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", stripped)).strip()


# --- report series -----------------------------------------------------------

#: {"x": "2026-09-05 10:00:00", "y": 42} — the shape Downdetector's chart
#: payload has used; matched loosely so key order and spacing do not matter.
_XY_OBJ_RE = re.compile(
    r"\{\s*[\"']x[\"']\s*:\s*[\"']?(?P<x>[^,\"'}]+)[\"']?\s*,\s*"
    r"[\"']y[\"']\s*:\s*(?P<y>-?\d+(?:\.\d+)?)",
    re.I,
)
#: Highcharts numeric pairs: [[1757066400000, 42], ...]
_PAIR_RE = re.compile(r"\[\s*(?P<x>1[5-9]\d{11})\s*,\s*(?P<y>-?\d+(?:\.\d+)?)\s*\]")


def extract_series(html: str) -> list[float]:
    """Pull the report counts out of the page, trying several encodings.

    Returns counts in document order (oldest first), or ``[]`` if no
    strategy matched. Strategies are ordered most-specific first and the
    first one that yields a usable series wins.
    """
    for strategy in (_series_from_xy_objects, _series_from_pairs, _series_from_json_blob):
        try:
            series = strategy(html)
        except Exception as exc:  # a broken strategy must not kill the others
            log.debug("series strategy %s failed: %s", strategy.__name__, exc)
            continue
        if len(series) >= 4:
            return series
    return []


def _series_from_xy_objects(html: str) -> list[float]:
    return [float(m.group("y")) for m in _XY_OBJ_RE.finditer(html)]


def _series_from_pairs(html: str) -> list[float]:
    return [float(m.group("y")) for m in _PAIR_RE.finditer(html)]


def _series_from_json_blob(html: str) -> list[float]:
    """Look inside embedded JSON blobs for the longest numeric array.

    Covers the case where the chart is hydrated from a
    ``<script type="application/json">`` payload instead of inline JS.
    """
    best: list[float] = []
    for m in re.finditer(
        r"<script[^>]*type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
        html,
        re.S | re.I,
    ):
        try:
            blob = json.loads(m.group(1))
        except ValueError:
            continue
        for candidate in _walk_numeric_arrays(blob):
            if len(candidate) > len(best):
                best = candidate
    return best


def _walk_numeric_arrays(node, depth: int = 0):
    if depth > 8:
        return
    if isinstance(node, list):
        if node and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in node):
            yield [float(v) for v in node]
        else:
            for v in node:
                yield from _walk_numeric_arrays(v, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_numeric_arrays(v, depth + 1)


def _level_from_series(
    series: list[float],
    *,
    min_reports: int,
    warning_ratio: float,
    outage_ratio: float,
    baseline_floor: float,
) -> tuple[Level, str, dict]:
    """Score the series against its own baseline.

    The baseline is the median of the window rather than the mean, so an
    ongoing spike at the tail does not drag the "normal" level up with it.
    """
    if len(series) < 4:
        return Level.UNKNOWN, "", {}

    latest = series[-1]
    # The final bucket is often still filling, so also consider the previous
    # one and take the higher: a spike should not be missed just because the
    # newest bucket is 30 seconds old.
    current = max(series[-1], series[-2])
    baseline = max(statistics.median(series[:-2] or series), baseline_floor)
    ratio = current / baseline
    peak = max(series)

    data = {
        "reports_current": int(current),
        "reports_latest": int(latest),
        "reports_baseline": round(baseline, 2),
        "reports_peak": int(peak),
        "reports_ratio": round(ratio, 2),
        "series_points": len(series),
    }
    detail = f"報告数 {int(current):,}件 (平常時 {baseline:,.0f}件 / 比 {ratio:.1f}倍)"

    if current < min_reports:
        return Level.OK, detail, data
    if ratio >= outage_ratio:
        return Level.OUTAGE, detail, data
    if ratio >= warning_ratio:
        return Level.WARNING, detail, data
    return Level.OK, detail, data


# --- "most reported problems" breakdown --------------------------------------


def extract_breakdown(html: str) -> list[dict]:
    """Extract the "最も報告されている問題" percentages, if present."""
    out: list[dict] = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return out

    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("[class*=bar], [class*=Bar], li, div"):
        label_el = el.select_one("[class*=label], .text, span")
        text = el.get_text(" ", strip=True)
        m = re.search(r"(?P<label>[^\d%]{2,24}?)\s*(?P<pct>\d{1,3})\s*%", text)
        if not m:
            continue
        pct = int(m.group("pct"))
        label = (label_el.get_text(strip=True) if label_el else m.group("label")).strip()
        label = re.sub(r"\s*\d{1,3}\s*%$", "", label).strip()
        if not label or pct > 100:
            continue
        if any(o["label"] == label for o in out):
            continue
        out.append({"label": label, "percent": pct})
        if len(out) >= 5:
            break
    return out
