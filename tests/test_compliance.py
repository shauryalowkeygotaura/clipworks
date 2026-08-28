"""Compliance is the module that decides whether money gets earned or wasted,
so it gets the heaviest coverage. Every test here is a real rejection mode
that would otherwise only show up as an unpaid clip weeks later.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clipworks.compliance import check
from clipworks.models import Campaign, PublikclipSettings, RenderedClip, Rules

SOURCE = "https://www.youtube.com/watch?v=abc123"


def make_rules(**over) -> Rules:
    base = dict(
        min_duration_sec=15,
        max_duration_sec=90,
        required_hashtags=("#creatorname",),
        required_credit_text="clip from @creatorname",
        caption_template="{hook}\n\n{credit}\n{hashtags}",
        banned_styles=frozenset(),
        per_clipper_daily_cap=5,
    )
    base.update(over)
    return Rules(**base)


def make_campaign(**over) -> Campaign:
    base = dict(
        slug="test",
        platforms=("instagram", "youtube", "tiktok"),
        allowed_sources=frozenset({SOURCE}),
        rules=make_rules(),
        publikclip=PublikclipSettings(),
    )
    base.update(over)
    return Campaign(**base)


def make_clip(**over) -> RenderedClip:
    base = dict(
        job_id="job1",
        index=0,
        path=Path("clip_00.mp4"),
        score=0.8,
        best_platform="instagram",
        duration=42.0,
        source_video=SOURCE,
        hook="the hook",
    )
    base.update(over)
    return RenderedClip(**base)


GOOD_CAPTION = "the hook\n\nclip from @creatorname\n#creatorname"


def test_compliant_clip_passes():
    v = check(make_clip(), GOOD_CAPTION, make_campaign(), "instagram", posted_today=0)
    assert v.ok, v.reasons
    assert bool(v) is True


def test_missing_hashtag_is_rejected():
    caption = "the hook\n\nclip from @creatorname"
    v = check(make_clip(), caption, make_campaign(), "instagram", posted_today=0)
    assert not v.ok
    assert any("hashtag" in r for r in v.reasons)


def test_missing_credit_is_rejected():
    caption = "the hook\n\n#creatorname"
    v = check(make_clip(), caption, make_campaign(), "instagram", posted_today=0)
    assert not v.ok
    assert any("credit" in r for r in v.reasons)


def test_hashtag_match_is_case_insensitive():
    """Briefs write '#CreatorName'; platforms treat tags case-insensitively.
    Rejecting on case would be a false negative that silently halves output."""
    campaign = make_campaign(rules=make_rules(required_hashtags=("#CreatorName",)))
    v = check(make_clip(), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert v.ok, v.reasons


def test_platform_hard_cap_beats_permissive_campaign():
    """A campaign allowing 300s cannot make Instagram accept a 120s reel."""
    campaign = make_campaign(rules=make_rules(max_duration_sec=300))
    v = check(make_clip(duration=120.0), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert not v.ok
    assert any("instagram cap" in r for r in v.reasons)


def test_same_clip_passes_on_a_longer_platform():
    campaign = make_campaign(rules=make_rules(max_duration_sec=300))
    v = check(make_clip(duration=120.0), GOOD_CAPTION, campaign, "tiktok", posted_today=0)
    assert v.ok, v.reasons


def test_too_short_is_rejected():
    v = check(make_clip(duration=8.0), GOOD_CAPTION, make_campaign(), "instagram", posted_today=0)
    assert not v.ok
    assert any("below campaign minimum" in r for r in v.reasons)


def test_platform_not_allowed_by_campaign():
    campaign = make_campaign(platforms=("tiktok",))
    v = check(make_clip(), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert not v.ok
    assert any("not allowed by campaign" in r for r in v.reasons)


def test_source_outside_allowed_set_is_rejected():
    clip = make_clip(source_video="https://www.youtube.com/watch?v=NOTALLOWED")
    v = check(clip, GOOD_CAPTION, make_campaign(), "instagram", posted_today=0)
    assert not v.ok
    assert any("allowed sources" in r for r in v.reasons)


def test_daily_cap_blocks_at_the_limit_not_past_it():
    """Cap of 5 means the 6th post is blocked, so posted_today==5 must fail."""
    assert check(make_clip(), GOOD_CAPTION, make_campaign(), "instagram", posted_today=4).ok
    v = check(make_clip(), GOOD_CAPTION, make_campaign(), "instagram", posted_today=5)
    assert not v.ok
    assert any("daily cap" in r for r in v.reasons)


def test_unknown_banned_style_fails_closed():
    """The core safety property: a rule we do not understand blocks the post.
    Passing it would be silently promising compliance we cannot deliver."""
    campaign = make_campaign(rules=make_rules(banned_styles=frozenset({"no_face_reveal"})))
    v = check(make_clip(), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert not v.ok
    assert any("no guard for it" in r for r in v.reasons)


def test_banned_style_violated_by_current_settings():
    campaign = make_campaign(
        rules=make_rules(banned_styles=frozenset({"burned_captions"})),
        publikclip=PublikclipSettings(captions="default"),
    )
    v = check(make_clip(), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert not v.ok
    assert any("burned_captions" in r for r in v.reasons)


def test_banned_style_satisfied_by_settings():
    campaign = make_campaign(
        rules=make_rules(banned_styles=frozenset({"burned_captions"})),
        publikclip=PublikclipSettings(captions="none"),
    )
    v = check(make_clip(), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert v.ok, v.reasons


def test_structurally_impossible_style_passes_regardless_of_settings():
    """publikclip has no TTS stage, so ai_voiceover cannot occur."""
    campaign = make_campaign(rules=make_rules(banned_styles=frozenset({"ai_voiceover"})))
    v = check(make_clip(), GOOD_CAPTION, campaign, "instagram", posted_today=0)
    assert v.ok, v.reasons


def test_all_failures_are_reported_together():
    """One quarantine entry should be enough to diagnose a broken campaign,
    rather than fixing one rule per run for three runs."""
    campaign = make_campaign(platforms=("tiktok",))
    v = check(make_clip(duration=5.0), "no tags here", campaign, "instagram", posted_today=99)
    assert not v.ok
    assert len(v.reasons) >= 5


def test_unknown_platform_is_rejected():
    campaign = make_campaign(platforms=("instagram", "snapchat"))
    v = check(make_clip(), GOOD_CAPTION, campaign, "snapchat", posted_today=0)
    assert not v.ok
    assert any("unknown platform" in r for r in v.reasons)
