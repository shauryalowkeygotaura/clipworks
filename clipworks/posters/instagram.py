"""Instagram Reels via the official Graph API (Content Publishing).

Resumable upload, so the MP4 goes straight from disk to Meta. The older
`video_url` flow needs the file on a public URL, which means running a file
host just to post; this needs nothing but the token.

Requirements (one-time, operator side): an Instagram Professional account
linked to a Facebook Page, a Meta app with instagram_content_publish and
instagram_manage_insights, and a long-lived token. Long-lived tokens expire
after ~60 days; a 190 error from Graph means it is time to mint a new one,
and the pipeline surfaces exactly that instead of a raw stack trace.

Env: IG_USER_ID, IG_ACCESS_TOKEN, optional IG_GRAPH_VERSION.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..http import Http, HttpError
from .base import PostResult, Sleep, env_get, poll

GRAPH = "https://graph.facebook.com"


@dataclass
class InstagramPoster:
    http: Http
    user_id: str
    token: str = field(repr=False)
    version: str = "v23.0"
    sleep: Sleep = field(default=time.sleep)
    platform: str = "instagram"

    @classmethod
    def from_env(cls, http: Http, env: dict[str, str] | None = None) -> "InstagramPoster":
        return cls(
            http=http,
            user_id=env_get(env, "IG_USER_ID"),
            token=env_get(env, "IG_ACCESS_TOKEN"),
            version=env_get(env, "IG_GRAPH_VERSION", "v23.0"),
        )

    def missing_config(self) -> list[str]:
        return [k for k, v in (("IG_USER_ID", self.user_id), ("IG_ACCESS_TOKEN", self.token)) if not v]

    def _url(self, path: str) -> str:
        return f"{GRAPH}/{self.version}/{path.lstrip('/')}"

    def post(self, video: Path, caption: str, title: str) -> PostResult:
        try:
            return self._post(Path(video), caption)
        except HttpError as err:
            return PostResult(ok=False, error=_explain(err))

    def _post(self, video: Path, caption: str) -> PostResult:
        data = video.read_bytes()
        created = self.http.post(
            self._url(f"{self.user_id}/media"),
            form={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true",
                "access_token": self.token,
            },
        ).json()
        container = created.get("id")
        upload_uri = created.get("uri")
        if not container or not upload_uri:
            return PostResult(ok=False, error=f"instagram: container creation returned {created}")

        self.http.post(
            upload_uri,
            data=data,
            headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(len(data)),
                "Content-Type": "application/octet-stream",
            },
        )

        def status() -> str | None:
            body = self.http.get(
                self._url(container), params={"fields": "status_code,status", "access_token": self.token}
            ).json()
            code = body.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return "ok"
            if code in ("ERROR", "EXPIRED"):
                return f"error: {body.get('status') or code}"
            return None

        # Reels processing is usually 10-60s; 5 minutes is the outer bound
        # before we call it stuck rather than slow.
        outcome = poll(status, attempts=60, interval=5, sleep=self.sleep)
        if outcome != "ok":
            return PostResult(ok=False, error=f"instagram: processing {outcome or 'timed out after 5 min'}")

        published = self.http.post(
            self._url(f"{self.user_id}/media_publish"),
            form={"creation_id": container, "access_token": self.token},
        ).json()
        media_id = published.get("id")
        if not media_id:
            return PostResult(ok=False, error=f"instagram: publish returned {published}")

        permalink = None
        try:
            info = self.http.get(
                self._url(media_id), params={"fields": "permalink", "access_token": self.token}
            ).json()
            permalink = info.get("permalink")
        except HttpError:
            pass  # posted is posted; a missing permalink is recoverable later
        return PostResult(ok=True, url=permalink, media_id=str(media_id))

    def views(self, media_id: str) -> int | None:
        try:
            body = self.http.get(
                self._url(f"{media_id}/insights"), params={"metric": "views", "access_token": self.token}
            ).json()
        except HttpError:
            return None
        for metric in body.get("data") or []:
            values = metric.get("values") or []
            if values and isinstance(values[0].get("value"), int):
                return values[0]["value"]
            if isinstance(metric.get("total_value", {}).get("value"), int):
                return metric["total_value"]["value"]
        return None


def _explain(err: HttpError) -> str:
    if '"code":190' in err.body.replace(" ", ""):
        return "instagram: access token expired or revoked (Graph code 190). Mint a new long-lived token."
    return f"instagram: {err}"
