"""Runtime settings, all from the environment (Doppler in practice).

Nothing secret is read anywhere else, and nothing here has a default that
spends money or posts publicly: every live integration is off until its
credentials exist, and CLIPWORKS_DRY_RUN=1 forces all of them off.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from err


@dataclass(frozen=True)
class Settings:
    home: Path
    publikclip_dir: Path | None = None
    publikclip_venv: str | None = None
    dry_run: bool = False
    cookies_from_browser: str | None = None
    videos_per_cycle: int = 1
    max_resume_attempts: int = 3
    reconcile_days: int = 30
    discord_webhook: str = field(default="", repr=False)
    discord_bot_token: str = field(default="", repr=False)
    discord_brief_channels: tuple[str, ...] = ()
    whop_user_token: str = field(default="", repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def campaigns_dir(self) -> Path:
        return self.home / "campaigns"

    @property
    def active_dir(self) -> Path:
        return self.campaigns_dir / "active"

    @property
    def drafts_dir(self) -> Path:
        return self.campaigns_dir / "drafts"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        pc = (env.get("PUBLIKCLIP_DIR") or "").strip()
        channels = tuple(c.strip() for c in (env.get("DISCORD_BRIEF_CHANNEL_IDS") or "").split(",") if c.strip())
        return cls(
            home=Path(env.get("CLIPWORKS_HOME") or Path.cwd()).resolve(),
            publikclip_dir=Path(pc).expanduser() if pc else None,
            publikclip_venv=(env.get("PUBLIKCLIP_VENV") or "").strip() or None,
            dry_run=(env.get("CLIPWORKS_DRY_RUN") or "").strip() == "1",
            cookies_from_browser=(env.get("YTDLP_COOKIES_FROM_BROWSER") or "").strip() or None,
            videos_per_cycle=_int(env, "CLIPWORKS_VIDEOS_PER_CYCLE", 1),
            max_resume_attempts=_int(env, "CLIPWORKS_MAX_RESUME_ATTEMPTS", 3),
            reconcile_days=_int(env, "CLIPWORKS_RECONCILE_DAYS", 30),
            discord_webhook=(env.get("DISCORD_WEBHOOK_URL") or "").strip(),
            discord_bot_token=(env.get("DISCORD_BOT_TOKEN") or "").strip(),
            discord_brief_channels=channels,
            whop_user_token=(env.get("WHOP_USER_TOKEN") or "").strip(),
            env=env,
        )
