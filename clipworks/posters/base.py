from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PostResult:
    ok: bool
    url: str | None = None
    media_id: str | None = None
    error: str | None = None
    # True when the platform accepted the file but a human must still press
    # publish (TikTok inbox mode). Such a post has no public URL yet, so it
    # cannot be submitted to a campaign and must not be counted as live.
    needs_manual_publish: bool = False


class Poster(Protocol):
    platform: str

    def missing_config(self) -> list[str]:
        """Env vars still needed. Empty means ready to post for real."""
        ...

    def post(self, video: Path, caption: str, title: str) -> PostResult: ...

    def views(self, media_id: str) -> int | None:
        """Current view count, or None if the platform will not tell us."""
        ...


Sleep = Callable[[float], None]


def poll(fn: Callable[[], str | None], *, attempts: int, interval: float, sleep: Sleep = time.sleep) -> str | None:
    """Call `fn` until it returns a non-None terminal value or attempts run out."""
    for i in range(attempts):
        value = fn()
        if value is not None:
            return value
        if i < attempts - 1:
            sleep(interval)
    return None


def env_get(env: dict[str, str] | None, key: str, default: str = "") -> str:
    import os
    source = env if env is not None else os.environ
    return (source.get(key) or default).strip()
