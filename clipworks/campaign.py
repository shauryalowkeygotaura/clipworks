"""Load and validate a campaign YAML into a frozen Campaign.

Validation is whitelist-only, per vault coding rules. Every enum-shaped field
(platform, camera mode, LLM backend) is checked against a known-good set and
rejected outright if it does not match. Nothing is coerced, stripped, or
"cleaned up" into validity — a typo in a campaign file must fail loudly at
load time, because the alternative is a campaign that silently posts to the
wrong platform or runs with a setting the brief forbids.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    PLATFORMS,
    Campaign,
    PublikclipSettings,
    Rules,
)

VALID_CAMERA = frozenset({"cut", "pan", "locked"})
# "groq" exists only because we patched it into the vendored publikclip fork
# (Code/publikclip/.../scoring/llm.py). Upstream ships gemini and ollama only,
# so a campaign asking for groq against a clean checkout would fail at run
# time rather than here. Kept in the whitelist deliberately: groq is the house
# default, and the fork is part of the project.
VALID_LLM = frozenset({"groq", "gemini", "ollama"})


class CampaignError(ValueError):
    """A campaign file is malformed. Always fatal: we never guess."""


def load(path: Path) -> Campaign:
    text = _read(path)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise CampaignError(f"{path}: not valid YAML: {err}") from err
    if not isinstance(raw, dict):
        raise CampaignError(f"{path}: top level must be a mapping, got {type(raw).__name__}")
    return parse(raw, origin=str(path))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise CampaignError(f"no campaign file at {path}") from err
    except OSError as err:
        raise CampaignError(f"cannot read {path}: {err}") from err


def parse(raw: dict[str, Any], origin: str = "<dict>") -> Campaign:
    """Pure. Kept separate from `load` so tests need no filesystem."""
    slug = _require_str(raw, "slug", origin)

    platforms = tuple(_require_list(raw, "platforms", origin))
    if not platforms:
        raise CampaignError(f"{origin}: platforms must not be empty")
    unknown = [p for p in platforms if p not in PLATFORMS]
    if unknown:
        raise CampaignError(
            f"{origin}: unknown platform(s) {unknown}; allowed: {sorted(PLATFORMS)}"
        )

    allowed_sources = frozenset(_collect_sources(raw.get("source"), origin))
    if not allowed_sources:
        raise CampaignError(
            f"{origin}: source must list at least one channel, playlist, or video. "
            f"An empty source set would reject every clip at compliance."
        )

    return Campaign(
        slug=slug,
        platforms=platforms,
        allowed_sources=allowed_sources,
        rules=_parse_rules(raw.get("rules"), origin),
        publikclip=_parse_publikclip(raw.get("publikclip"), origin),
        rate_per_1k_views_usd=_optional_float(raw.get("payout"), "rate_per_1k_views_usd", origin),
        budget_remaining_usd=_optional_float(raw.get("payout"), "budget_remaining_usd", origin),
    )


def _collect_sources(source: Any, origin: str) -> list[str]:
    if source is None:
        raise CampaignError(f"{origin}: missing required 'source' section")
    if not isinstance(source, dict):
        raise CampaignError(f"{origin}: 'source' must be a mapping")
    out: list[str] = []
    for key in ("channels", "playlists", "videos"):
        values = source.get(key) or []
        if not isinstance(values, list):
            raise CampaignError(f"{origin}: source.{key} must be a list")
        for v in values:
            if not isinstance(v, str) or not v.strip():
                raise CampaignError(f"{origin}: source.{key} entries must be non-empty strings")
            out.append(v.strip())
    return out


def _parse_rules(raw: Any, origin: str) -> Rules:
    if not isinstance(raw, dict):
        raise CampaignError(f"{origin}: missing required 'rules' section")

    min_sec = _require_int(raw, "min_duration_sec", origin)
    max_sec = _require_int(raw, "max_duration_sec", origin)
    if min_sec <= 0:
        raise CampaignError(f"{origin}: rules.min_duration_sec must be positive")
    if max_sec < min_sec:
        raise CampaignError(
            f"{origin}: rules.max_duration_sec ({max_sec}) is below min_duration_sec ({min_sec}); "
            f"no clip could ever pass"
        )

    hashtags = raw.get("required_hashtags") or []
    if not isinstance(hashtags, list) or any(not isinstance(h, str) for h in hashtags):
        raise CampaignError(f"{origin}: rules.required_hashtags must be a list of strings")

    template = raw.get("caption_template")
    if not isinstance(template, str) or not template.strip():
        raise CampaignError(f"{origin}: rules.caption_template is required")

    banned = raw.get("banned_styles") or []
    if not isinstance(banned, list) or any(not isinstance(b, str) for b in banned):
        raise CampaignError(f"{origin}: rules.banned_styles must be a list of strings")

    cap = _require_int(raw, "per_clipper_daily_cap", origin)
    if cap <= 0:
        raise CampaignError(f"{origin}: rules.per_clipper_daily_cap must be positive")

    return Rules(
        min_duration_sec=min_sec,
        max_duration_sec=max_sec,
        required_hashtags=tuple(hashtags),
        required_credit_text=str(raw.get("required_credit_text") or ""),
        caption_template=template,
        banned_styles=frozenset(banned),
        per_clipper_daily_cap=cap,
    )


def _parse_publikclip(raw: Any, origin: str) -> PublikclipSettings:
    if raw is None:
        return PublikclipSettings()
    if not isinstance(raw, dict):
        raise CampaignError(f"{origin}: 'publikclip' must be a mapping")
    settings = PublikclipSettings(
        llm=str(raw.get("llm", "groq")),
        captions=str(raw.get("captions", "default")),
        camera=str(raw.get("camera", "cut")),
    )
    if settings.llm not in VALID_LLM:
        raise CampaignError(
            f"{origin}: publikclip.llm must be one of {sorted(VALID_LLM)}, got {settings.llm!r}"
        )
    if settings.camera not in VALID_CAMERA:
        raise CampaignError(
            f"{origin}: publikclip.camera must be one of {sorted(VALID_CAMERA)}, got {settings.camera!r}"
        )
    return settings


def _require_str(raw: dict[str, Any], key: str, origin: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(f"{origin}: '{key}' is required and must be a non-empty string")
    return value.strip()


def _require_list(raw: dict[str, Any], key: str, origin: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise CampaignError(f"{origin}: '{key}' is required and must be a list of strings")
    return value


def _require_int(raw: dict[str, Any], key: str, origin: str) -> int:
    value = raw.get(key)
    # bool is an int subclass in Python; a YAML `true` here is a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignError(f"{origin}: '{key}' is required and must be an integer")
    return value


def _optional_float(section: Any, key: str, origin: str) -> float | None:
    if section is None:
        return None
    if not isinstance(section, dict):
        raise CampaignError(f"{origin}: 'payout' must be a mapping")
    value = section.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignError(f"{origin}: payout.{key} must be a number if present")
    return float(value)
