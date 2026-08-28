"""Decide which clips get posted, where, and when.

publikclip typically returns 5-12 scored clips per source video. The scheduler
turns that pile into a day's posting plan under three constraints: the
campaign's per-clipper daily cap, each platform's tolerance for volume, and
the fact that clips carry a `best_platform` hint we may or may not honour.

Everything here is pure. The ledger is passed in as a snapshot so the same
inputs always produce the same plan, which is what makes it testable.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import PostJob, RenderedClip


@dataclass(frozen=True)
class CadenceConfig:
    """Posting rhythm for one platform."""
    platform: str
    posts_per_day: int
    window_start_hour: int = 9   # local-ish; times are emitted as UTC ISO
    window_end_hour: int = 22
    jitter_minutes: int = 25


@dataclass(frozen=True)
class LedgerView:
    """Read-only slice of the ledger the scheduler needs."""
    posted_today: dict[str, int]          # platform -> count posted today
    posted_clip_platform: frozenset[tuple[str, str]]  # (clip_id, platform) already posted

    def already_posted(self, clip: RenderedClip, platform: str) -> bool:
        return (clip.clip_id, platform) in self.posted_clip_platform


def spread_times(cfg: CadenceConfig, day: datetime, rng: random.Random) -> list[str]:
    """Evenly space `posts_per_day` slots across the window, then jitter each.

    Even spacing keeps a burst of eight uploads from landing in one hour, which
    is the pattern that reads as automation. The jitter stops the schedule from
    being identical every day.
    """
    if cfg.posts_per_day <= 0:
        return []
    if day.tzinfo is None:
        # A naive datetime would be read as system-local by astimezone(), so a
        # cron box in a different timezone would silently shift the whole
        # schedule. Refuse rather than guess.
        raise ValueError("day must be timezone-aware; pass datetime.now(timezone.utc)")
    if cfg.window_end_hour <= cfg.window_start_hour:
        raise ValueError(
            f"{cfg.platform}: window_end_hour ({cfg.window_end_hour}) must be after "
            f"window_start_hour ({cfg.window_start_hour})"
        )
    start = day.replace(hour=cfg.window_start_hour, minute=0, second=0, microsecond=0)
    end = day.replace(hour=cfg.window_end_hour, minute=0, second=0, microsecond=0)
    span = (end - start).total_seconds()
    step = span / max(1, cfg.posts_per_day)

    out: list[str] = []
    for i in range(cfg.posts_per_day):
        base = start + timedelta(seconds=step * i + step / 2)
        offset = rng.uniform(-cfg.jitter_minutes, cfg.jitter_minutes)
        out.append((base + timedelta(minutes=offset)).astimezone(timezone.utc).isoformat())
    return sorted(out)


def _pick_spread(slots: list[str], count: int) -> list[str]:
    """Take `count` roughly evenly spaced entries from `slots`.

    Used when there are fewer clips than slots. Taking the first `count` would
    bunch every post into the start of the window, which is the burst pattern
    the spacing exists to avoid.
    """
    if count <= 0 or not slots:
        return []
    if count >= len(slots):
        return slots[:count]
    step = len(slots) / count
    return [slots[min(len(slots) - 1, int(i * step))] for i in range(count)]


# Never post below this, even if it means leaving a paid slot empty. Whop pays
# per view so volume is tempting, but a run of weak clips suppresses the
# account's reach, which taxes every later post including the good ones. An
# empty slot costs one clip's views; a throttled account costs all of them.
MIN_SCORE = 0.6

# One source video may supply at most this many posts to one platform per day.
# Without it a single strong podcast episode occupies the whole schedule and
# the account reads as a repost farm, which is exactly the signal we are
# trying not to send.
MAX_PER_SOURCE_PER_DAY = 2


def select(
    clips: list[RenderedClip],
    platform: str,
    slots: int,
    ledger: LedgerView,
    honour_best_platform: bool,
) -> list[RenderedClip]:
    """Choose which clips fill this platform's slots today.

    Two-tier fill. publikclip's `best_platform` hint gets first refusal, then
    anything else eligible backfills. Treating the hint as binding would leave
    slots empty whenever a day's clips all point elsewhere, and it is a hint
    from a scorer we have not yet calibrated, not a fact.

    Returns at most `slots` clips, possibly fewer. Fewer is a valid answer.
    """
    eligible = [
        c for c in clips
        if c.score >= MIN_SCORE and not ledger.already_posted(c, platform)
    ]
    # Sort by score first so the per-source cap keeps each video's best clips
    # rather than whichever happened to be rendered first.
    eligible.sort(key=lambda c: (-c.score, c.clip_id))

    tiers = [eligible]
    if honour_best_platform:
        preferred = [c for c in eligible if c.best_platform == platform]
        # Split by clip_id, not by value equality: RenderedClip is a frozen
        # dataclass, so `c not in preferred` compares field-by-field and would
        # drop a distinct clip that happens to match another one's values.
        preferred_ids = {c.clip_id for c in preferred}
        tiers = [preferred, [c for c in eligible if c.clip_id not in preferred_ids]]

    chosen: list[RenderedClip] = []
    per_source: dict[str, int] = {}
    for tier in tiers:
        for c in tier:
            if len(chosen) >= slots:
                return chosen
            if per_source.get(c.source_video, 0) >= MAX_PER_SOURCE_PER_DAY:
                continue
            per_source[c.source_video] = per_source.get(c.source_video, 0) + 1
            chosen.append(c)
    return chosen


def plan(
    clips: list[RenderedClip],
    cadences: list[CadenceConfig],
    ledger: LedgerView,
    captions: dict[tuple[str, str], str],
    day: datetime | None = None,
    seed: int | None = None,
    honour_best_platform: bool = True,
) -> list[PostJob]:
    """Build one day's posting plan across every configured platform."""
    day = day or datetime.now(timezone.utc)
    rng = random.Random(seed)
    jobs: list[PostJob] = []

    for cfg in cadences:
        remaining = cfg.posts_per_day - ledger.posted_today.get(cfg.platform, 0)
        if remaining <= 0:
            continue
        chosen = select(clips, cfg.platform, remaining, ledger, honour_best_platform)
        # Drop anything without a caption BEFORE assigning times. A missing
        # caption means the clip never cleared compliance, and filtering after
        # the zip would leave holes that re-bunch the surviving posts into the
        # early window.
        chosen = [c for c in chosen if (c.clip_id, cfg.platform) in captions]
        if not chosen:
            continue
        # Spread whatever we actually got across the whole remaining window.
        # Zipping straight against the slot list would bunch every post into
        # the earliest slots whenever select() returns fewer clips than slots,
        # which MIN_SCORE and MAX_PER_SOURCE_PER_DAY make a common case. That
        # burst is exactly the pattern spread_times() exists to avoid.
        times = _pick_spread(spread_times(cfg, day, rng)[-remaining:], len(chosen))
        for clip, when in zip(chosen, times):
            jobs.append(
                PostJob(
                    clip=clip,
                    platform=cfg.platform,
                    caption=captions[(clip.clip_id, cfg.platform)],
                    scheduled_for=when,
                )
            )

    return sorted(jobs, key=lambda j: j.scheduled_for)
