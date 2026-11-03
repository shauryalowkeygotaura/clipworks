"""A scripted stand-in for clipworks.http.Http. Every test that touches a
poster, Whop or Discord goes through this, so no test can reach a real API."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from clipworks.http import HttpError, Response


@dataclass
class FakeHttp:
    # (method, url regex) -> list of responses (dict, Response, or Exception), consumed in order;
    # the last one repeats.
    routes: list[tuple[str, str, list]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def on(self, method: str, pattern: str, *responses) -> "FakeHttp":
        self.routes.append((method, pattern, list(responses)))
        return self

    def request(self, method, url, *, params=None, json_body=None, form=None, data=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json_body,
                           "form": form, "data": data, "headers": headers or {}})
        for m, pat, responses in self.routes:
            if m == method and re.search(pat, url):
                r = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(r, Exception):
                    raise r
                if isinstance(r, Response):
                    return r
                return Response(200, {}, json.dumps(r).encode())
        raise HttpError(404, url, f"no fake route for {method} {url}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def find(self, method: str, pattern: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and re.search(pattern, c["url"])]
