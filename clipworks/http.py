"""The one place clipworks talks to the network.

stdlib only (urllib): every platform API this project uses is a handful of
JSON calls plus one binary upload, and a client library per platform would
add four dependency trees to keep current for that. More importantly, every
poster takes an `Http` instance, so tests swap in a fake and no test can
ever reach a real API by accident.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 120
UA = "clipworks/0.2"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        # Never echo query strings: access tokens travel there for Graph API.
        safe_url = url.split("?", 1)[0]
        super().__init__(f"HTTP {status} from {safe_url}: {body[:400]}")
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8", errors="replace"))


@dataclass
class Http:
    timeout: int = DEFAULT_TIMEOUT
    default_headers: dict[str, str] = field(default_factory=lambda: {"User-Agent": UA})

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        form: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        hdrs = {**self.default_headers, **(headers or {})}
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace") if err.fp else ""
            raise HttpError(err.code, url, raw) from None

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw) -> Response:
        return self.request("PUT", url, **kw)
