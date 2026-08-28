from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from clipworks.ledger import Ledger, PostRecord, post_id_for, utc_today
from clipworks.models import PostJob, RenderedClip

# utc_today(), NOT date.today(). The ledger stores posted_at in UTC and
# derives posted_day from it, so a local date disagrees for the whole
# 00:00-05:30 IST window and this test fails only during those hours.
# That is the exact skew view_for() is written to avoid, so asserting
# against a local date tested the bug rather than the fix.
TODAY = utc_today()


def clip(idx: int = 0, score: float = 0.9, source: str = "https://youtu.be/a") -> RenderedClip:
    return RenderedClip(
        job_id="job", index=idx, path=Path(f"c{idx}.mp4"), score=score,
        best_platform="instagram", duration=30.0, source_video=source, hook="h",
    )


def job(idx: int = 0, platform: str = "instagram") -> PostJob:
    return PostJob(clip=clip(idx), platform=platform, caption="cap", scheduled_for="2026-08-16T10:00:00+00:00")


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.jsonl")


def test_empty_ledger_reads_as_empty(ledger):
    assert ledger.load() == []
    assert ledger.current() == {}
    view = ledger.view_for()
    assert view.posted_today == {}
    assert view.posted_clip_platform == frozenset()


def test_records_a_successful_post(ledger):
    rec = ledger.record_result(job(), campaign="c", url="https://ig/p/1", media_id="m1")
    assert rec.succeeded
    assert ledger.current()[rec.post_id].url == "https://ig/p/1"


def test_post_id_is_deterministic_so_retries_cannot_double_count(ledger):
    """A crash between posting and recording must not create two rows that the
    scheduler then treats as two separate posts."""
    assert post_id_for(job()) == post_id_for(job())
    ledger.record_result(job(), campaign="c", url="u", media_id="m")
    ledger.record_result(job(), campaign="c", url="u", media_id="m")
    assert len(ledger.load()) == 2, "both rows are written (append-only)"
    assert len(ledger.current()) == 1, "but they fold to one post"


def test_failed_post_does_not_consume_a_daily_slot(ledger):
    """A failed attempt must not eat the day's capacity or block a retry."""
    ledger.record_result(job(), campaign="c", url=None, media_id=None, error="429 from IG")
    view = ledger.view_for(today=TODAY)
    assert view.posted_today == {}
    assert view.posted_clip_platform == frozenset()


def test_successful_post_blocks_the_same_clip_platform_pair(ledger):
    ledger.record_result(job(), campaign="c", url="u", media_id="m")
    view = ledger.view_for(today=TODAY)
    assert view.already_posted(clip(), "instagram")
    assert not view.already_posted(clip(), "tiktok")


def test_counts_are_per_platform(ledger):
    ledger.record_result(job(0, "instagram"), campaign="c", url="u", media_id="m")
    ledger.record_result(job(1, "instagram"), campaign="c", url="u", media_id="m")
    ledger.record_result(job(2, "tiktok"), campaign="c", url="u", media_id="m")
    view = ledger.view_for(today=TODAY)
    assert view.posted_today == {"instagram": 2, "tiktok": 1}


def test_reconcile_appends_rather_than_rewrites(ledger):
    rec = ledger.record_result(job(), campaign="c", url="u", media_id="m")
    ledger.reconcile(rec.post_id, views=12_000, payout_usd=12.0)
    assert len(ledger.load()) == 2, "history preserved"
    current = ledger.current()[rec.post_id]
    assert current.views == 12_000
    assert current.payout_usd == 12.0
    assert current.reconciled_at is not None


def test_reconciling_an_unknown_post_raises(ledger):
    with pytest.raises(KeyError):
        ledger.reconcile("nope:instagram", views=1)


def test_realised_rate_is_none_until_something_is_paid(ledger):
    ledger.record_result(job(), campaign="c", url="u", media_id="m")
    assert ledger.realised_rate() is None


def test_realised_rate_uses_only_reconciled_rows(ledger):
    """Quoting a campaign's advertised rate back at yourself is not evidence.
    Only rows with both views and payout count."""
    a = ledger.record_result(job(0), campaign="c", url="u", media_id="m")
    b = ledger.record_result(job(1), campaign="c", url="u", media_id="m")
    ledger.reconcile(a.post_id, views=10_000, payout_usd=15.0)
    # b stays unreconciled and must not drag the average toward zero.
    assert ledger.realised_rate() == pytest.approx(1.5)


def test_realised_rate_can_split_whop_from_discord_direct(ledger):
    a = ledger.record_result(job(0), campaign="c", url="u", media_id="m", campaign_source="whop")
    b = ledger.record_result(job(1), campaign="c", url="u", media_id="m", campaign_source="discord_direct")
    ledger.reconcile(a.post_id, views=10_000, payout_usd=10.0)
    ledger.reconcile(b.post_id, views=10_000, payout_usd=0.0)  # streamer never paid
    assert ledger.realised_rate("whop") == pytest.approx(1.0)
    assert ledger.realised_rate("discord_direct") == pytest.approx(0.0)
    assert ledger.realised_rate() == pytest.approx(0.5)


def test_unreconciled_lists_only_successful_unpaid_posts(ledger):
    a = ledger.record_result(job(0), campaign="c", url="u", media_id="m")
    ledger.record_result(job(1), campaign="c", url=None, media_id=None, error="failed")
    assert [r.post_id for r in ledger.unreconciled()] == [a.post_id]


def test_truncated_final_line_costs_one_record_not_the_ledger(ledger):
    """A cron job killed mid-write leaves a partial line. Losing one record is
    acceptable; losing the file is not."""
    ledger.record_result(job(0), campaign="c", url="u", media_id="m")
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('{"post_id": "half-written", "clip_i')
    assert len(ledger.load()) == 1


def test_unknown_fields_in_a_row_are_skipped_not_fatal(ledger):
    ledger.record_result(job(0), campaign="c", url="u", media_id="m")
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"post_id": "x", "totally": "different schema"}) + "\n")
    assert len(ledger.load()) == 1
