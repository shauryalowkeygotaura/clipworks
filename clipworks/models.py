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


@dataclass(frozen=True)
class Campaign:
    slug: str
    platforms: tuple[str, ...]
    allowed_sources: frozenset[str]
    rules: Rules
    publikclip: PublikclipSettings = field(default_factory=PublikclipSettings)
    rate_per_1k_views_usd: float | None = None
    budget_remaining_usd: float | None = None


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
