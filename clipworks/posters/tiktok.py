"""TikTok via the official Content Posting API.

Two modes, and the difference is not cosmetic:

* `inbox` (default): the video lands in the TikTok app's inbox as a draft
  and a human taps Post. Works for any developer app with video.upload.
* `direct`: publishes immediately, but TikTok only allows public direct
  posts from apps that passed its audit. An unaudited app can direct-post
  only as SELF_ONLY (private), which earns nothing on a views campaign.

So until the app is audited, TikTok is NOT fully automatic, and this module
says so rather than pretending: inbox results carry needs_manual_publish.

Access tokens live 24h; refresh tokens rotate. The rotated refresh token is
persisted to <state_dir>/tiktok_token.json (mode 0600) so the next run works.

Env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN,
optional TIKTOK_MODE (inbox|direct), TIKTOK_PRIVACY (direct mode only).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..http import Http, HttpError
from .base import PostResult, Sleep, env_get, poll

API = "https://open.tiktokapis.com/v2"
SINGLE_CHUNK_MAX = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
VALID_MODES = frozenset({"inbox", "direct"})


@dataclass
class TikTokPoster:
    http: Http
    client_key: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    mode: str = "inbox"
    privacy: str = "PUBLIC_TO_EVERYONE"
    state_dir: Path | None = None
    sleep: Sleep = field(default=time.sleep)
    platform: str = "tiktok"

    @classmethod
    def from_env(cls, http: Http, env: dict[str, str] | None = None, state_dir: Path | None = None) -> "TikTokPoster":
        mode = env_get(env, "TIKTOK_MODE", "inbox")
        if mode not in VALID_MODES:
            raise ValueError(f"TIKTOK_MODE must be one of {sorted(VALID_MODES)}, got {mode!r}")
        poster = cls(
            http=http,
            client_key=env_get(env, "TIKTOK_CLIENT_KEY"),
            client_secret=env_get(env, "TIKTOK_CLIENT_SECRET"),
            refresh_token=env_get(env, "TIKTOK_REFRESH_TOKEN"),
            mode=mode,
            privacy=env_get(env, "TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE"),
            state_dir=Path(state_dir) if state_dir else None,
        )
        stored = poster._stored_refresh_token()
        if stored:
            poster.refresh_token = stored
        return poster

    def missing_config(self) -> list[str]:
        pairs = (
            ("TIKTOK_CLIENT_KEY", self.client_key),
            ("TIKTOK_CLIENT_SECRET", self.client_secret),
            ("TIKTOK_REFRESH_TOKEN", self.refresh_token),
        )
        return [k for k, v in pairs if not v]

    # --- token handling ------------------------------------------------------

    def _token_file(self) -> Path | None:
        return self.state_dir / "tiktok_token.json" if self.state_dir else None

    def _stored_refresh_token(self) -> str:
        path = self._token_file()
        if not path or not path.exists():
            return ""
        try:
            return str(json.loads(path.read_text()).get("refresh_token") or "")
        except (OSError, json.JSONDecodeError):
            return ""

    def _access_token(self) -> str:
        body = self.http.post(
            f"{API}/oauth/token/",
            form={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        ).json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError(f"tiktok: token refresh failed: {body.get('error_description') or body}")
        rotated = body.get("refresh_token")
        if rotated and rotated != self.refresh_token:
            self.refresh_token = rotated
            path = self._token_file()
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"refresh_token": rotated}))
                os.chmod(path, 0o600)
        return token

    # --- posting -------------------------------------------------------------

    def post(self, video: Path, caption: str, title: str) -> PostResult:
        try:
            return self._post(Path(video), caption)
        except (HttpError, RuntimeError) as err:
            return PostResult(ok=False, error=f"tiktok: {err}")

    def _post(self, video: Path, caption: str) -> PostResult:
        token = self._access_token()
        data = video.read_bytes()
        size = len(data)
        if size <= SINGLE_CHUNK_MAX:
            chunk, count = size, 1
        else:
            chunk, count = CHUNK, size // CHUNK  # last chunk absorbs the remainder

        source_info = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        if self.mode == "direct":
            body = {
                "post_info": {"title": caption[:2200], "privacy_level": self.privacy, "disable_comment": False},
                "source_info": source_info,
            }
            path = "/post/publish/video/init/"
        else:
            body = {"source_info": source_info}
            path = "/post/publish/inbox/video/init/"
        init = self.http.post(f"{API}{path}", json_body=body, headers=auth).json()
        err = (init.get("error") or {}).get("code")
        if err and err != "ok":
            return PostResult(ok=False, error=f"tiktok: init {err}: {(init.get('error') or {}).get('message')}")
        publish_id = (init.get("data") or {}).get("publish_id")
        upload_url = (init.get("data") or {}).get("upload_url")
        if not publish_id or not upload_url:
            return PostResult(ok=False, error=f"tiktok: init returned {init}")

        for i in range(count):
            start = i * chunk
            end = size if i == count - 1 else start + chunk
            self.http.put(
                upload_url,
                data=data[start:end],
                headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end - 1}/{size}"},
            )

        if self.mode == "inbox":
            return PostResult(ok=True, media_id=publish_id, needs_manual_publish=True)

        post_ids: list[str] = []

        def status() -> str | None:
            resp = self.http.post(
                f"{API}/post/publish/status/fetch/", json_body={"publish_id": publish_id}, headers=auth
            ).json()
            d = resp.get("data") or {}
            st = d.get("status")
            if st == "PUBLISH_COMPLETE":
                post_ids.extend(str(x) for x in d.get("publicaly_available_post_id") or [])
                return "ok"
            if st == "FAILED":
                return f"failed: {d.get('fail_reason')}"
            return None

        outcome = poll(status, attempts=60, interval=5, sleep=self.sleep)
        if outcome != "ok":
            return PostResult(ok=False, error=f"tiktok: publish {outcome or 'timed out'}")
        if self.privacy != "PUBLIC_TO_EVERYONE":
            return PostResult(ok=True, media_id=publish_id, needs_manual_publish=True)
        vid = post_ids[0] if post_ids else None
        # TikTok accepts /@/video/<id> and redirects to the canonical handle.
        url = f"https://www.tiktok.com/@/video/{vid}" if vid else None
        return PostResult(ok=True, url=url, media_id=vid or publish_id)

    def views(self, media_id: str) -> int | None:
        if not media_id.isdigit():
            return None  # a publish_id, not a post id: nothing public to count
        try:
            token = self._access_token()
            body = self.http.post(
                f"{API}/video/query/",
                params={"fields": "id,view_count"},
                json_body={"filters": {"video_ids": [media_id]}},
                headers={"Authorization": f"Bearer {token}"},
            ).json()
        except (HttpError, RuntimeError):
            return None
        for v in (body.get("data") or {}).get("videos") or []:
            if str(v.get("id")) == media_id and isinstance(v.get("view_count"), int):
                return v["view_count"]
        return None
