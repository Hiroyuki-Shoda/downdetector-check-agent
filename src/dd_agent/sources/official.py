"""Official status page sources.

Downdetector tells us that *users* are reporting trouble. These sources
tell us what the provider itself says is broken, which is what actually
belongs in a summary. Most providers run Atlassian Statuspage, which
exposes a stable documented JSON API — no scraping, no Cloudflare.

Every adapter has the same contract: take a config dict, return a
``SourceResult``, and never raise. An adapter that cannot reach its
endpoint sets ``error`` and leaves the level at ``UNKNOWN``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..http import FetchError, fetch, fetch_json
from ..models import Incident, Level, SourceResult

log = logging.getLogger(__name__)

SOURCE = "official"

#: Atlassian Statuspage severity indicators -> our levels.
_STATUSPAGE_LEVELS = {
    "none": Level.OK,
    "maintenance": Level.OK,
    "minor": Level.WARNING,
    "major": Level.OUTAGE,
    "critical": Level.OUTAGE,
}

#: Statuspage / Google impact values that we treat as full outages.
_MAJOR_IMPACTS = {"major", "critical", "SERVICE_OUTAGE", "SERVICE_DISRUPTION"}


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _trim(text: str, limit: int = 600) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Atlassian Statuspage ----------------------------------------------------


def check_statuspage(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """Handle any Atlassian Statuspage-hosted status page.

    ``base`` is the page root, e.g. ``https://status.openai.com``.
    """
    base = cfg["base"].rstrip("/")
    result = SourceResult(source=SOURCE, url=cfg.get("url") or base)

    try:
        summary = fetch_json(f"{base}/api/v2/summary.json", timeout=timeout)
    except FetchError as exc:
        result.error = str(exc)
        return result

    indicator = str((summary.get("status") or {}).get("indicator", "")).lower()
    description = (summary.get("status") or {}).get("description", "")
    result.level = _STATUSPAGE_LEVELS.get(indicator, Level.UNKNOWN)
    result.data["indicator"] = indicator
    result.data["description"] = description

    for raw in summary.get("incidents") or []:
        if str(raw.get("status") or "").lower() in ("resolved", "postmortem", "completed"):
            continue
        updates = raw.get("incident_updates") or []
        body = updates[0].get("body", "") if updates else ""
        result.incidents.append(
            Incident(
                id=str(raw.get("id") or ""),
                title=_trim(raw.get("name", ""), 200),
                status=str(raw.get("status") or ""),
                impact=str(raw.get("impact") or ""),
                body=_trim(body),
                url=raw.get("shortlink") or result.url,
                updated_at=_parse_ts(raw.get("updated_at") or raw.get("created_at")),
            )
        )
        if str(raw.get("impact") or "").lower() in _MAJOR_IMPACTS:
            result.level = max(result.level, Level.OUTAGE)

    # An unresolved incident with an "all clear" indicator still deserves a
    # warning — providers routinely lag on flipping the top-level banner.
    if result.incidents and result.level == Level.OK:
        result.level = Level.WARNING

    result.detail = _describe(result, description)
    return result


# --- Slack (custom API, not Statuspage) --------------------------------------


def check_slack(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    url = cfg.get("api", "https://status.slack.com/api/v2.0.0/current")
    result = SourceResult(source=SOURCE, url=cfg.get("url", "https://status.slack.com/"))
    try:
        data = fetch_json(url, timeout=timeout)
    except FetchError as exc:
        result.error = str(exc)
        return result

    status = str(data.get("status") or "").lower()
    result.data["indicator"] = status
    active = data.get("active_incidents") or []

    for raw in active:
        if str(raw.get("type") or "").lower() == "maintenance":
            continue
        notes = raw.get("notes") or []
        body = notes[-1].get("body", "") if notes else ""
        services = ", ".join(raw.get("services") or [])
        result.incidents.append(
            Incident(
                id=str(raw.get("id") or ""),
                title=_trim(raw.get("title", ""), 200),
                status=str(raw.get("status") or ""),
                impact=str(raw.get("type") or ""),
                body=_trim(f"{body} (影響: {services})" if services else body),
                url=raw.get("url") or result.url,
                updated_at=_parse_ts(raw.get("date_updated") or raw.get("date_created")),
            )
        )

    if result.incidents:
        # Slack does not grade severity; treat any active incident as an
        # outage when it names many services, otherwise a warning.
        result.level = Level.OUTAGE if status == "active" else Level.WARNING
    elif status == "ok":
        result.level = Level.OK

    result.detail = _describe(result, "All Systems Operational" if status == "ok" else status)
    return result


# --- Google (Workspace / Cloud dashboards) -----------------------------------


def check_google(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """Google's status dashboards publish a flat JSON array of incidents.

    ``products`` filters that array down to the products we care about,
    since one dashboard covers all of Workspace (or all of Cloud).
    """
    api = cfg["api"]
    products = [p.lower() for p in cfg.get("products", [])]
    result = SourceResult(source=SOURCE, url=cfg.get("url", api))

    try:
        data = fetch_json(api, timeout=timeout)
    except FetchError as exc:
        result.error = str(exc)
        return result

    if not isinstance(data, list):
        result.error = f"unexpected payload shape from {api}"
        return result

    result.level = Level.OK
    for raw in data:
        if raw.get("end"):  # resolved incidents carry an end timestamp
            continue
        names = [str(raw.get("service_name") or "")]
        names += [str(p.get("title") or "") for p in raw.get("affected_products") or []]
        blob = " ".join(names).lower()
        if products and not any(p in blob for p in products):
            continue

        update = raw.get("most_recent_update") or {}
        impact = str(raw.get("status_impact") or "")
        # `uri` may be absent, null, relative or absolute — normalise all four.
        uri = str(raw.get("uri") or "")
        if uri.startswith("/"):
            uri = "https://www.google.com" + uri
        result.incidents.append(
            Incident(
                id=str(raw.get("id") or ""),
                title=_trim(raw.get("external_desc", "") or raw.get("service_name", ""), 200),
                status=str(update.get("status") or "active"),
                impact=impact,
                body=_trim(update.get("text", "")),
                url=uri or result.url,
                updated_at=_parse_ts(update.get("modified") or raw.get("begin")),
            )
        )
        result.level = max(
            result.level,
            Level.OUTAGE if impact in _MAJOR_IMPACTS else Level.WARNING,
        )

    result.detail = _describe(result, "報告されている障害はありません")
    return result


# --- AWS ---------------------------------------------------------------------


def check_aws(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """AWS Health dashboard.

    The modern endpoint returns a nested object keyed by region/service; the
    legacy ``data.json`` returns ``{"current": [...], "archive": [...]}``.
    Both are tried so a change on either side is survivable.
    """
    result = SourceResult(source=SOURCE, url=cfg.get("url", "https://health.aws.amazon.com/health/status"))
    regions = [r.lower() for r in cfg.get("regions", [])]

    last_error: str | None = None
    for api in cfg.get("apis", []):
        try:
            data = fetch_json(api, timeout=timeout)
        except FetchError as exc:
            last_error = str(exc)
            continue

        events = _aws_events(data)
        result.level = Level.OK
        for ev in events:
            blob = " ".join(
                str(ev.get(k) or "") for k in ("service", "region", "event_log_id", "summary")
            ).lower()
            if regions and not any(r in blob for r in regions):
                continue
            impact = str(ev.get("status_code") or ev.get("impact") or "").lower()
            level = Level.OUTAGE if "outage" in impact or "disruption" in impact else Level.WARNING
            result.incidents.append(
                Incident(
                    id=str(ev.get("event_arn") or ev.get("event_log_id") or ev.get("summary") or "")[:120],
                    title=_trim(
                        f"{ev.get('service_name') or ev.get('service') or 'AWS'} "
                        f"({ev.get('region') or '-'})",
                        200,
                    ),
                    status=str(ev.get("event_status") or "open"),
                    impact=impact,
                    body=_trim(ev.get("summary") or ev.get("latestDescription") or ""),
                    url=result.url,
                    updated_at=_parse_ts(ev.get("date") or ev.get("last_updated_time")),
                )
            )
            result.level = max(result.level, level)
        result.detail = _describe(result, "報告されている障害はありません")
        return result

    result.error = last_error or "no AWS status endpoint configured"
    return result


def _aws_events(data) -> list[dict]:
    """Flatten either AWS payload shape into a list of event dicts."""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("current"), list):
        return [e for e in data["current"] if isinstance(e, dict)]

    out: list[dict] = []
    for value in data.values():
        if isinstance(value, list):
            out += [e for e in value if isinstance(e, dict)]
        elif isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, list):
                    out += [e for e in inner if isinstance(e, dict)]
    # Open events only: AWS marks closed ones with an end/resolved time.
    return [e for e in out if not (e.get("end_time") or e.get("endTime"))]


# --- Salesforce --------------------------------------------------------------


#: Salesforce Trust per-instance status enum -> level. Maintenance and
#: informational states are deliberately OK: they are not outages.
_SALESFORCE_LEVELS = {
    "OK": Level.OK,
    "MAINTENANCE": Level.OK,
    "INFORMATIONAL": Level.OK,
    "MINOR_INCIDENT": Level.WARNING,
    "MAJOR_INCIDENT": Level.OUTAGE,
}


def check_salesforce(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """Salesforce Trust status API (``/v1/instances/status``).

    The payload is one entry per Salesforce instance (``AP15``, ``NA224``,
    ...). Salesforce runs hundreds of them, so without an ``instances``
    filter *something* is nearly always degraded somewhere. Configure the
    instance your org actually runs on to make this signal meaningful; with
    no filter we fall back to reporting only the worst status seen and name
    the affected instances.
    """
    api = cfg["api"]
    wanted = {i.upper() for i in cfg.get("instances", [])}
    result = SourceResult(source=SOURCE, url=cfg.get("url", "https://status.salesforce.com/"))

    try:
        data = fetch_json(api, timeout=timeout)
    except FetchError as exc:
        result.error = str(exc)
        return result

    entries = data if isinstance(data, list) else data.get("instances") or []
    if not entries:
        result.error = f"no instance entries in payload from {api}"
        return result

    result.level = Level.OK
    affected: list[str] = []
    seen: set[str] = set()

    for inst in entries:
        if not isinstance(inst, dict):
            continue
        key = str(inst.get("key") or "").upper()
        if wanted and key not in wanted:
            continue

        status = str(inst.get("status") or "").upper()
        # Strip the _CORE / _NONCORE suffix before mapping.
        base = status.replace("_NONCORE", "").replace("_CORE", "")
        level = _SALESFORCE_LEVELS.get(base, Level.UNKNOWN)
        if level in (Level.UNKNOWN, Level.OK):
            continue

        result.level = max(result.level, level)
        affected.append(key)

        for raw in inst.get("Incidents") or inst.get("incidents") or []:
            if not isinstance(raw, dict) or raw.get("endTime"):
                continue
            inc_id = str(raw.get("id") or raw.get("incidentId") or "")
            if inc_id in seen:
                continue
            seen.add(inc_id)

            impacts = raw.get("IncidentImpacts") or raw.get("incidentImpacts") or []
            impact = str(impacts[0].get("severity") or "") if impacts else base
            body = next(
                (
                    ev.get("message", "")
                    for ev in raw.get("IncidentEvents") or raw.get("incidentEvents") or []
                    if ev.get("message")
                ),
                "",
            )
            result.incidents.append(
                Incident(
                    id=inc_id,
                    title=_trim(raw.get("message") or f"{base} on {key}", 200),
                    status="active",
                    impact=impact,
                    body=_trim(_strip_html(body)),
                    url=f"https://status.salesforce.com/incidents/{inc_id}" if inc_id else result.url,
                    updated_at=_parse_ts(raw.get("updatedAt") or raw.get("createdAt")),
                )
            )

    if wanted and not any(
        str(i.get("key", "")).upper() in wanted for i in entries if isinstance(i, dict)
    ):
        result.error = f"none of the configured instances {sorted(wanted)} appear in the payload"
        return result

    if affected:
        result.data["affected_instances"] = affected[:20]
        note = f"影響インスタンス: {', '.join(affected[:8])}"
        if len(affected) > 8:
            note += f" 他{len(affected) - 8}件"
        result.detail = _describe(result, note)
        if not result.incidents:
            result.detail = note
    else:
        result.detail = "報告されている障害はありません"
    return result


# --- Statuspal (Nulab / Backlog) ---------------------------------------------

_STATUSPAL_LEVELS = {
    "major": Level.OUTAGE,
    "minor": Level.WARNING,
    "scheduled": Level.OK,
    "": Level.OK,
}


def check_statuspal(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """Statuspal-hosted status page (Nulab / Backlog).

    ``/status`` gives the page-level verdict cheaply; ``/summary`` is only
    fetched when something is wrong, so a healthy poll costs one request.
    ``services`` optionally narrows to specific services — Nulab's page
    covers Backlog Japan, Backlog Global, Cacoo and more on one page.
    """
    base = cfg["api"].rstrip("/")
    wanted = [s.lower() for s in cfg.get("services", [])]
    result = SourceResult(source=SOURCE, url=cfg.get("url", "https://status.nulab.com/"))

    try:
        data = fetch_json(f"{base}/status", timeout=timeout)
    except FetchError as exc:
        result.error = str(exc)
        return result

    page = data.get("status_page") if isinstance(data, dict) else None
    if not isinstance(page, dict):
        result.error = f"unexpected Statuspal payload from {base}/status"
        return result

    incident_type = str(page.get("current_incident_type") or "").lower()
    result.data["indicator"] = incident_type or "none"
    result.level = _STATUSPAL_LEVELS.get(incident_type, Level.UNKNOWN)

    if result.level <= Level.OK:
        result.detail = "報告されている障害はありません"
        return result

    try:
        summary = fetch_json(f"{base}/summary", timeout=timeout)
    except FetchError as exc:
        # The page-level verdict already stands; we just lack the detail.
        result.detail = f"{incident_type} incident (詳細取得失敗: {exc})"
        return result

    matched_service_levels: list[Level] = []
    for svc in (summary.get("services") if isinstance(summary, dict) else None) or []:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name") or "")
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        svc_type = str(svc.get("current_incident_type") or "").lower()
        # Record every matched service, including healthy ones: a matched
        # service reporting no incident is exactly the evidence needed to
        # rule out the page-level incident being ours.
        matched_service_levels.append(_STATUSPAL_LEVELS.get(svc_type, Level.WARNING))
        result.data.setdefault("services", []).append({"name": name, "status": svc_type or "ok"})

    for raw in (summary.get("incidents") if isinstance(summary, dict) else None) or []:
        if not isinstance(raw, dict) or raw.get("ends_at"):
            continue
        services = ", ".join(
            str(s.get("name") or "") for s in raw.get("services") or [] if isinstance(s, dict)
        )
        if wanted and services and not any(w in services.lower() for w in wanted):
            continue
        result.incidents.append(
            Incident(
                id=str(raw.get("id") or ""),
                title=_trim(raw.get("title", ""), 200),
                status="active",
                impact=str(raw.get("type") or incident_type),
                body=_trim(_strip_html(raw.get("description") or "")
                           + (f" (影響: {services})" if services else "")),
                url=raw.get("url") or result.url,
                updated_at=_parse_ts(raw.get("updated_at") or raw.get("starts_at")),
            )
        )

    # If we filtered to specific services and none of them are affected, the
    # page-level incident is about something else on the same status page.
    if wanted and matched_service_levels and max(matched_service_levels) <= Level.OK:
        result.level = Level.OK
        result.detail = f"他サービスで {incident_type} 障害中（対象サービスは正常）"
        return result

    result.detail = _describe(result, f"{incident_type} incident")
    return result


def _strip_html(text: str) -> str:
    """Strip tags from incident bodies — several providers embed HTML."""
    import re

    return re.sub(r"<[^>]+>", " ", text or "")


# --- RSS / Atom feeds --------------------------------------------------------


def check_feed(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """Read an RSS/Atom outage feed.

    For providers that publish incidents as a feed rather than JSON — the
    documented fallback for the Google dashboards, whose JSON paths are
    undocumented while their RSS feed is the officially recommended way to
    be notified of outages. Entries only count as current if they fall
    inside ``recent_hours``, since these feeds keep history indefinitely.
    """
    api = cfg["api"]
    recent_hours = float(cfg.get("recent_hours", 24))
    result = SourceResult(source=SOURCE, url=cfg.get("url", api))

    try:
        resp = fetch(
            api,
            timeout=timeout,
            impersonate=False,
            accept="application/rss+xml, application/xml, text/xml",
        )
    except FetchError as exc:
        result.error = str(exc)
        return result

    entries = _parse_feed(resp.text)
    now = datetime.now(timezone.utc)
    result.level = Level.OK
    for title, body, link, published in entries:
        if published is not None:
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if (now - published).total_seconds() / 3600 > recent_hours:
                continue
        result.incidents.append(
            Incident(
                id=link or title,
                title=_trim(title, 200),
                status="active",
                impact="unknown",
                body=_trim(_strip_html(body)),
                url=link or result.url,
                updated_at=published,
            )
        )
        result.level = max(result.level, Level.WARNING)

    result.detail = _describe(result, "直近の障害情報はありません")
    return result


def _parse_feed(xml: str) -> list[tuple[str, str, str, datetime | None]]:
    import re
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return []

    def txt(node, *names) -> str:
        for name in names:
            for el in node.iter():
                if el.tag.split("}")[-1] == name and (el.text or "").strip():
                    return re.sub(r"<[^>]+>", " ", el.text).strip()
        return ""

    out = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = txt(item, "title")
        if not title:
            continue
        link = ""
        for el in item.iter():
            if el.tag.split("}")[-1] == "link":
                link = (el.get("href") or el.text or "").strip()
                if link:
                    break
        out.append(
            (
                title,
                txt(item, "description", "summary", "content"),
                link,
                _parse_rfc822(txt(item, "pubDate", "updated", "published")),
            )
        )
    return out


def _parse_rfc822(value: str) -> datetime | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return _parse_ts(value)


# --- shared ------------------------------------------------------------------


def _describe(result: SourceResult, fallback: str) -> str:
    if result.incidents:
        head = result.incidents[0]
        extra = f" 他{len(result.incidents) - 1}件" if len(result.incidents) > 1 else ""
        impact = f"[{head.impact}] " if head.impact and head.impact != "unknown" else ""
        return f"{impact}{head.title}{extra}"
    return fallback


def check_none(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """For services with no official machine-readable status page (e.g. X)."""
    return SourceResult(
        source=SOURCE,
        url=cfg.get("url", ""),
        level=Level.UNKNOWN,
        detail="公式ステータスページなし",
        error=None,
    )


ADAPTERS = {
    "statuspage": check_statuspage,  # Atlassian Statuspage (OpenAI, Claude)
    "statuspal": check_statuspal,  # Statuspal (Nulab / Backlog)
    "slack": check_slack,
    "google": check_google,
    "aws": check_aws,
    "salesforce": check_salesforce,
    "feed": check_feed,  # RSS / Atom
    "none": check_none,
}


def check(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    """Dispatch to the adapter named by ``cfg['kind']``."""
    kind = cfg.get("kind", "statuspage")
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        return SourceResult(
            source=SOURCE, url=cfg.get("url", ""), error=f"unknown official source kind: {kind}"
        )
    try:
        return adapter(cfg, timeout=timeout)
    except Exception as exc:  # adapter bug or unexpected payload
        log.exception("official adapter %s crashed", kind)
        return SourceResult(source=SOURCE, url=cfg.get("url", ""), error=f"{kind} adapter error: {exc}")
