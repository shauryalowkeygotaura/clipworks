"""YouTube Shorts via the Data API v3 resumable upload.

Same OAuth model as football-shorts-autopilot: a refresh token minted once
in a browser (its auth_youtube.py works unchanged for this), then headless
forever. No google-api-python-client: the upload is two HTTP calls.

Quota: an upload costs 1,600 of the default 10,000 daily units. The default
cadence (3/day) leaves headroom for the stats reads reconciliation makes.

Env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN, optional
YOUTUBE_API_KEY (for view counts; the upload scope cannot read stats),
optional YT_PRIVACY (default public).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..http import Http, HttpError
from .base import PostResult, env_get

TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URI = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URI = "https://www.googleapis.com/youtube/v3/videos"
VALID_PRIVACY = frozenset({"public", "unlisted", "private"})


@dataclass
class YouTubePoster:
    http: Http
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    api_key: str = field(default="", repr=False)
    privacy: str = "public"
    platform: str = "youtube"

    @classmethod
    def from_env(cls, http: Http, env: dict[str, str] | None = None) -> "YouTubePoster":
        privacy = env_get(env, "YT_PRIVACY", "public")
        if privacy not in VALID_PRIVACY:
            raise ValueError(f"YT_PRIVACY must be one of {sorted(VALID_PRIVACY)}, got {privacy!r}")
        return cls(
            http=http,
            client_id=env_get(env, "YT_CLIENT_ID"),
            client_secret=env_get(env, "YT_CLIENT_SECRET"),
            refresh_token=env_get(env, "YT_REFRESH_TOKEN"),
            api_key=env_get(env, "YOUTUBE_API_KEY"),
            privacy=privacy,
        )

    def missing_config(self) -> list[str]:
        pairs = (
            ("YT_CLIENT_ID", self.client_id),
            ("YT_CLIENT_SECRET", self.client_secret),
            ("YT_REFRESH_TOKEN", self.refresh_token),
        )
        return [k for k, v in pairs if not v]

    def _access_token(self) -> str:
        body = self.http.post(
            TOKEN_URI,
            form={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        ).json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("youtube: token refresh returned no access_token")
        return token

    def post(self, video: Path, caption: str, title: str) -> PostResult:
        try:
            return self._post(Path(video), caption, title)
        except HttpError as err:
            if "invalid_grant" in err.body:
                return PostResult(ok=False, error="youtube: refresh token revoked or expired (invalid_grant). Re-run auth_youtube.py.")
            if "quotaExceeded" in err.body:
                return PostResult(ok=False, error="youtube: daily quota exceeded; lower cadence.youtube")
            return PostResult(ok=False, error=f"youtube: {err}")
        except RuntimeError as err:
            return PostResult(ok=False, error=str(err))

    def _post(self, video: Path, caption: str, title: str) -> PostResult:
        token = self._access_token()
        data = video.read_bytes()
        meta = {
            "snippet": {
                "title": shorts_title(title),
                "description": caption,
                "categoryId": "22",
            },
            "status": {"privacyStatus": self.privacy, "selfDeclaredMadeForKids": False},
        }
        init = self.http.post(
            UPLOAD_URI,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json_body=meta,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(len(data)),
            },
        )
        location = init.headers.get("location")
        if not location:
            return PostResult(ok=False, error="youtube: resumable init returned no upload location")
        done = self.http.put(
            location,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
        ).json()
        vid = done.get("id")
        if not vid:
            return PostResult(ok=False, error=f"youtube: upload finished without a video id: {done}")
        return PostResult(ok=True, url=f"https://www.youtube.com/shorts/{vid}", media_id=vid)

    def views(self, media_id: str) -> int | None:
        params = {"part": "statistics", "id": media_id}
        headers = {}
        if self.api_key:
            params["key"] = self.api_key
        else:
            try:
                headers["Authorization"] = f"Bearer {self._access_token()}"
            except (HttpError, RuntimeError):
                return None
        try:
            body = self.http.get(VIDEOS_URI, params=params, headers=headers).json()
        except HttpError:
            return None
        items = body.get("items") or []
        if not items:
            return None
        raw = items[0].get("statistics", {}).get("viewCount")
        return int(raw) if isinstance(raw, str) and raw.isdigit() else None


def shorts_title(text: str) -> str:
    """YouTube titles: max 100 chars, no angle brackets, never empty."""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = re.sub(r"[<>]", "", first)
    # Hashtags belong in the description; in a title they eat the 100 chars.
    first = re.sub(r"(^|\s)#\w+", "", first).strip()
    if not first:
        first = "Clip"
    if len(first) > 100:
        first = first[:97].rstrip() + "..."
    return first
