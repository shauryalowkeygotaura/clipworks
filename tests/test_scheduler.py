from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clipworks.models import RenderedClip
from clipworks.scheduler import (
    MAX_PER_SOURCE_PER_DAY,
    MIN_SCORE,
    CadenceConfig,
    LedgerView,
    _pick_spread,
    plan,
    select,
    spread_times,
)

DAY = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
SRC_A = "https://youtu.be/aaa"
SRC_B = "https://youtu.be/bbb"


def clip(idx: int, score: float, platform: str = "instagram", source: str = SRC_A) -> RenderedClip:
    return RenderedClip(
        job_id="job", index=idx, path=Path(f"c{idx}.mp4"), score=score,
        best_platform=platform, duration=30.0, source_video=source, hook=f"hook{idx}",
    )


def empty_ledger() -> LedgerView:
    return LedgerView(posted_today={}, posted_clip_platform=frozenset())


# --- select -----------------------------------------------------------------

def test_picks_highest_scoring_first():
    clips = [clip(0, 0.62), clip(1, 0.95), clip(2, 0.78)]
    out = select(clips, "instagram", slots=2, ledger=empty_ledger(), honour_best_platform=False)
    assert [c.index for c in out] == [1, 2]


def test_score_floor_leaves_slots_empty_rather_than_posting_weak_clips():
    """The core policy call: an empty slot costs one clip's views, a throttled
    account costs all of them."""
    clips = [clip(0, 0.9), clip(1, MIN_SCORE - 0.01), clip(2, 0.2)]
    out = select(clips, "instagram", slots=3, ledger=empty_ledger(), honour_best_platform=False)
    assert len(out) == 1
    assert out[0].index == 0


def test_clip_exactly_at_the_floor_is_allowed():
    out = select([clip(0, MIN_SCORE)], "instagram", 1, empty_ledger(), False)
    assert len(out) == 1


def test_one_source_cannot_occupy_the_whole_day():
    clips = [clip(i, 0.9 - i * 0.01, source=SRC_A) for i in range(5)]
    out = select(clips, "instagram", slots=5, ledger=empty_ledger(), honour_best_platform=False)
    assert len(out) == MAX_PER_SOURCE_PER_DAY


def test_per_source_cap_keeps_the_best_clips_of_that_source():
    """Sorting happens before the cap, so the cap keeps the strongest two."""
    clips = [clip(0, 0.65), clip(1, 0.99), clip(2, 0.98)]
    out = select(clips, "instagram", slots=2, ledger=empty_ledger(), honour_best_platform=False)
    assert [c.index for c in out] == [1, 2]


def test_multiple_sources_fill_more_slots():
    clips = [clip(i, 0.9, source=SRC_A) for i in range(3)]
    clips += [clip(10 + i, 0.85, source=SRC_B) for i in range(3)]
    out = select(clips, "instagram", slots=4, ledger=empty_ledger(), honour_best_platform=False)
    assert len(out) == 4
    assert len({c.source_video for c in out}) == 2


def test_already_posted_pair_is_excluded():
    c = clip(0, 0.9)
    ledger = LedgerView(posted_today={}, posted_clip_platform=frozenset({(c.clip_id, "instagram")}))
    assert select([c], "instagram", 1, ledger, False) == []
    # Same clip on a different platform is still fair game.
    assert len(select([c], "tiktok", 1, ledger, False)) == 1


def test_best_platform_hint_gets_first_refusal_then_backfills():
    """The hint is advisory. Treating it as binding would leave slots empty
    whenever a day's clips all point elsewhere."""
    # Distinct sources so MAX_PER_SOURCE_PER_DAY does not bind and the tier
    # ordering is what is actually under test.
    clips = [clip(0, 0.70, platform="tiktok", source=SRC_A),
             clip(1, 0.99, platform="tiktok", source=SRC_B),
             clip(2, 0.65, platform="instagram", source="https://youtu.be/ccc")]
    out = select(clips, "instagram", slots=3, ledger=empty_ledger(), honour_best_platform=True)
    assert out[0].index == 2, "instagram-hinted clip should come first despite lower score"
    assert len(out) == 3, "the rest should backfill rather than leaving slots empty"
    assert [c.index for c in out[1:]] == [1, 0], "backfill tier still sorts by score"


def test_value_identical_clips_are_not_collapsed():
    """Regression: `c not in preferred` used dataclass equality, so a distinct
    clip matching another's field values got dropped from the backfill tier."""
    a = clip(0, 0.9, platform="instagram", source=SRC_A)
    b = RenderedClip(**{**a.__dict__, "job_id": "other"})  # same values, different job
    out = select([a, b], "instagram", slots=2, ledger=empty_ledger(), honour_best_platform=True)
    assert len(out) == 2


# --- spread_times -----------------------------------------------------------

def test_naive_datetime_is_refused():
    """Regression: astimezone() on a naive datetime silently assumes system
    local time, which would shift the schedule on a differently-zoned box."""
    cfg = CadenceConfig(platform="instagram", posts_per_day=3)
    with pytest.raises(ValueError, match="timezone-aware"):
        spread_times(cfg, datetime(2026, 8, 14, 12, 0), random.Random(1))


def test_inverted_window_is_refused():
    cfg = CadenceConfig(platform="instagram", posts_per_day=3, window_start_hour=22, window_end_hour=9)
    with pytest.raises(ValueError, match="must be after"):
        spread_times(cfg, DAY, random.Random(1))


def test_times_are_spread_not_bunched():
    cfg = CadenceConfig(platform="instagram", posts_per_day=4, jitter_minutes=0)
    times = spread_times(cfg, DAY, random.Random(1))
    assert len(times) == 4
    assert times == sorted(times)
    hours = [datetime.fromisoformat(t).hour for t in times]
    assert len(set(hours)) == 4, "four posts should not share an hour"


def test_seed_makes_the_schedule_reproducible():
    cfg = CadenceConfig(platform="instagram", posts_per_day=3)
    assert spread_times(cfg, DAY, random.Random(7)) == spread_times(cfg, DAY, random.Random(7))


# --- _pick_spread -----------------------------------------------------------

def test_pick_spread_uses_the_whole_range():
    slots = [f"t{i}" for i in range(10)]
    assert _pick_spread(slots, 3) == ["t0", "t3", "t6"]


def test_pick_spread_handles_edges():
    assert _pick_spread([], 3) == []
    assert _pick_spread(["a", "b"], 0) == []
    assert _pick_spread(["a", "b"], 5) == ["a", "b"]


# --- plan -------------------------------------------------------------------

def captions_for(clips, platforms) -> dict[tuple[str, str], str]:
    return {(c.clip_id, p): f"caption {c.index}" for c in clips for p in platforms}


def test_plan_spreads_fewer_clips_across_the_whole_window():
    """Regression: zipping chosen against the slot list bunched every post
    into the earliest slots when select() returned fewer clips than slots."""
    clips = [clip(0, 0.95), clip(1, 0.94)]  # 2 clips, 6 slots
    cfg = CadenceConfig(platform="instagram", posts_per_day=6, jitter_minutes=0)
    jobs = plan(clips, [cfg], empty_ledger(), captions_for(clips, ["instagram"]), day=DAY, seed=1)
    assert len(jobs) == 2
    hours = [datetime.fromisoformat(j.scheduled_for).hour for j in jobs]
    assert hours[1] - hours[0] >= 4, f"posts bunched together: {hours}"


def test_plan_drops_clips_without_captions_before_assigning_times():
    """A missing caption means the clip never cleared compliance. Filtering it
    after the zip would leave a hole and re-bunch the survivors."""
    clips = [clip(0, 0.95), clip(1, 0.94), clip(2, 0.93, source=SRC_B)]
    cfg = CadenceConfig(platform="instagram", posts_per_day=6, jitter_minutes=0)
    partial = {(clips[0].clip_id, "instagram"): "only this one"}
    jobs = plan(clips, [cfg], empty_ledger(), partial, day=DAY, seed=1)
    assert len(jobs) == 1
    assert jobs[0].caption == "only this one"


def test_plan_respects_remaining_daily_capacity():
    clips = [clip(i, 0.9, source=SRC_A if i < 2 else SRC_B) for i in range(4)]
    cfg = CadenceConfig(platform="instagram", posts_per_day=5)
    ledger = LedgerView(posted_today={"instagram": 4}, posted_clip_platform=frozenset())
    jobs = plan(clips, [cfg], ledger, captions_for(clips, ["instagram"]), day=DAY, seed=1)
    assert len(jobs) == 1


def test_plan_emits_nothing_when_the_cap_is_already_hit():
    clips = [clip(0, 0.9)]
    cfg = CadenceConfig(platform="instagram", posts_per_day=3)
    ledger = LedgerView(posted_today={"instagram": 3}, posted_clip_platform=frozenset())
    assert plan(clips, [cfg], ledger, captions_for(clips, ["instagram"]), day=DAY, seed=1) == []


def test_plan_across_platforms_is_sorted_by_time():
    clips = [clip(i, 0.9, source=SRC_A if i < 2 else SRC_B) for i in range(4)]
    cadences = [
        CadenceConfig(platform="instagram", posts_per_day=2),
        CadenceConfig(platform="tiktok", posts_per_day=2),
    ]
    caps = captions_for(clips, ["instagram", "tiktok"])
    jobs = plan(clips, cadences, empty_ledger(), caps, day=DAY, seed=3)
    assert len(jobs) == 4
    assert [j.scheduled_for for j in jobs] == sorted(j.scheduled_for for j in jobs)
