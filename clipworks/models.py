"""Core data types.

Everything here is frozen. The pipeline's correctness argument depends on
clips and campaign rules being immutable once loaded: a compliance verdict
computed at 09:00 must still be true when the scheduler posts at 14:00, and
that only holds if nothing can mutate the clip or the rules in between.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Hard platform ceilings. These are the platforms' own limits, NOT campaign
# rules — a campaign can be stricter but never looser, so these are checked
# separately and cannot be overridden by a campaign YAML.
PLATFORM_MAX_SEC: dict[str, int] = {
    "instagram": 90,
    "youtube": 180,
    "tiktok": 600,
}

PLATFORMS = frozenset(PLATFORM_MAX_SEC)


@dataclass(frozen=True)
class RenderedClip:
    """One finished 9:16 MP4 from publikclip's render stage.

    Mirrors the objects in `<job_dir>/render.json`'s `outputs` array. `score`
    and `best_platform` come from publikclip's scoring stage; we treat both as
    advisory, not binding.
    """
    job_id: str
    index: int
    path: Path
    score: float
    best_platform: str
    duration: float
    source_video: str
    hook: str = ""
    # The campaign source (channel / playlist / video URL) this clip's video
    # was resolved FROM. Needed because `sources.resolve` expands a channel
    # into watch?v= URLs, and only the channel URL is in allowed_sources.
    # Without provenance every channel-sourced clip fails compliance.
    origin: str = ""

    @property
    def clip_id(self) -> str:
        """Stable identity across runs. The job id is publikclip's, so this
        survives a `resume` and never collides between source videos."""
        return f"{self.job_id}:{self.index:02d}"


@dataclass(frozen=True)
class Rules:
    min_duration_sec: int
    max_duration_sec: int
    required_hashtags: tuple[str, ...]
    required_credit_text: str
    caption_template: str
    banned_styles: frozenset[str]
    per_clipper_daily_cap: int


@dataclass(frozen=True)
class PublikclipSettings:
    """The run knobs we pass to publikclip.

    These exist here rather than in a config file because `compliance.check`
    asserts banned edit styles against them. We control generation, so a banned
    style is prevented at the source instead of detected in the output.
    """
    # groq by default: house rule, and the fastest of the three. Note it is
    # text-only here — no Groq model does strict JSON schema AND vision, so
    # publikclip's visual subscore degrades to signals_missing. See the
    # GROQ_MODEL comment in the vendored fork's scoring/llm.py.
    llm: str = "groq"
    captions: str = "default"
    camera: str = "cut"


# Where a campaign came from, which decides how a posted clip gets paid.
# "whop": submitted to a Whop bounty or Content Rewards campaign.
# "discord_direct": a streamer pays direct; submission is whatever their
# Discord asks for, so it is always a manual handoff.
CAMPAIGN_SOURCES = frozenset({"whop", "discord_direct"})


@dataclass(frozen=True)
class WhopLink:
    """How posted clips reach Whop.

    `bounty_id` set: submitted via the public Bounty Submissions API.
    Only `campaign_url` set: Content Rewards has no submission API, so the
    clip URL is queued for a one-tap manual submit and pinged to Discord.
    """
    campaign_url: str = ""
    bounty_id: str = ""


@dataclass(frozen=True)
class Campaign:
    slug: str
    platforms: tuple[str, ...]
    allowed_sources: frozenset[str]
    rules: Rules
    publikclip: PublikclipSettings = field(default_factory=PublikclipSettings)
    rate_per_1k_views_usd: float | None = None
    budget_remaining_usd: float | None = None
    # (platform, posts_per_day). Empty means "use DEFAULT_POSTS_PER_DAY".
    cadence: tuple[tuple[str, int], ...] = ()
    source_kind: str = "whop"
    whop: WhopLink = field(default_factory=WhopLink)

    def posts_per_day(self, platform: str) -> int:
        for name, n in self.cadence:
            if name == platform:
                return n
        return DEFAULT_POSTS_PER_DAY.get(platform, 1)


# Conservative per-account defaults. YouTube's is bounded by quota: an upload
# costs 1,600 of the default 10,000 daily units, so 6 is the hard ceiling and
# 3 leaves room for retries and the stats calls reconciliation makes.
DEFAULT_POSTS_PER_DAY: dict[str, int] = {
    "instagram": 2,
    "youtube": 3,
    "tiktok": 3,
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class PostJob:
    clip: RenderedClip
    platform: str
    caption: str
    scheduled_for: str  # ISO 8601 UTC
