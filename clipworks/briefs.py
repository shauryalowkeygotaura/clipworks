"""Turn a pasted or crossposted campaign brief into a DRAFT campaign YAML.

Deterministic regex extraction, not an LLM. The compliance gate is the part
of this project that decides whether a 40k-view clip pays, and a model that
confidently invents "max 60s" when the brief said 90 would make the gate lie.
So the extractor only writes what it literally found; anything it could not
find is written as the string TODO, which `campaign.parse` rejects. A draft
therefore cannot be approved, and cannot run, until a human has read it.

The original brief is kept verbatim at the top of the draft so the human
check is a side-by-side read, not a hunt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

TODO = "TODO"

_URL = re.compile(r"https?://[^\s<>()\"']+")
_HASHTAG = re.compile(r"(?<![\w&])#([A-Za-z][A-Za-z0-9_]{1,49})")
_HANDLE_AFTER_VERB = re.compile(
    r"\b(?:tag|credit|mention|@\s*credit)\b[^@\n]{0,40}(@[A-Za-z0-9_.]{2,30})", re.IGNORECASE
)
_BOUNTY = re.compile(r"\bbnty_[A-Za-z0-9]+\b")
_RATE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*(?:usd\s*)?(?:per|/|for\s+every|every)\s*(?:1\s*k|1,000|1000|thousand)\b",
    re.IGNORECASE,
)
_CPM = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*cpm\b|\bcpm\b[^$\n]{0,10}\$\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_BUDGET = re.compile(r"\bbudget\b[^$\n]{0,20}\$\s*([\d,]+(?:\.\d+)?)\s*(k)?", re.IGNORECASE)
_UNIT = r"(s|sec|secs|seconds?|m|min|mins|minutes?)"
_RANGE = re.compile(rf"(\d+)\s*{_UNIT}?\s*(?:-|–|to)\s*(\d+)\s*{_UNIT}\b", re.IGNORECASE)
_MIN = re.compile(rf"\b(?:min(?:imum)?|at\s+least)\b[^\d\n]{{0,15}}(\d+)\s*{_UNIT}\b", re.IGNORECASE)
_MAX = re.compile(rf"\b(?:max(?:imum)?|under|no\s+longer\s+than|up\s+to)\b[^\d\n]{{0,15}}(\d+)\s*{_UNIT}\b", re.IGNORECASE)
_CAP = re.compile(
    r"\b(?:max(?:imum)?\s*)?(\d+)\s*(?:clips?|posts?|videos?|submissions?)\s*(?:per|a|/)\s*day\b", re.IGNORECASE
)

_PLATFORM_WORDS = {
    "instagram": re.compile(r"\b(instagram|insta|ig|reels?)\b", re.IGNORECASE),
    "youtube": re.compile(r"\b(youtube\s*shorts|yt\s*shorts|shorts)\b", re.IGNORECASE),
    "tiktok": re.compile(r"\b(tik\s*tok|tiktok)\b", re.IGNORECASE),
}

_BANNED_PATTERNS = {
    "ai_voiceover": re.compile(r"\bno\s+(ai|tts|robot(ic)?)\s*(voice|voiceover|narration)s?\b", re.IGNORECASE),
    "split_screen_gameplay": re.compile(r"\bno\s+(split[\s-]*screen\s+)?(gameplay|subway\s+surfers|minecraft\s+parkour)\b", re.IGNORECASE),
    "split_screen": re.compile(r"\bno\s+split[\s-]*screens?\b", re.IGNORECASE),
    "jump_cuts": re.compile(r"\bno\s+jump[\s-]*cuts?\b", re.IGNORECASE),
    "burned_captions": re.compile(r"\bno\s+(burned[\s-]*in\s+)?(subtitles|captions)\b", re.IGNORECASE),
}


@dataclass
class Draft:
    data: dict
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _seconds(value: str, unit: str | None) -> int:
    n = int(value)
    return n * 60 if unit and unit.lower().startswith("m") else n


def _slugify(text: str) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip()), "campaign")
    s = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    return (s[:40].rstrip("-") or "campaign")


def extract(text: str, slug: str | None = None) -> Draft:
    missing: list[str] = []
    notes: list[str] = []
    urls = [u.rstrip(".,;:!?)") for u in _URL.findall(text)]

    channels, playlists, videos = [], [], []
    whop_url = ""
    for u in urls:
        if re.match(r"^https://(www\.)?whop\.com/", u, re.IGNORECASE) and not whop_url:
            whop_url = u
        elif re.match(r"^https://(www\.|m\.)?(youtube\.com|youtu\.be)/", u, re.IGNORECASE):
            u = u.replace("://m.", "://www.")
            if "list=" in u and "watch?" not in u:
                playlists.append(u)
            elif "youtu.be/" in u or "watch?v=" in u or "/shorts/" in u:
                videos.append(u)
            else:
                channels.append(u)
    if not (channels or playlists or videos):
        missing.append("source (no YouTube channel/playlist/video URL found)")

    platforms = [p for p, rx in _PLATFORM_WORDS.items() if rx.search(text)]
    if not platforms:
        missing.append("platforms")

    hashtags = list(dict.fromkeys(f"#{h}" for h in _HASHTAG.findall(text)))
    credit_m = _HANDLE_AFTER_VERB.search(text)
    credit = credit_m.group(1) if credit_m else ""
    if not credit:
        notes.append("no credit/tag requirement found; confirm the brief has none")

    min_sec: int | str = TODO
    max_sec: int | str = TODO
    rng = _RANGE.search(text)
    if rng:
        unit = rng.group(4) or rng.group(2)
        min_sec, max_sec = _seconds(rng.group(1), rng.group(2) or unit), _seconds(rng.group(3), unit)
    else:
        if (m := _MIN.search(text)):
            min_sec = _seconds(m.group(1), m.group(2))
        if (m := _MAX.search(text)):
            max_sec = _seconds(m.group(1), m.group(2))
    if min_sec == TODO:
        missing.append("rules.min_duration_sec")
    if max_sec == TODO:
        missing.append("rules.max_duration_sec")

    cap_m = _CAP.search(text)
    cap: int | str = int(cap_m.group(1)) if cap_m else TODO
    if cap == TODO:
        missing.append("rules.per_clipper_daily_cap")

    banned = sorted(name for name, rx in _BANNED_PATTERNS.items() if rx.search(text))

    rate = None
    if (m := _RATE.search(text)):
        rate = float(m.group(1))
    elif (m := _CPM.search(text)):
        rate = float(m.group(1) or m.group(2))
    budget = None
    if (m := _BUDGET.search(text)):
        budget = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)

    bounty = _BOUNTY.search(text)
    source_kind = "whop" if (whop_url or bounty) else "discord_direct"
    if source_kind == "discord_direct":
        notes.append("no Whop link found: assumed streamer pays direct (no escrow). Get payment terms in writing.")

    data: dict = {
        "slug": slug or _slugify(text),
        "source_kind": source_kind,
        "platforms": platforms or [TODO],
        "source": {"channels": channels, "playlists": playlists, "videos": videos},
        "rules": {
            "min_duration_sec": min_sec,
            "max_duration_sec": max_sec,
            "required_hashtags": hashtags,
            "required_credit_text": credit,
            "caption_template": "{hook}\n\n{credit} {hashtags}",
            "banned_styles": banned,
            "per_clipper_daily_cap": cap,
        },
    }
    if rate is not None or budget is not None:
        data["payout"] = {k: v for k, v in (("rate_per_1k_views_usd", rate), ("budget_remaining_usd", budget)) if v is not None}
    if whop_url or bounty:
        data["whop"] = {k: v for k, v in (("campaign_url", whop_url), ("bounty_id", bounty.group(0) if bounty else "")) if v}
    return Draft(data=data, missing=missing, notes=notes)


def render(draft: Draft, brief: str) -> str:
    """YAML text with the brief and the reviewer's checklist as comments."""
    header = ["# DRAFT campaign generated from a brief. NOT active.", "#"]
    if draft.missing:
        header.append("# MISSING (left as TODO, approve will refuse until filled):")
        header += [f"#   - {m}" for m in draft.missing]
    if draft.notes:
        header.append("# CHECK:")
        header += [f"#   - {n}" for n in draft.notes]
    header += ["#", "# Approve with:  clipworks campaign approve <this file>", "#", "# ---- original brief ----"]
    header += [f"# {ln}" if ln else "#" for ln in brief.strip().splitlines()]
    header.append("# ------------------------")
    body = yaml.safe_dump(draft.data, sort_keys=False, allow_unicode=True, width=1000)
    return "\n".join(header) + "\n" + body
