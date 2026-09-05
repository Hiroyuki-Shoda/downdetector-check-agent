"""Slack delivery via Incoming Webhook."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .models import Level, ServiceReport

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9), "JST")

_KIND_HEADINGS = {
    "new": "障害検知",
    "escalation": "障害レベル上昇",
    "update": "障害情報 更新",
    "reminder": "障害継続中",
    "recovery": "復旧",
}


class SlackError(RuntimeError):
    pass


def template_summary(report: ServiceReport) -> str:
    """Deterministic fallback used when the Claude summary is unavailable."""
    parts: list[str] = []
    off = report.official
    if off and off.ok and off.incidents:
        inc = off.incidents[0]
        impact = f"[{inc.impact}] " if inc.impact and inc.impact != "unknown" else ""
        parts.append(f"公式ステータス: {impact}{inc.title}")
        if inc.body:
            parts.append(inc.body[:300])
    elif off and off.ok and off.level == Level.OK:
        parts.append("公式ステータスでは障害は報告されていません（利用者からの報告のみ）。")

    dd = report.downdetector
    if dd and dd.ok and dd.detail:
        parts.append(f"Downdetector: {dd.detail}")

    return "\n".join(parts) or "詳細情報を取得できませんでした。リンク先を確認してください。"


def build_blocks(
    report: ServiceReport,
    *,
    kind: str,
    summary: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Build the Slack Block Kit payload for one service notification."""
    now = (now or datetime.now(timezone.utc)).astimezone(JST)
    level = report.level
    heading = _KIND_HEADINGS.get(kind, "障害情報")
    emoji = "✅" if kind == "recovery" else level.emoji

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {heading}: {report.name}", "emoji": True},
        }
    ]

    if kind == "recovery":
        body = f"*{report.name}* は正常な状態に戻りました。"
    else:
        body = summary or template_summary(report)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})

    fields = []
    dd = report.downdetector
    if dd:
        value = dd.detail if dd.ok else f"_取得失敗: {dd.error}_"
        fields.append({"type": "mrkdwn", "text": f"*Downdetector*\n{value or '—'}"})
    off = report.official
    if off:
        if not off.ok:
            value = f"_取得失敗: {off.error}_"
        elif off.level == Level.UNKNOWN:
            value = "公式ステータスなし"
        else:
            value = off.detail or "—"
        fields.append({"type": "mrkdwn", "text": f"*公式ステータス*\n{value}"})
    if fields:
        blocks.append({"type": "section", "fields": fields})

    # Deduplicate by URL, not by rendered link: an incident shortlink is
    # often the status page root, which would otherwise appear twice under
    # two different labels.
    links: list[str] = []
    seen_urls: set[str] = set()

    def add_link(url: str, label: str) -> None:
        if url and url not in seen_urls:
            seen_urls.add(url)
            links.append(f"<{url}|{label}>")

    if dd:
        add_link(dd.url, "Downdetector")
    if off:
        add_link(off.url, "公式ステータス")
    for inc in report.incidents[:3]:
        add_link(inc.url, inc.title[:40])

    context_parts = [f"判定: {level.label_ja}", now.strftime("%Y-%m-%d %H:%M JST")]
    if links:
        context_parts.append(" / ".join(links))
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " ｜ ".join(context_parts)}],
        }
    )
    return blocks


def build_payload(
    report: ServiceReport,
    *,
    kind: str,
    summary: str | None = None,
    now: datetime | None = None,
) -> dict:
    emoji = "✅" if kind == "recovery" else report.level.emoji
    heading = _KIND_HEADINGS.get(kind, "障害情報")
    return {
        # `text` is the notification/fallback string shown in the sidebar and
        # on mobile push, so it must stand alone without the blocks.
        "text": f"{emoji} {heading}: {report.name} ({report.level.label_ja})",
        "blocks": build_blocks(report, kind=kind, summary=summary, now=now),
    }


def post(webhook_url: str, payload: dict, *, timeout: float = 15.0, retries: int = 2) -> None:
    """POST ``payload`` to a Slack Incoming Webhook."""
    if not webhook_url:
        raise SlackError("no Slack webhook URL configured (set SLACK_WEBHOOK_URL)")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace").strip()
                if resp.status == 200 and text == "ok":
                    return
                raise SlackError(f"unexpected Slack response: HTTP {resp.status} {text[:200]}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            # 4xx from Slack means a bad payload or a revoked webhook —
            # retrying cannot help, so fail loudly.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise SlackError(f"Slack rejected the message: HTTP {exc.code} {detail}") from exc
            last = SlackError(f"HTTP {exc.code} {detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc

        if attempt < retries:
            import time

            time.sleep(2**attempt)

    raise SlackError(f"could not deliver to Slack: {last}")
