"""Discord, the two legitimate halves.

1. **Notify** (webhook): run summaries, failures, "submit this to Whop",
   "publish this TikTok draft". A webhook is a URL; no bot needed.

2. **Brief intake** (bot, read-only): a bot in YOUR server reads channels you
   point it at and turns each new campaign post into a draft campaign YAML.
   To get a streamer's campaign channel into your server, use Discord's
   *Follow* button on their announcement channel: Discord itself crossposts
   every new message into your channel, and the bot reads it there.

What this deliberately does NOT do: log in as your user account to read
servers the bot is not in. That is a self-bot, Discord terminates accounts
for it, and the account it would burn is the one the streamers know you by.
If a server's campaign channel is not an announcement channel, that server
stays manual: paste the brief into your intake channel and the bot picks it up.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

from .http import Http, HttpError

API = "https://discord.com/api/v10"
MAX_LEN = 2000


@dataclass
class Notifier:
    http: Http
    webhook: str = field(default="", repr=False)

    def send(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        print(f"[notify] {text}", file=sys.stderr)
        if not self.webhook:
            return False
        ok = True
        for part in _chunks(text, MAX_LEN):
            try:
                # allowed_mentions none: a caption containing @everyone must
                # not ping your whole server.
                self.http.post(self.webhook, json_body={"content": part, "allowed_mentions": {"parse": []}})
            except HttpError as err:
                print(f"[notify] webhook failed: {err}", file=sys.stderr)
                ok = False
        return ok


@dataclass(frozen=True)
class Message:
    id: str
    channel_id: str
    author: str
    content: str
    is_bot: bool


@dataclass
class BriefReader:
    http: Http
    token: str = field(default="", repr=False)
    channel_ids: tuple[str, ...] = ()

    def available(self) -> bool:
        return bool(self.token and self.channel_ids)

    def fetch_new(self, last_seen: dict[str, str]) -> list[Message]:
        """Messages newer than `last_seen[channel]`, oldest first.

        On a channel's first poll we only record the current head and return
        nothing: turning a channel's whole history into drafts on day one is
        a flood of stale, expired campaigns.
        """
        out: list[Message] = []
        for cid in self.channel_ids:
            params: dict[str, str | int] = {"limit": 100}
            first_poll = cid not in last_seen
            if first_poll:
                params["limit"] = 1
            else:
                params["after"] = last_seen[cid]
            try:
                rows = self.http.get(
                    f"{API}/channels/{cid}/messages",
                    params=params,
                    headers={"Authorization": f"Bot {self.token}"},
                ).json()
            except HttpError as err:
                print(f"[discord] cannot read channel {cid}: {err}", file=sys.stderr)
                continue
            if not isinstance(rows, list):
                continue
            rows.sort(key=lambda m: int(m.get("id", 0)))
            if rows:
                last_seen[cid] = str(rows[-1]["id"])
            elif first_poll:
                last_seen[cid] = "0"
            if first_poll:
                continue
            for m in rows:
                author = m.get("author") or {}
                content = _flatten(m)
                if not content.strip():
                    continue
                out.append(
                    Message(
                        id=str(m["id"]),
                        channel_id=cid,
                        author=str(author.get("username") or "?"),
                        content=content,
                        # Crossposted announcements arrive as webhook messages
                        # with author.bot=True; those are exactly the briefs we
                        # want, so only skip a bot that is NOT a crosspost.
                        is_bot=bool(author.get("bot")) and not m.get("webhook_id"),
                    )
                )
        return [m for m in out if not m.is_bot]


def _flatten(msg: dict) -> str:
    """Message text plus embed text: many campaign posts are all embed."""
    parts = [msg.get("content") or ""]
    for emb in msg.get("embeds") or []:
        parts += [emb.get("title") or "", emb.get("description") or ""]
        for f in emb.get("fields") or []:
            parts.append(f"{f.get('name', '')}: {f.get('value', '')}")
        if emb.get("url"):
            parts.append(emb["url"])
    return "\n".join(p for p in parts if p).strip()


def _chunks(text: str, n: int) -> list[str]:
    out = []
    while text:
        if len(text) <= n:
            out.append(text)
            break
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > 0 else n
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out
