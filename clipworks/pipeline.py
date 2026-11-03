"""One unattended cycle: briefs in, clips generated, posted, submitted, paid.

Run it from cron / Task Scheduler every 20-30 minutes. Each cycle is
idempotent and cheap when there is nothing to do, so the schedule is the
only knob: a cycle never "catches up" by posting a burst.

Order inside a cycle, and why:
  1. Brief intake (Discord)  -> drafts only, never live campaigns
  2. Generation (publikclip) -> at most N source videos, resume before new
  3. Planning                -> once per campaign per UTC day, persisted
  4. Posting                 -> due jobs only, at most one per platform
  5. Whop                    -> submit new URLs, pull approval outcomes
  6. Reconcile               -> view counts, once per UTC day
"""
from __future__ import annotations

import os
import time
import traceback
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import briefs as briefs_mod
from . import caption as caption_mod
from . import compliance, generate, scheduler, sources
from .campaign import CampaignError
from .campaign import load as load_campaign
from .config import Settings
from .discord import BriefReader, Notifier
from .http import Http
from .ledger import Ledger, PostRecord, post_id_for
from .models import DEFAULT_POSTS_PER_DAY, Campaign, PostJob, RenderedClip
from .posters import build_all
from .posters.base import Poster
from .store import Store
from .whop import MANUAL, WhopClient
from .whop import sync as whop_sync

LOCK_STALE_SEC = 6 * 3600
CAP_REASON = "daily cap reached"


@dataclass
class Deps:
    settings: Settings
    store: Store
    ledger: Ledger
    posters: dict[str, Poster]
    notifier: Notifier
    whop: WhopClient
    brief_reader: BriefReader
    run_publikclip: Callable[..., generate.RunResult] = generate.run
    resume_publikclip: Callable[..., generate.RunResult] = generate.resume
    resolve: Callable[..., list[tuple[str, str]]] = sources.resolve_with_origin
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Deps":
        http = Http()
        return cls(
            settings=settings,
            store=Store(settings.runs_dir),
            ledger=Ledger(settings.runs_dir / "ledger.jsonl"),
            posters=build_all(http, settings.env, state_dir=settings.runs_dir),
            notifier=Notifier(http, settings.discord_webhook),
            whop=WhopClient(http, settings.whop_user_token),
            brief_reader=BriefReader(http, settings.discord_bot_token, settings.discord_brief_channels),
        )


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def alert(self, msg: str) -> None:
        """Something the operator must act on. Always sent to Discord."""
        self.alerts.append(msg)


# --- locking ----------------------------------------------------------------


class Locked(RuntimeError):
    pass


class CycleLock:
    """Portable single-runner lock (no fcntl, so it works on Windows too).

    A publikclip run can outlast the cron interval; two cycles at once would
    plan and post the same clip twice. A lock older than LOCK_STALE_SEC is
    treated as left behind by a killed process.
    """

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - self.path.stat().st_mtime
            if age < LOCK_STALE_SEC:
                raise Locked(f"another cycle holds {self.path} ({int(age)}s old)") from None
            self.path.unlink()
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# --- campaigns --------------------------------------------------------------


def load_active(settings: Settings, report: Report) -> list[Campaign]:
    out: list[Campaign] = []
    if not settings.active_dir.exists():
        return out
    for path in sorted(settings.active_dir.glob("*.y*ml")):
        try:
            c = load_campaign(path)
        except CampaignError as err:
            report.alert(f"campaign {path.name} is invalid and was skipped: {err}")
            continue
        stray = caption_mod.unresolved_placeholders(c.rules.caption_template)
        if stray:
            report.alert(f"campaign {c.slug}: caption_template uses unknown placeholders {stray}")
        out.append(c)
    return out


# --- 1. brief intake --------------------------------------------------------


def intake_briefs(deps: Deps, state: dict, report: Report) -> None:
    reader = deps.brief_reader
    if not reader.available():
        return
    cursors = state.setdefault("discord_last", {})
    for msg in reader.fetch_new(cursors):
        draft = briefs_mod.extract(msg.content)
        slug = draft.data["slug"]
        path = deps.settings.drafts_dir / f"{slug}-{msg.id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(briefs_mod.render(draft, msg.content), encoding="utf-8")
        gaps = f" Missing: {', '.join(draft.missing)}." if draft.missing else ""
        report.alert(
            f"New campaign brief from {msg.author} -> draft `{path.name}`.{gaps}\n"
            f"Review it, then: `clipworks campaign approve {path.relative_to(deps.settings.home)}`"
        )


# --- 2. generation ----------------------------------------------------------


def generate_step(deps: Deps, campaign: Campaign, state: dict, report: Report) -> None:
    s = deps.settings
    if s.publikclip_dir is None:
        return
    seen: list[str] = state.setdefault("seen", {}).setdefault(campaign.slug, [])
    pending: dict[str, dict] = state.setdefault("pending_jobs", {}).setdefault(campaign.slug, {})
    budget = s.videos_per_cycle

    def finish(url: str, result: generate.RunResult, origin: str) -> None:
        if result.ok:
            clips = [_with_origin(c, origin) for c in result.clips]
            deps.store.add_clips(campaign.slug, clips)
            seen.append(url)
            pending.pop(url, None)
            report.info(f"{campaign.slug}: rendered {len(clips)} clips from {url}")
            return
        attempts = pending.get(url, {}).get("attempts", 0) + 1
        if result.resumable and attempts < s.max_resume_attempts:
            pending[url] = {"job_id": result.job_id, "attempts": attempts, "origin": origin}
            report.info(f"{campaign.slug}: publikclip failed on {url} (attempt {attempts}), will resume: {result.error}")
            return
        seen.append(url)
        pending.pop(url, None)
        report.alert(f"{campaign.slug}: giving up on {url} after {attempts} attempt(s): {result.error}")

    try:
        for url, info in list(pending.items()):
            if budget <= 0:
                return
            budget -= 1
            res = deps.resume_publikclip(
                info["job_id"], url, campaign, s.publikclip_dir, venv=s.publikclip_venv
            )
            finish(url, res, info.get("origin", ""))

        if budget <= 0:
            return
        try:
            todo = deps.resolve(
                campaign,
                seen=frozenset(seen) | frozenset(pending),
                cfg=sources.ResolveConfig(cookies_from_browser=s.cookies_from_browser),
            )
        except sources.CookiesRequired as err:
            _alert_once(state, report, f"cookies:{campaign.slug}", deps.now(), str(err))
            return
        except sources.SourceError as err:
            report.alert(f"{campaign.slug}: source resolution failed: {err}")
            return
        for url, origin in todo[:budget]:
            res = deps.run_publikclip(url, campaign, s.publikclip_dir, venv=s.publikclip_venv)
            finish(url, res, origin)
    except generate.GenerateError as err:
        _alert_once(state, report, "publikclip-missing", deps.now(), f"publikclip cannot run: {err}")


def _with_origin(c: RenderedClip, origin: str) -> RenderedClip:
    if c.origin or not origin:
        return c
    return RenderedClip(**{**c.__dict__, "origin": origin})


def _alert_once(state: dict, report: Report, key: str, now: datetime, msg: str) -> None:
    """Repeating the same alert every 30 minutes trains you to ignore alerts."""
    sent = state.setdefault("alerted", {})
    day = now.date().isoformat()
    if sent.get(key) != day:
        sent[key] = day
        report.alert(msg)


# --- 3. planning ------------------------------------------------------------


def _account_cap(settings: Settings, platform: str) -> int:
    raw = settings.env.get(f"CLIPWORKS_MAX_PER_DAY_{platform.upper()}", "").strip()
    if raw.isdigit():
        return int(raw)
    # Account-wide ceiling across ALL campaigns on one account. Two campaigns
    # each posting their own 3/day to the same Instagram account is 6/day,
    # which is the volume pattern that gets reach throttled.
    return DEFAULT_POSTS_PER_DAY.get(platform, 1) * 2


def _window(settings: Settings) -> tuple[int, int]:
    """Posting window in UTC hours. Default 12-23 UTC is 8am-7pm US Eastern:
    Whop clipping campaigns pay on mostly US views."""
    raw = settings.env.get("CLIPWORKS_WINDOW_UTC", "12-23").strip()
    try:
        a, b = (int(x) for x in raw.split("-", 1))
    except ValueError as err:
        raise ValueError(f"CLIPWORKS_WINDOW_UTC must look like 12-23, got {raw!r}") from err
    if not (0 <= a < b <= 23):
        raise ValueError(f"CLIPWORKS_WINDOW_UTC hours must satisfy 0 <= start < end <= 23, got {raw!r}")
    return a, b


def _campaign_posts_today(ledger_rows: list[PostRecord], slug: str, day: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in ledger_rows:
        if r.campaign == slug and r.succeeded and r.posted_day == day:
            out[r.platform] = out.get(r.platform, 0) + 1
    return out


def plan_step(deps: Deps, campaign: Campaign, report: Report) -> None:
    now = deps.now()
    day = now.date().isoformat()
    if deps.store.plan(campaign.slug, day) is not None:
        return

    rows = list(deps.ledger.current().values())
    global_view = deps.ledger.view_for(day)
    mine = _campaign_posts_today(rows, campaign.slug, day)
    total_today = sum(mine.values())

    captions: dict[tuple[str, str], str] = {}
    usable: list[RenderedClip] = []
    for clip in deps.store.clips(campaign.slug):
        if not Path(clip.path).exists():
            continue
        usable.append(clip)
        for platform in campaign.platforms:
            if global_view.already_posted(clip, platform):
                continue
            text = caption_mod.build(clip, campaign)
            verdict = compliance.check(clip, text, campaign, platform, posted_today=total_today)
            if verdict.ok:
                captions[(clip.clip_id, platform)] = text
                continue
            real = [r for r in verdict.reasons if not r.startswith(CAP_REASON)]
            if real and deps.store.quarantine(
                {"campaign": campaign.slug, "clip_id": clip.clip_id, "platform": platform,
                 "path": str(clip.path), "reasons": real, "at": now.isoformat()}
            ):
                report.info(f"{campaign.slug}: quarantined {clip.clip_id} for {platform}: {'; '.join(real)}")

    # Planned-but-not-posted entries in OTHER campaigns' plans today count
    # against the account ceiling, or two campaigns would both claim it.
    others_planned: dict[str, int] = {}
    for slug, entries in deps.store.plans_for_day(day).items():
        if slug == campaign.slug:
            continue
        for e in entries:
            if e.get("status") == "planned":
                others_planned[e["platform"]] = others_planned.get(e["platform"], 0) + 1

    start, end = _window(deps.settings)
    cadences = []
    for platform in campaign.platforms:
        room = _account_cap(deps.settings, platform) - global_view.posted_today.get(platform, 0) - others_planned.get(platform, 0)
        per_day = min(campaign.posts_per_day(platform), mine.get(platform, 0) + max(0, room))
        cadences.append(scheduler.CadenceConfig(platform, per_day, window_start_hour=start, window_end_hour=end))

    view = scheduler.LedgerView(posted_today=mine, posted_clip_platform=global_view.posted_clip_platform)
    jobs = scheduler.plan(usable, cadences, view, captions, day=now, seed=zlib.crc32(f"{campaign.slug}:{day}".encode()))

    # The campaign's per-clipper cap is across platforms: keep the best clips.
    remaining = max(0, campaign.rules.per_clipper_daily_cap - total_today)
    if len(jobs) > remaining:
        keep = {id(j) for j in sorted(jobs, key=lambda j: (-j.clip.score, j.scheduled_for))[:remaining]}
        jobs = [j for j in jobs if id(j) in keep]

    deps.store.save_plan(campaign.slug, day, [Store.job_to_entry(j) for j in jobs])
    if jobs:
        by = {}
        for j in jobs:
            by[j.platform] = by.get(j.platform, 0) + 1
        report.info(f"{campaign.slug}: planned {len(jobs)} posts for {day} ({', '.join(f'{k} {v}' for k, v in by.items())})")
    elif usable:
        report.info(f"{campaign.slug}: nothing eligible to plan for {day}")


# --- 4. posting -------------------------------------------------------------


def post_step(deps: Deps, campaign: Campaign, report: Report) -> None:
    now = deps.now()
    day = now.date().isoformat()
    entries = deps.store.plan(campaign.slug, day)
    if not entries:
        return
    posted_this_cycle: set[str] = set()

    for e in entries:
        if e.get("status") != "planned" or e["platform"] in posted_this_cycle:
            continue
        if datetime.fromisoformat(e["scheduled_for"]) > now:
            continue
        job = Store.entry_to_job(e)
        status, detail = _post_one(deps, campaign, job, report)
        e["status"], e["detail"] = status, detail
        deps.store.save_plan(campaign.slug, day, entries)
        if status not in ("skipped_cap", "missing_file"):
            posted_this_cycle.add(job.platform)


def _post_one(deps: Deps, campaign: Campaign, job: PostJob, report: Report) -> tuple[str, str]:
    store, ledger = deps.store, deps.ledger
    pid = post_id_for(job)
    existing = ledger.current().get(pid)
    if existing and existing.succeeded:
        return "done", "already in ledger"

    if store.is_inflight(pid):
        # Crashed between upload and ledger write. Record it as posted with no
        # URL: that consumes the slot and blocks a re-post. Never retry blind.
        ledger.record_result(job, campaign.slug, url=None, media_id=None, campaign_source=campaign.source_kind)
        store.clear_inflight(pid)
        report.alert(
            f"{campaign.slug}: outcome UNKNOWN for {pid} (process died mid-upload). Check {job.platform}; "
            f"if it is live run `clipworks ledger set-url {pid} <url>`."
        )
        return "unknown", "crashed mid-upload"

    rows = list(ledger.current().values())
    total_today = sum(_campaign_posts_today(rows, campaign.slug, deps.now().date().isoformat()).values())
    verdict = compliance.check(job.clip, job.caption, campaign, job.platform, posted_today=total_today)
    if not verdict.ok:
        return "skipped_cap" if all(r.startswith(CAP_REASON) for r in verdict.reasons) else "blocked", "; ".join(verdict.reasons)

    if not Path(job.clip.path).exists():
        return "missing_file", str(job.clip.path)

    poster = deps.posters.get(job.platform)
    missing = poster.missing_config() if poster else ["poster"]
    if deps.settings.dry_run or missing:
        store.log_dry_run({"post_id": pid, "campaign": campaign.slug, "platform": job.platform,
                           "path": str(job.clip.path), "caption": job.caption,
                           "missing": missing, "at": deps.now().isoformat()})
        report.info(f"{campaign.slug}: DRY RUN {pid} ({'forced' if deps.settings.dry_run else 'missing ' + ', '.join(missing)})")
        return "dry_run", ", ".join(missing)

    store.mark_inflight(pid)
    try:
        title = job.clip.hook or job.caption
        result = poster.post(Path(job.clip.path), job.caption, title)
    except Exception as err:  # noqa: BLE001 - a poster bug must not kill the cycle
        result = None
        error = f"{type(err).__name__}: {err}"
    if result is None:
        ledger.record_result(job, campaign.slug, url=None, media_id=None, error=error, campaign_source=campaign.source_kind)
        store.clear_inflight(pid)
        report.alert(f"{campaign.slug}: {job.platform} poster crashed on {pid}: {error}")
        return "failed", error

    record = ledger.record_result(
        job, campaign.slug, url=result.url, media_id=result.media_id,
        error=None if result.ok else (result.error or "unknown error"),
        campaign_source=campaign.source_kind,
    )
    store.clear_inflight(pid)

    if not result.ok:
        report.alert(f"{campaign.slug}: {job.platform} post failed for {pid}: {result.error}")
        return "failed", result.error or ""
    if result.needs_manual_publish:
        report.alert(
            f"{campaign.slug}: {job.platform} draft uploaded for {pid}. Publish it in the app, then "
            f"`clipworks ledger set-url {pid} <url>` to submit it."
        )
        return "needs_publish", ""
    report.info(f"{campaign.slug}: posted {pid} -> {result.url}")
    submit_to_campaign(deps, campaign, record, report)
    return "done", result.url or ""


def submit_to_campaign(deps: Deps, campaign: Campaign, record: PostRecord, report: Report) -> None:
    if campaign.source_kind == "discord_direct":
        report.alert(f"{campaign.slug}: posted {record.url}. Direct-pay campaign: send it to the streamer as their brief asks.")
        return
    out = deps.whop.submit(record, campaign)
    if out.status == "skipped":
        return
    if out.status == "error":
        deps.ledger.update(record.post_id, whop_status=MANUAL)
        report.alert(f"{campaign.slug}: Whop API submit failed ({out.message}). Submit by hand: {record.url}")
        return
    deps.ledger.update(record.post_id, whop_submission_id=out.submission_id, whop_status=out.status)
    if out.status == MANUAL:
        where = campaign.whop.campaign_url or "the Whop campaign page"
        report.alert(
            f"{campaign.slug}: submit {record.url} at {where}, then `clipworks whop done {record.post_id}`"
        )
    else:
        report.info(f"{campaign.slug}: submitted {record.post_id} to Whop bounty ({out.submission_id})")


# --- 5/6. whop sync + reconcile ---------------------------------------------


def whop_step(deps: Deps, report: Report) -> None:
    for rec in whop_sync(deps.ledger, deps.whop):
        if rec.whop_status == "denied":
            report.alert(f"Whop DENIED {rec.post_id} ({rec.url}): {rec.denial_reason or 'no reason given'}")
        else:
            report.info(f"Whop {rec.whop_status}: {rec.post_id}")


def reconcile_step(deps: Deps, state: dict, report: Report, force: bool = False) -> int:
    now = deps.now()
    day = now.date().isoformat()
    if not force and state.get("last_reconcile") == day:
        return 0
    cutoff = now - timedelta(days=deps.settings.reconcile_days)
    updated = 0
    for rec in deps.ledger.current().values():
        if not rec.succeeded or not rec.media_id:
            continue
        if datetime.fromisoformat(rec.posted_at) < cutoff:
            continue
        poster = deps.posters.get(rec.platform)
        if poster is None or poster.missing_config():
            continue
        views = poster.views(rec.media_id)
        if views is not None and views != rec.views:
            deps.ledger.reconcile(rec.post_id, views=views)
            updated += 1
    state["last_reconcile"] = day
    if updated:
        report.info(f"reconciled views on {updated} posts")
    return updated


# --- the cycle --------------------------------------------------------------


def cycle(deps: Deps) -> Report:
    report = Report()
    with CycleLock(deps.settings.runs_dir / ".lock"):
        state = deps.store.state()
        try:
            _safely(report, "brief intake", lambda: intake_briefs(deps, state, report))
            for campaign in load_active(deps.settings, report):
                _safely(report, f"{campaign.slug} generate", lambda c=campaign: generate_step(deps, c, state, report))
                deps.store.save_state(state)
                _safely(report, f"{campaign.slug} plan", lambda c=campaign: plan_step(deps, c, report))
                _safely(report, f"{campaign.slug} post", lambda c=campaign: post_step(deps, c, report))
            _safely(report, "whop sync", lambda: whop_step(deps, report))
            _safely(report, "reconcile", lambda: reconcile_step(deps, state, report))
        finally:
            deps.store.save_state(state)
    if report.alerts:
        deps.notifier.send("**clipworks**\n" + "\n".join(f"- {a}" for a in report.alerts))
    return report


def _safely(report: Report, what: str, fn: Callable[[], object]) -> None:
    """One broken campaign or API must not stop the others from posting."""
    try:
        fn()
    except Exception as err:  # noqa: BLE001
        tb = traceback.format_exc(limit=3).strip().splitlines()[-1]
        report.alert(f"{what} crashed: {type(err).__name__}: {err} ({tb})")
