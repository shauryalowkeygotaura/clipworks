from __future__ import annotations

from pathlib import Path

import pytest

from clipworks.campaign import CampaignError, parse
from clipworks.caption import build, unresolved_placeholders
from clipworks.models import RenderedClip

RAW = {
    "slug": "example",
    "platforms": ["instagram", "tiktok"],
    "source": {"channels": ["https://www.youtube.com/@somecreator"]},
    "rules": {
        "min_duration_sec": 15,
        "max_duration_sec": 90,
        "required_hashtags": ["#creatorname"],
        "required_credit_text": "clip from @creatorname",
        "caption_template": "{hook}\n\n{credit}\n{hashtags}",
        "per_clipper_daily_cap": 5,
    },
}


def clip(hook: str = "wait until the end") -> RenderedClip:
    return RenderedClip(
        job_id="j", index=0, path=Path("c.mp4"), score=0.9,
        best_platform="instagram", duration=30.0,
        source_video="https://www.youtube.com/watch?v=x", hook=hook,
    )


# --- campaign loading -------------------------------------------------------

def test_parses_a_valid_campaign():
    c = parse(RAW)
    assert c.slug == "example"
    assert c.platforms == ("instagram", "tiktok")
    assert "https://www.youtube.com/@somecreator" in c.allowed_sources
    assert c.publikclip.camera == "cut"  # default


def test_unknown_platform_rejected_by_whitelist():
    raw = {**RAW, "platforms": ["instagram", "myspace"]}
    with pytest.raises(CampaignError, match="unknown platform"):
        parse(raw)


def test_unknown_camera_mode_rejected_by_whitelist():
    raw = {**RAW, "publikclip": {"camera": "orbit"}}
    with pytest.raises(CampaignError, match="publikclip.camera"):
        parse(raw)


def test_unknown_llm_rejected_by_whitelist():
    raw = {**RAW, "publikclip": {"llm": "gpt4"}}
    with pytest.raises(CampaignError, match="publikclip.llm"):
        parse(raw)


def test_groq_is_the_default_llm():
    """House rule is Groq. It only works because we patched a GroqClient into
    the vendored publikclip fork; upstream ships gemini and ollama only."""
    assert parse(RAW).publikclip.llm == "groq"


def test_all_three_llm_backends_are_accepted():
    for backend in ("groq", "gemini", "ollama"):
        assert parse({**RAW, "publikclip": {"llm": backend}}).publikclip.llm == backend


def test_empty_source_rejected():
    """An empty source set would reject every clip at compliance, so it is
    caught at load time where the error is actionable."""
    raw = {**RAW, "source": {"channels": []}}
    with pytest.raises(CampaignError, match="at least one"):
        parse(raw)


def test_impossible_duration_window_rejected():
    raw = {**RAW, "rules": {**RAW["rules"], "min_duration_sec": 90, "max_duration_sec": 30}}
    with pytest.raises(CampaignError, match="no clip could ever pass"):
        parse(raw)


def test_yaml_true_is_not_accepted_as_an_integer():
    """bool subclasses int in Python; `per_clipper_daily_cap: true` must fail
    rather than silently becoming a cap of 1."""
    raw = {**RAW, "rules": {**RAW["rules"], "per_clipper_daily_cap": True}}
    with pytest.raises(CampaignError, match="must be an integer"):
        parse(raw)


def test_missing_caption_template_rejected():
    rules = {k: v for k, v in RAW["rules"].items() if k != "caption_template"}
    with pytest.raises(CampaignError, match="caption_template"):
        parse({**RAW, "rules": rules})


# --- caption building -------------------------------------------------------

def test_builds_caption_from_template():
    out = build(clip(), parse(RAW))
    assert out == "wait until the end\n\nclip from @creatorname\n#creatorname"


def test_explicit_hook_overrides_clip_hook():
    out = build(clip(), parse(RAW), hook="my own hook")
    assert out.startswith("my own hook")


def test_empty_hook_does_not_leave_leading_blank_lines():
    out = build(clip(hook=""), parse(RAW))
    assert not out.startswith("\n")
    assert out == "clip from @creatorname\n#creatorname"


def test_literal_braces_in_hook_do_not_crash():
    """str.format() would raise on this. The whole reason build() uses
    replacement instead."""
    out = build(clip(hook="he said {this} and {that}"), parse(RAW))
    assert "{this}" in out


def test_unknown_placeholder_is_left_alone_not_crashed_on():
    raw = {**RAW, "rules": {**RAW["rules"], "caption_template": "{hook} {creator}"}}
    out = build(clip(), parse(raw))
    assert "{creator}" in out


def test_unresolved_placeholders_lints_the_template():
    assert unresolved_placeholders("{hook} {credit} {hashtags}") == []
    assert unresolved_placeholders("{hook} {creator} {brand}") == ["brand", "creator"]


def test_built_caption_satisfies_its_own_campaign_rules():
    """The contract between caption.build and compliance.check: a caption
    built from the campaign's own template must pass that campaign."""
    from clipworks.compliance import check

    campaign = parse(RAW)
    c = clip()
    caption = build(c, campaign)
    verdict = check(
        RenderedClip(**{**c.__dict__, "source_video": next(iter(campaign.allowed_sources))}),
        caption, campaign, "instagram", posted_today=0,
    )
    assert verdict.ok, verdict.reasons
