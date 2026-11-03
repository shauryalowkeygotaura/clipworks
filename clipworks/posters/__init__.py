"""Platform posters. Official APIs only.

Private-API clients (instagrapi and friends) are deliberately absent: they
died on app-version checks in October 2026, and when they work they are the
fastest way to get the account that earns the money banned.
"""
from __future__ import annotations

from ..http import Http
from .base import Poster, PostResult
from .instagram import InstagramPoster
from .tiktok import TikTokPoster
from .youtube import YouTubePoster


def build_all(http: Http | None = None, env: dict[str, str] | None = None, state_dir=None) -> dict[str, Poster]:
    http = http or Http()
    return {
        "instagram": InstagramPoster.from_env(http, env),
        "youtube": YouTubePoster.from_env(http, env),
        "tiktok": TikTokPoster.from_env(http, env, state_dir=state_dir),
    }


__all__ = ["Poster", "PostResult", "InstagramPoster", "YouTubePoster", "TikTokPoster", "build_all"]
