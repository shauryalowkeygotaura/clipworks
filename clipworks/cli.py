"""`clipworks` command line.

  clipworks doctor                    what is live, what is dry, what is missing
  clipworks cycle                     one full unattended cycle (cron this)
  clipworks loop --every 30           run cycles forever (no cron needed)
  clipworks status                    today's plans, pending submissions, earnings
  clipworks campaign list|check|approve <file>
  clipworks brief <file|->            draft a campaign from a pasted brief
  clipworks whop pending|done <post_id>|sync
  clipworks ledger set-url <post_id> <url> | set-payout <post_id> <usd>
  clipworks reconcile                 refresh view counts now
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import briefs
from .campaign import CampaignError
from .campaign import load as load_campaign
from .config import Settings
from .pipeline import Deps, Locked, Report, cycle, load_active, reconcile_step, submit_to_campaign
from .whop import MANUAL, pending_manual
from .whop import sync as whop_sync


def _deps() -> Deps:
    return Deps.from_settings(Settings.from_env())


def cmd_doctor(args) -> int:
    s = Settings.from_env()
    deps = Deps.from_settings(s)
    ok = True
    print(f"home: {s.home}")
    print(f"mode: {'DRY RUN (forced)' if s.dry_run else 'live where credentials exist'}\n")

    print("tools")
    for tool in ("yt-dlp", "uv", "ffmpeg"):
        found = shutil.which(tool)
        ok &= bool(found)
        print(f"  {'ok ' if found else 'MISSING'} {tool}")
    if s.publikclip_dir and s.publikclip_dir.exists():
        print(f"  ok  publikclip at {s.publikclip_dir}")
    else:
        ok = False
        print(f"  OFF publikclip (set PUBLIKCLIP_DIR): generation is disabled")

    print("\nplatforms")
    for name, poster in deps.posters.items():
        missing = poster.missing_config()
        note = ""
        if name == "tiktok" and not missing and getattr(poster, "mode", "") == "inbox":
            note = "  (inbox mode: drafts need a tap in the app)"
        print(f"  {'LIVE' if not missing else 'dry '} {name}{note}" + (f"  missing: {', '.join(missing)}" if missing else ""))

    print("\nintegrations")
    print(f"  {'on ' if s.discord_webhook else 'off'} discord notifications (CLIPWORKS_DISCORD_WEBHOOK_URL)")
    print(f"  {'on ' if deps.brief_reader.available() else 'off'} discord brief intake (CLIPWORKS_DISCORD_BOT_TOKEN + CLIPWORKS_DISCORD_BRIEF_CHANNEL_IDS)")
    print(f"  {'on ' if s.whop_user_token else 'off'} whop bounty API (CLIPWORKS_WHOP_USER_TOKEN); Content Rewards is always manual")

    print("\ncampaigns")
    report = Report()
    active = load_active(s, report)
    for c in active:
        print(f"  active  {c.slug}: {', '.join(c.platforms)}  cap {c.rules.per_clipper_daily_cap}/day  [{c.source_kind}]")
    for a in report.alerts:
        ok = False
        print(f"  INVALID {a}")
    drafts = sorted(s.drafts_dir.glob("*.y*ml")) if s.drafts_dir.exists() else []
    for d in drafts:
        print(f"  draft   {d.name}")
    if not active:
        ok = False
        print("  none active: put a campaign YAML in campaigns/active/ (see campaigns/example.yaml)")
    return 0 if ok else 1


def cmd_cycle(args) -> int:
    try:
        report = cycle(_deps())
    except Locked as err:
        print(f"skipped: {err}", file=sys.stderr)
        return 0
    for line in report.lines:
        print(line)
    for a in report.alerts:
        print(f"ALERT: {a}")
    return 0


def cmd_loop(args) -> int:
    while True:
        cmd_cycle(args)
        time.sleep(max(60, args.every * 60))


def cmd_status(args) -> int:
    deps = _deps()
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).date().isoformat()
    print(f"plans for {day} (UTC)")
    for slug, entries in sorted(deps.store.plans_for_day(day).items()):
        for e in entries:
            print(f"  {e['scheduled_for'][11:16]}  {slug:<20} {e['platform']:<9} {e['status']:<13} {e['post_id']}  {e.get('detail', '')[:60]}")
    manual = pending_manual(deps.ledger)
    if manual:
        print(f"\nwaiting for manual Whop submission ({len(manual)})")
        for r in manual:
            print(f"  {r.post_id}  {r.url}")
    rows = [r for r in deps.ledger.current().values() if r.succeeded]
    views = sum(r.views or 0 for r in rows)
    paid = sum(r.payout_usd or 0 for r in rows)
    rate = deps.ledger.realised_rate()
    print(f"\nledger: {len(rows)} live posts, {views:,} views, ${paid:,.2f} paid"
          + (f", realised ${rate:.2f}/1k views" if rate is not None else ", no payouts recorded yet"))
    q = deps.store.quarantined()
    if q:
        print(f"quarantine: {len(q)} clip/platform pairs blocked by compliance (runs/quarantine.jsonl)")
    return 0


def cmd_campaign(args) -> int:
    s = Settings.from_env()
    if args.action == "list":
        report = Report()
        for c in load_active(s, report):
            print(f"{c.slug}\t{','.join(c.platforms)}\t{c.source_kind}")
        for a in report.alerts:
            print(f"INVALID: {a}")
        return 0
    path = Path(args.file)
    try:
        c = load_campaign(path)
    except CampaignError as err:
        print(f"not valid: {err}", file=sys.stderr)
        if path.exists() and "TODO" in path.read_text(encoding="utf-8", errors="replace"):
            print("the draft still has TODO fields: fill them from the brief at the top of the file", file=sys.stderr)
        return 1
    if args.action == "check":
        print(f"ok: {c.slug} -> {', '.join(c.platforms)}, {c.rules.min_duration_sec}-{c.rules.max_duration_sec}s, "
              f"cap {c.rules.per_clipper_daily_cap}/day, {len(c.allowed_sources)} source(s)")
        return 0
    # approve
    s.active_dir.mkdir(parents=True, exist_ok=True)
    dest = s.active_dir / f"{c.slug}.yaml"
    if dest.exists() and dest.resolve() != path.resolve():
        print(f"refusing: {dest} already exists. Remove it first if this replaces it.", file=sys.stderr)
        return 1
    shutil.move(str(path), dest)
    print(f"active: {dest}")
    return 0


def cmd_brief(args) -> int:
    s = Settings.from_env()
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    draft = briefs.extract(text, slug=args.slug)
    s.drafts_dir.mkdir(parents=True, exist_ok=True)
    out = s.drafts_dir / f"{draft.data['slug']}.yaml"
    out.write_text(briefs.render(draft, text), encoding="utf-8")
    print(f"draft: {out}")
    for m in draft.missing:
        print(f"  MISSING {m}")
    for n in draft.notes:
        print(f"  CHECK   {n}")
    return 0


def cmd_whop(args) -> int:
    deps = _deps()
    if args.action == "pending":
        for r in pending_manual(deps.ledger):
            print(f"{r.post_id}\t{r.campaign}\t{r.url}")
        return 0
    if args.action == "sync":
        for r in whop_sync(deps.ledger, deps.whop):
            print(f"{r.post_id}\t{r.whop_status}\t{r.denial_reason or ''}")
        return 0
    rec = deps.ledger.current().get(args.post_id)
    if rec is None:
        print(f"unknown post {args.post_id}", file=sys.stderr)
        return 1
    if rec.whop_status != MANUAL:
        print(f"{args.post_id} is not waiting for manual submission (status {rec.whop_status})", file=sys.stderr)
        return 1
    deps.ledger.update(args.post_id, whop_status="submitted_manual")
    print(f"marked {args.post_id} as submitted")
    return 0


def cmd_ledger(args) -> int:
    deps = _deps()
    rec = deps.ledger.current().get(args.post_id)
    if rec is None:
        print(f"unknown post {args.post_id}", file=sys.stderr)
        return 1
    if args.action == "set-payout":
        deps.ledger.update(args.post_id, payout_usd=float(args.value))
        print(f"payout recorded: {args.post_id} ${float(args.value):.2f}")
        return 0
    # set-url: a TikTok draft got published, or a crashed upload was found live.
    if not args.value.startswith("https://"):
        print("url must start with https://", file=sys.stderr)
        return 1
    rec = deps.ledger.update(args.post_id, url=args.value)
    report = Report()
    s = deps.settings
    campaigns = {c.slug: c for c in load_active(s, report)}
    c = campaigns.get(rec.campaign)
    if c is not None and not rec.whop_status:
        submit_to_campaign(deps, c, rec, report)
    for line in report.lines + report.alerts:
        print(line)
    return 0


def cmd_reconcile(args) -> int:
    deps = _deps()
    state = deps.store.state()
    report = Report()
    n = reconcile_step(deps, state, report, force=True)
    deps.store.save_state(state)
    print(f"updated {n} posts")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipworks", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("cycle").set_defaults(fn=cmd_cycle)
    p = sub.add_parser("loop")
    p.add_argument("--every", type=int, default=30, help="minutes between cycles (min 1)")
    p.set_defaults(fn=cmd_loop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("campaign")
    p.add_argument("action", choices=["list", "check", "approve"])
    p.add_argument("file", nargs="?")
    p.set_defaults(fn=cmd_campaign)

    p = sub.add_parser("brief")
    p.add_argument("file", help="brief text file, or - for stdin")
    p.add_argument("--slug")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("whop")
    p.add_argument("action", choices=["pending", "done", "sync"])
    p.add_argument("post_id", nargs="?")
    p.set_defaults(fn=cmd_whop)

    p = sub.add_parser("ledger")
    p.add_argument("action", choices=["set-url", "set-payout"])
    p.add_argument("post_id")
    p.add_argument("value")
    p.set_defaults(fn=cmd_ledger)

    args = ap.parse_args(argv)
    if args.cmd == "campaign" and args.action != "list" and not args.file:
        ap.error("campaign check/approve needs a file")
    if args.cmd == "whop" and args.action == "done" and not args.post_id:
        ap.error("whop done needs a post_id")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
