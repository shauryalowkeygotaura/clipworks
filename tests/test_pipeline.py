"""End-to-end cycles with every external edge faked: publikclip, yt-dlp,
platforms, Whop, Discord. Each test is a way the real thing loses money."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from clipworks import pipeline
from clipworks.config import Settings
from clipworks.discord import BriefReader, Notifier
from clipworks.generate import RunResult
from clipworks.ledger import Ledger
from clipworks.models import RenderedClip
from clipworks.posters.base import PostResult
from clipworks.store import Store
from clipworks.whop import MANUAL, WhopClient

from fakes import FakeHttp

CHANNEL = "https://www.youtube.com/@creator"

CAMPAIGN = {
    "slug": "demo",
    "platforms": ["instagram", "youtube"],
    "source": {"channels": [CHANNEL]},
    "cadence": {"instagram": 2, "youtube": 2},
    "rules": {
        "min_duration_sec": 10,
        "max_duration_sec": 60,
        "required_hashtags": ["#creator"],
        "required_credit_text": "@creator",
        "caption_template": "{hook}\n\n{credit} {hashtags}",
        "per_clipper_daily_cap": 3,
    },
    "whop": {"campaign_url": "https://whop.com/discover/demo/"},
}


class FakePoster:
    def __init__(self, platform, *, configured=True, result=None, explode=False):
        self.platform = platform
        self.configured = configured
        self.result = result
        self.explode = explode
        self.posted: list[tuple[Path, str]] = []

    def missing_config(self):
        return [] if self.configured else [f"{self.platform.upper()}_TOKEN"]

    def post(self, video, caption, title):
        if self.explode:
            raise RuntimeError("boom")
        self.posted.append((video, caption))
        n = len(self.posted)
        return self.result or PostResult(ok=True, url=f"https://{self.platform}.example/p{n}", media_id=f"{self.platform}-{n}")

    def views(self, media_id):
        return 1000


class Clock:
    def __init__(self, iso):
        self.t = datetime.fromisoformat(iso)

    def __call__(self):
        return self.t

    def set(self, iso):
        self.t = datetime.fromisoformat(iso)


def make_clips(tmp_path, job, n, duration=30.0, score=0.9):
    out = []
    for i in range(n):
        p = tmp_path / "renders" / f"{job}_{i}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        out.append(RenderedClip(job_id=job, index=i, path=p, score=score - i * 0.01, best_platform="instagram",
                                duration=duration, source_video=f"https://www.youtube.com/watch?v={job:0<11}"[:43],
                                hook=f"hook {i}"))
    return out


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "campaigns" / "active").mkdir(parents=True)
    (home / "campaigns" / "active" / "demo.yaml").write_text(yaml.safe_dump(CAMPAIGN))
    pc = tmp_path / "publikclip"
    pc.mkdir()
    settings = Settings(home=home, publikclip_dir=pc, env={"CLIPWORKS_WINDOW_UTC": "12-23"})
    clock = Clock("2026-11-03T12:00:00+00:00")
    posters = {"instagram": FakePoster("instagram"), "youtube": FakePoster("youtube")}
    notified: list[str] = []
    notifier = Notifier(FakeHttp())
    notifier.send = lambda text: notified.append(text) or True
    runs = {"n": 0}

    def fake_run(url, campaign, publikclip_dir, venv=None):
        runs["n"] += 1
        return RunResult(ok=True, job_id=f"job{runs['n']}", clips=tuple(make_clips(tmp_path, f"job{runs['n']}", 4)))

    deps = pipeline.Deps(
        settings=settings,
        store=Store(settings.runs_dir),
        ledger=Ledger(settings.runs_dir / "ledger.jsonl"),
        posters=posters,
        notifier=notifier,
        whop=WhopClient(FakeHttp()),
        brief_reader=BriefReader(FakeHttp()),
        run_publikclip=fake_run,
        resolve=lambda campaign, seen, cfg: [(u, CHANNEL) for u in
                                              ("https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                               "https://www.youtube.com/watch?v=bbbbbbbbbbb") if u not in seen],
        now=clock,
    )
    return deps, clock, posters, notified, runs


def run_day(deps, clock, day="2026-11-03"):
    for hour in range(12, 24):
        for minute in (0, 30):
            clock.set(f"{day}T{hour:02d}:{minute:02d}:00+00:00")
            pipeline.cycle(deps)


def test_channel_clips_pass_compliance_via_origin(env):
    """Regression: channel-sourced clips used to fail 'not in allowed sources'."""
    deps, clock, *_ = env
    pipeline.cycle(deps)
    assert deps.store.quarantined() == []
    assert all(c.origin == CHANNEL for c in deps.store.clips("demo"))


def test_full_day_respects_campaign_cap_and_never_double_posts(env):
    deps, clock, posters, notified, runs = env
    run_day(deps, clock)
    rows = [r for r in deps.ledger.current().values() if r.succeeded]
    assert len(rows) == 3  # per_clipper_daily_cap, across platforms
    assert len({r.post_id for r in rows}) == 3
    assert sum(len(p.posted) for p in posters.values()) == 3
    # publikclip runs once per cycle at most, one new source each time
    assert runs["n"] == 2


def test_never_more_than_one_post_per_platform_per_cycle(env):
    deps, clock, posters, *_ = env
    pipeline.cycle(deps)  # plan made at 12:00
    clock.set("2026-11-03T23:59:00+00:00")  # every slot now overdue at once
    pipeline.cycle(deps)
    assert all(len(p.posted) <= 1 for p in posters.values())


def test_second_day_does_not_repost_yesterdays_clips(env):
    deps, clock, posters, *_ = env
    run_day(deps, clock, "2026-11-03")
    day1 = {name: len(p.posted) for name, p in posters.items()}
    run_day(deps, clock, "2026-11-04")
    for p in posters.values():
        paths = [str(v) for v, _ in p.posted]
        assert len(paths) == len(set(paths)), f"{p.platform} uploaded a file twice"
    assert sum(len(p.posted) for p in posters.values()) > sum(day1.values())  # day 2 did post


def test_missing_credentials_is_dry_run_and_never_touches_ledger(env):
    deps, clock, posters, *_ = env
    posters["instagram"].configured = False
    posters["youtube"].configured = False
    run_day(deps, clock)
    assert deps.ledger.load() == []
    assert (deps.settings.runs_dir / "dryrun.jsonl").exists()


def test_crash_mid_upload_is_never_retried(env):
    deps, clock, posters, notified, _ = env
    pipeline.cycle(deps)
    plan = deps.store.plan("demo", "2026-11-03")
    first = plan[0]
    deps.store.mark_inflight(first["post_id"])  # simulate death after upload
    clock.set(first["scheduled_for"])
    pipeline.cycle(deps)
    rec = deps.ledger.current()[first["post_id"]]
    assert rec.succeeded and rec.url is None  # slot consumed, blocks re-post
    assert any("UNKNOWN" in n for n in notified)
    assert all(v != first["clip"]["path"] for p in posters.values() for v, _ in p.posted)


def test_failed_post_is_recorded_and_alerted(env):
    deps, clock, posters, notified, _ = env
    posters["instagram"].result = PostResult(ok=False, error="instagram: nope")
    posters["youtube"].result = PostResult(ok=False, error="youtube: nope")
    run_day(deps, clock)
    rows = deps.ledger.load()
    assert rows and all(not r.succeeded for r in rows)
    assert any("post failed" in n for n in notified)


def test_poster_exception_does_not_kill_the_cycle(env):
    deps, clock, posters, notified, _ = env
    posters["instagram"].explode = True
    run_day(deps, clock)
    assert posters["youtube"].posted  # the other platform still posted
    assert any("crashed" in n for n in notified)


def test_content_rewards_post_is_queued_for_manual_whop_submit(env):
    deps, clock, posters, notified, _ = env
    run_day(deps, clock)
    rows = [r for r in deps.ledger.current().values() if r.succeeded]
    assert rows and all(r.whop_status == MANUAL for r in rows)
    assert any("whop.com/discover/demo" in n for n in notified)


def test_bounty_campaign_submits_via_api(env, tmp_path):
    deps, clock, posters, notified, _ = env
    raw = {**CAMPAIGN, "whop": {"bounty_id": "bnty_abc"}}
    (deps.settings.active_dir / "demo.yaml").write_text(yaml.safe_dump(raw))
    http = FakeHttp().on("POST", r"/bounty_submissions$", {"id": "btys_1", "status": "submitted"}) \
                     .on("GET", r"/bounty_submissions/btys_", {"id": "btys_1", "status": "denied", "denial_reason": "no credit"})
    deps.whop = WhopClient(http, token="T")
    run_day(deps, clock)
    sub = http.find("POST", r"/bounty_submissions$")[0]
    assert sub["json"]["bounty_id"] == "bnty_abc"
    assert sub["headers"]["Idempotency-Key"].startswith("clipworks:")
    assert any("DENIED" in n and "no credit" in n for n in notified)


def test_publikclip_failure_resumes_then_gives_up(env, tmp_path):
    deps, clock, posters, notified, _ = env
    resumed = []
    deps.run_publikclip = lambda url, *a, **k: RunResult(ok=False, job_id="j1", error="OOM")
    deps.resume_publikclip = lambda job_id, url, *a, **k: resumed.append(job_id) or RunResult(ok=False, job_id=job_id, error="OOM")
    for _ in range(4):
        pipeline.cycle(deps)
    assert resumed == ["j1", "j1"]  # 1 run + 2 resumes = max_resume_attempts (3)
    assert any("giving up" in n for n in notified)


def test_invalid_campaign_is_skipped_not_fatal(env):
    deps, clock, posters, notified, _ = env
    (deps.settings.active_dir / "broken.yaml").write_text("slug: x\nplatforms: [myspace]\n")
    pipeline.cycle(deps)
    assert any("broken.yaml is invalid" in n for n in notified)
    assert deps.store.plan("demo", "2026-11-03") is not None


def test_account_ceiling_shared_across_campaigns(env):
    deps, clock, posters, *_ = env
    second = {**CAMPAIGN, "slug": "demo2", "platforms": ["instagram"], "cadence": {"instagram": 4},
              "rules": {**CAMPAIGN["rules"], "per_clipper_daily_cap": 10}}
    (deps.settings.active_dir / "demo2.yaml").write_text(yaml.safe_dump(second))
    deps.settings.env["CLIPWORKS_MAX_PER_DAY_INSTAGRAM"] = "3"
    run_day(deps, clock)
    ig = [r for r in deps.ledger.current().values() if r.succeeded and r.platform == "instagram"]
    assert len(ig) <= 3


def test_reconcile_runs_once_per_day(env):
    deps, clock, posters, *_ = env
    run_day(deps, clock)
    clock.set("2026-11-04T00:10:00+00:00")
    pipeline.cycle(deps)
    assert all(r.views == 1000 for r in deps.ledger.current().values() if r.succeeded and r.posted_day == "2026-11-03")


def test_lock_blocks_overlapping_cycles(env):
    deps, *_ = env
    with pipeline.CycleLock(deps.settings.runs_dir / ".lock"):
        with pytest.raises(pipeline.Locked):
            pipeline.cycle(deps)
