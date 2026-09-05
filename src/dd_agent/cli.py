"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import config as config_mod
from . import notify, runner
from . import state as state_mod
from . import summarize as summarize_mod
from .models import Level, ServiceReport, SourceResult
from .sources import downdetector, official

DEFAULT_STATE_PATH = ".dd-agent-state.json"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _webhook() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dd-agent",
        description="Downdetector と公式ステータスを監視し、障害を Slack に通知します。",
    )
    p.add_argument("-c", "--config", help="path to services.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="run one check cycle")
    chk.add_argument("--only", action="append", metavar="KEY", help="check only this service (repeatable)")
    chk.add_argument("--state", default=None, help=f"state file (default: {DEFAULT_STATE_PATH})")
    chk.add_argument("--dry-run", action="store_true", help="do everything except post to Slack")
    chk.add_argument("--force", action="store_true", help="notify even if already notified")
    chk.add_argument("--no-summary", action="store_true", help="skip the Claude summary")
    chk.add_argument("--no-state", action="store_true", help="do not read or write the state file")
    chk.add_argument("--json", action="store_true", help="emit machine-readable results on stdout")
    chk.add_argument("--github-summary", action="store_true", help="append a report to $GITHUB_STEP_SUMMARY")

    diag = sub.add_parser("diagnose", help="check whether scraping/parsing still works")
    diag.add_argument("--only", action="append", metavar="KEY")
    diag.add_argument("--dump-dir", help="write fetched Downdetector HTML here for inspection")

    ts = sub.add_parser("test-slack", help="post a sample notification to Slack")
    ts.add_argument("--service", default=None, help="service key to use in the sample")

    sub.add_parser("list", help="list configured services")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = config_mod.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2

    if args.command == "list":
        return _cmd_list(cfg)
    if args.command == "check":
        return _cmd_check(cfg, args)
    if args.command == "diagnose":
        return _cmd_diagnose(cfg, args)
    if args.command == "test-slack":
        return _cmd_test_slack(cfg, args)
    return 2


def _cmd_list(cfg) -> int:
    print(f"{'KEY':<12} {'NAME':<16} {'OFFICIAL':<12} DOWNDETECTOR")
    for s in cfg.services:
        mark = "" if s.enabled else " (disabled)"
        print(
            f"{s.key:<12} {s.name:<16} {s.official.get('kind', 'none'):<12} "
            f"{s.downdetector_url or '-'}{mark}"
        )
    print(
        f"\n通知しきい値: {cfg.notify_level.label_ja} 以上 / "
        f"再通知間隔: {cfg.reminder_hours}時間 / 復旧通知: {'有効' if cfg.notify_recovery else '無効'}"
    )
    return 0


def _cmd_check(cfg, args) -> int:
    if args.no_summary:
        cfg.summarize = False

    state_path = Path(args.state or os.environ.get("DD_STATE_PATH") or DEFAULT_STATE_PATH)
    state = state_mod.State() if args.no_state else state_mod.State.load(state_path)

    webhook = _webhook()
    if not webhook and not args.dry_run:
        print(
            "SLACK_WEBHOOK_URL が設定されていません。--dry-run で通知なし実行できます。",
            file=sys.stderr,
        )
        return 2

    try:
        outcomes = runner.run(
            cfg,
            state=state,
            webhook_url=webhook,
            only=args.only,
            dry_run=args.dry_run,
            force=args.force,
        )
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    if not args.no_state:
        state.save(state_path)

    if args.json:
        print(json.dumps([_outcome_dict(o) for o in outcomes], ensure_ascii=False, indent=2))
    else:
        print(runner.summarise_run(outcomes))
        for o in outcomes:
            if o.decision.notify:
                kind = o.decision.kind
                dest = "Slack へ送信" if o.delivered else ("dry-run" if args.dry_run else "送信失敗")
                print(f"\n--- {o.report.name} [{kind}] ({dest}) ---")
                print(o.summary or notify.template_summary(o.report))

    if args.github_summary:
        _write_github_summary(outcomes)

    return runner.exit_code(outcomes)


def _outcome_dict(o: runner.Outcome) -> dict:
    r = o.report
    return {
        "key": r.key,
        "name": r.name,
        "level": r.level.name,
        "level_ja": r.level.label_ja,
        "decision": o.decision.kind,
        "reason": o.decision.reason,
        "notified": o.delivered,
        "delivery_error": o.delivery_error,
        "summary": o.summary,
        "sources": {
            s.source: {
                "level": s.level.name,
                "detail": s.detail,
                "url": s.url,
                "error": s.error,
                "data": {k: v for k, v in s.data.items() if k != "html"},
                "incidents": [
                    {"title": i.title, "status": i.status, "impact": i.impact, "url": i.url}
                    for i in s.incidents
                ],
            }
            for s in r.sources
        },
    }


def _write_github_summary(outcomes: list[runner.Outcome]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Downdetector チェック結果",
        "",
        "| サービス | 判定 | Downdetector | 公式ステータス | 通知 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for o in outcomes:
        r = o.report
        dd = r.downdetector
        off = r.official
        dd_text = (dd.detail if dd and dd.ok else f"⚠️ {dd.error}" if dd else "-") or "-"
        off_text = (off.detail if off and off.ok else f"⚠️ {off.error}" if off else "-") or "-"
        notified = "送信" if o.delivered else (o.decision.kind if o.decision.notify else "-")
        lines.append(
            f"| {r.name} | {r.level.emoji} {r.level.label_ja} | "
            f"{_cell(dd_text)} | {_cell(off_text)} | {notified} |"
        )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:200]


def _cmd_diagnose(cfg, args) -> int:
    """Verify that each source still parses, and say precisely what broke.

    This is the command to run when notifications stop looking right:
    Downdetector layout changes are the expected failure mode of this
    agent, and this turns "it silently stopped working" into a specific,
    fixable report.
    """
    only = set(args.only or [])
    services = [s for s in cfg.services if not only or s.key in only]
    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for svc in services:
        print(f"\n=== {svc.name} ({svc.key}) ===")

        if svc.downdetector_url:
            res = downdetector.check(
                svc.key,
                svc.name,
                svc.downdetector_url,
                timeout=cfg.request_timeout,
                return_html=bool(dump_dir),
                **_threshold_kwargs(cfg, svc),
            )
            html = res.data.pop("html", None)
            if dump_dir and html:
                target = dump_dir / f"{svc.key}.html"
                target.write_text(html, encoding="utf-8")
                print(f"  HTML dumped: {target} ({len(html):,} bytes)")
            if res.ok:
                print(f"  Downdetector: OK  level={res.level.name}  {res.detail}")
                if "series_points" not in res.data:
                    print("    ⚠️  報告数の時系列を抽出できませんでした（ページ文言のみで判定）")
                print(f"    data: {json.dumps(res.data, ensure_ascii=False)}")
            else:
                failures += 1
                print(f"  Downdetector: FAILED  {res.error}")
        else:
            print("  Downdetector: 未設定")

        res = official.check(svc.official, timeout=cfg.request_timeout)
        if res.ok:
            print(
                f"  公式({svc.official.get('kind')}): OK  level={res.level.name}  "
                f"incidents={len(res.incidents)}  {res.detail}"
            )
        else:
            failures += 1
            print(f"  公式({svc.official.get('kind')}): FAILED  {res.error}")

    print(f"\n失敗したソース: {failures}")
    return 1 if failures else 0


def _threshold_kwargs(cfg, svc) -> dict:
    th = cfg.thresholds_for(svc)
    return {
        "min_reports": th.min_reports,
        "warning_ratio": th.warning_ratio,
        "outage_ratio": th.outage_ratio,
        "baseline_floor": th.baseline_floor,
    }


def _cmd_test_slack(cfg, args) -> int:
    webhook = _webhook()
    if not webhook:
        print("SLACK_WEBHOOK_URL が設定されていません。", file=sys.stderr)
        return 2

    svc = cfg.service(args.service) if args.service else cfg.services[0]
    if svc is None:
        print(f"不明なサービスキー: {args.service}", file=sys.stderr)
        return 2

    report = ServiceReport(
        key=svc.key,
        name=svc.name,
        downdetector=SourceResult(
            source="downdetector",
            level=Level.OUTAGE,
            url=svc.downdetector_url or "https://downdetector.jp/",
            detail="報告数 1,234件 (平常時 42件 / 比 29.4倍)",
            data={"reports_current": 1234, "reports_baseline": 42, "reports_ratio": 29.4},
        ),
        official=SourceResult(
            source="official",
            level=Level.OUTAGE,
            url="https://example.status.test/",
            detail="[major] これはテスト通知です",
        ),
    )
    payload = notify.build_payload(report, kind="new", summary="これは dd-agent の接続テストです。実際の障害ではありません。")
    try:
        notify.post(webhook, payload)
    except notify.SlackError as exc:
        print(f"送信失敗: {exc}", file=sys.stderr)
        return 1
    print("テスト通知を送信しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
