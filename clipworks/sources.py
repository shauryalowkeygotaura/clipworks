"""Expand a campaign's allowed source material into concrete video URLs.

Uses yt-dlp's flat-playlist mode, which lists a channel or playlist's entries
without touching the media. Cheap enough to run every cycle.

YouTube bot-checks this. A bare call returns "Sign in to confirm you're not a
bot", which yt-dlp reports as a plain exit code 1 with the reason only on
stderr. `_run` therefore parses stderr and raises a distinct exception, because
"YouTube wants cookies" and "that channel does not exist" need completely
different responses and both otherwise arrive as exit 1.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from .models import Campaign

# Whitelist, per vault rules: only these source shapes are accepted. Anything
# else is rejected rather than sanitized, because a half-cleaned URL handed to
# a subprocess is exactly the class of bug the whitelist rule exists to stop.
_YOUTUBE_HOST = re.compile(r"^https://(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

YTDLP_TIMEOUT = 120


class SourceError(RuntimeError):
    """Source resolution failed in a way the operator must act on."""


class CookiesRequired(SourceError):
    """YouTube demanded authentication. Distinct because the fix is specific:
    supply browser cookies, not retry, not fix the URL."""


@dataclass(frozen=True)
class ResolveConfig:
    """`cookies_from_browser` maps to yt-dlp's --cookies-from-browser (e.g.
    "chrome", "firefox"). Left None by default: pulling cookies attaches a real
    logged-in account to bulk downloading, which is a decision the operator
    should make explicitly rather than inherit from a default."""
    cookies_from_browser: str | None = None
    max_per_source: int = 50


def resolve(
    campaign: Campaign,
    seen: frozenset[str] = frozenset(),
    cfg: ResolveConfig | None = None,
) -> list[str]:
    """Return unprocessed video URLs for every source in the campaign.

    Order is stable (campaign source order, then playlist order) so a run that
    dies partway through resumes on the same next video rather than jumping
    around the back catalogue.
    """
    cfg = cfg or ResolveConfig()
    out: list[str] = []
    emitted: set[str] = set()

    for source in sorted(campaign.allowed_sources):
        for url in _expand(source, cfg):
            if url in seen or url in emitted:
                continue
            emitted.add(url)
            out.append(url)
    return out


def _expand(source: str, cfg: ResolveConfig) -> list[str]:
    if not _YOUTUBE_HOST.match(source):
        raise SourceError(
            f"unsupported source {source!r}: only https YouTube channel, playlist, "
            f"or video URLs are accepted"
        )
    # A single video needs no expansion, and asking yt-dlp to flat-list it
    # wastes a network round trip per cycle.
    if _is_single_video(source):
        return [source]

    payload = _run_flat(source, cfg)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SourceError(f"{source}: yt-dlp returned no entries list")

    urls: list[str] = []
    for entry in entries[: cfg.max_per_source]:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("id")
        if isinstance(vid, str) and _VIDEO_ID.match(vid):
            urls.append(f"https://www.youtube.com/watch?v={vid}")
    return urls


def _is_single_video(url: str) -> bool:
    if "youtu.be/" in url:
        return True
    match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})(?:&|$)", url)
    return match is not None


def _run_flat(source: str, cfg: ResolveConfig) -> dict:
    args = ["yt-dlp", "--flat-playlist", "-J", "--no-warnings", "--ignore-no-formats-error"]
    if cfg.cookies_from_browser:
        args += ["--cookies-from-browser", cfg.cookies_from_browser]
    args.append(source)

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=YTDLP_TIMEOUT)
    except FileNotFoundError as err:
        raise SourceError("yt-dlp is not installed or not on PATH") from err
    except subprocess.TimeoutExpired as err:
        raise SourceError(f"{source}: yt-dlp timed out after {YTDLP_TIMEOUT}s") from err

    if proc.returncode != 0:
        raise _classify(source, proc.stderr or "")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise SourceError(f"{source}: yt-dlp emitted unparseable JSON") from err
    if not isinstance(payload, dict):
        raise SourceError(f"{source}: expected a JSON object from yt-dlp")
    return payload


def _classify(source: str, stderr: str) -> SourceError:
    """Turn yt-dlp's exit-1-for-everything into an actionable exception."""
    lowered = stderr.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return CookiesRequired(
            f"{source}: YouTube demanded sign-in ('not a bot' check). Set "
            f"ResolveConfig(cookies_from_browser='chrome') or supply cookies. "
            f"Note this attaches a real account to bulk downloading."
        )
    if "private video" in lowered or "members-only" in lowered:
        return SourceError(f"{source}: video is private or members-only")
    if "unavailable" in lowered or "does not exist" in lowered or "404" in lowered:
        return SourceError(f"{source}: not found or unavailable")
    first_line = next((ln for ln in stderr.splitlines() if ln.strip()), "")
    return SourceError(f"{source}: yt-dlp failed: {first_line[:200] or 'no stderr'}")
