"""The gate that decides whether a clip is allowed to be posted.

This is the highest-value module in the project and the reason it exists.
Whop campaigns verify clips AFTER the views accrue and reject non-compliant
ones retroactively. A clip that does 40,000 views and is rejected for a
missing credit line pays nothing. So every rule the campaign states is
checked here, before the post, and a failure blocks rather than warns.

Two deliberate choices:

1. **Fail closed on unknown rules.** If a campaign names a banned edit style
   we have no guard for, that is a rejection, not a pass. We cannot promise
   compliance with a rule we do not understand, and quietly assuming we
   comply is exactly how retroactive rejections happen.

2. **Banned styles are asserted, not detected.** We generate the clips, so we
   check the settings we ran publikclip with rather than analysing pixels.
   Claiming to detect "no AI voiceover" from a video file would be dishonest;
   knowing we never enabled one is a fact.

Every check is pure. Nothing here touches disk or network.
"""
from __future__ import annotations

from collections.abc import Callable

from .models import PLATFORM_MAX_SEC, Campaign, PublikclipSettings, RenderedClip, Verdict

# A guard answers: "given these publikclip settings, is this banned style
# definitely absent from the output?" Returning False means we might produce
# it, so the clip is blocked.
#
# publikclip never synthesises speech and never composites a second video
# source, so those two are structurally impossible rather than merely
# configured off. They are still listed explicitly: an unlisted style fails
# closed, and silence would read as an oversight rather than a decision.
BANNED_STYLE_GUARDS: dict[str, Callable[[PublikclipSettings], bool]] = {
    # publikclip has no TTS stage. It only ever reframes and captions the
    # source audio, so no synthetic voice can appear.
    "ai_voiceover": lambda s: True,
    # Single-source reframe. There is no second video track to split against.
    "split_screen_gameplay": lambda s: True,
    "split_screen": lambda s: True,
    # Some campaigns forbid burned-in subtitles because they collide with the
    # platform's own caption UI. The only way to guarantee none is the
    # "none" caption preset.
    "burned_captions": lambda s: s.captions == "none",
    # "No jump cuts" campaigns object to the speaker-change hard cut. Pan and
    # locked camera modes hold a continuous frame instead.
    "jump_cuts": lambda s: s.camera in ("pan", "locked"),
    # Motion-heavy reframing. Only a locked camera is definitely still.
    "camera_movement": lambda s: s.camera == "locked",
}


def check(
    clip: RenderedClip,
    caption: str,
    campaign: Campaign,
    platform: str,
    posted_today: int,
) -> Verdict:
    """Validate one clip against one campaign for one platform.

    Collects every failure rather than short-circuiting on the first. A clip
    that is both too short and missing a hashtag should report both, so a
    systematically misconfigured campaign is obvious from one quarantine entry
    instead of taking three runs to diagnose.
    """
    reasons: list[str] = []
    rules = campaign.rules

    if platform not in campaign.platforms:
        reasons.append(f"platform {platform!r} not allowed by campaign (allowed: {', '.join(campaign.platforms)})")

    # Platform ceilings are the platform's own and cannot be relaxed by a
    # campaign, so they are checked independently of the campaign window.
    platform_cap = PLATFORM_MAX_SEC.get(platform)
    if platform_cap is None:
        reasons.append(f"unknown platform {platform!r}")
    elif clip.duration > platform_cap:
        reasons.append(f"duration {clip.duration:.1f}s exceeds {platform} cap of {platform_cap}s")

    if clip.duration < rules.min_duration_sec:
        reasons.append(f"duration {clip.duration:.1f}s below campaign minimum {rules.min_duration_sec}s")
    if clip.duration > rules.max_duration_sec:
        reasons.append(f"duration {clip.duration:.1f}s above campaign maximum {rules.max_duration_sec}s")

    # Case-insensitive: campaigns write "#CreatorName" in the brief and
    # platforms treat hashtags case-insensitively, so a case mismatch is a
    # false rejection, not a real one.
    lowered = caption.lower()
    for tag in rules.required_hashtags:
        if tag.lower() not in lowered:
            reasons.append(f"caption missing required hashtag {tag!r}")

    if rules.required_credit_text and rules.required_credit_text.lower() not in lowered:
        reasons.append(f"caption missing required credit text {rules.required_credit_text!r}")

    # Either the video itself is whitelisted, or it was resolved from a
    # whitelisted channel/playlist. `origin` is set by the pipeline from the
    # yt-dlp listing of that source, never from anything the clip claims.
    if (
        clip.source_video not in campaign.allowed_sources
        and clip.origin not in campaign.allowed_sources
    ):
        reasons.append(f"source video {clip.source_video!r} is not in the campaign's allowed sources")

    if posted_today >= rules.per_clipper_daily_cap:
        reasons.append(
            f"daily cap reached ({posted_today}/{rules.per_clipper_daily_cap} posted today)"
        )

    reasons.extend(_banned_style_reasons(rules.banned_styles, campaign.publikclip))

    return Verdict(ok=not reasons, reasons=tuple(reasons))


def _banned_style_reasons(
    banned: frozenset[str], settings: PublikclipSettings
) -> list[str]:
    """Fail closed: a style we have no guard for blocks the clip."""
    reasons: list[str] = []
    for style in sorted(banned):
        guard = BANNED_STYLE_GUARDS.get(style)
        if guard is None:
            reasons.append(
                f"campaign bans {style!r} but clipworks has no guard for it; "
                f"add one to BANNED_STYLE_GUARDS before running this campaign"
            )
        elif not guard(settings):
            reasons.append(
                f"current publikclip settings may produce banned style {style!r} "
                f"(llm={settings.llm}, captions={settings.captions}, camera={settings.camera})"
            )
    return reasons
