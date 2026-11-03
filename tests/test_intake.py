from __future__ import annotations

import pytest
import yaml

from clipworks import briefs
from clipworks.campaign import CampaignError, parse
from clipworks.discord import BriefReader, Notifier, _chunks
from clipworks.http import HttpError

from fakes import FakeHttp

BRIEF = """NEW CAMPAIGN: Creator Clips
Clip from https://www.youtube.com/@Creator
Post to TikTok, YouTube Shorts and Instagram Reels
$1.50 per 1k views, budget $10k
Clips must be 15-60 seconds. Max 3 clips per day.
Tag @creator and use #creator #ad
No AI voiceovers. No split screen.
Submit here: https://whop.com/discover/creator-clips/
"""


def test_complete_brief_becomes_a_valid_campaign():
    d = briefs.extract(BRIEF)
    assert d.missing == []
    c = parse(d.data)
    assert set(c.platforms) == {"instagram", "youtube", "tiktok"}
    assert (c.rules.min_duration_sec, c.rules.max_duration_sec) == (15, 60)
    assert c.rules.per_clipper_daily_cap == 3
    assert c.rules.required_credit_text == "@creator"
    assert c.rules.required_hashtags == ("#creator", "#ad")
    assert {"ai_voiceover", "split_screen"} <= c.rules.banned_styles
    assert c.rate_per_1k_views_usd == 1.5 and c.budget_remaining_usd == 10000
    assert c.whop.campaign_url.startswith("https://whop.com/")


def test_incomplete_brief_cannot_be_approved():
    """The core safety property: a guess never becomes a live rule."""
    d = briefs.extract("clip my streams https://www.youtube.com/@x on tiktok, pays well")
    assert "rules.min_duration_sec" in d.missing and "rules.per_clipper_daily_cap" in d.missing
    with pytest.raises(CampaignError):
        parse(yaml.safe_load(briefs.render(d, "x")))


def test_minutes_are_converted():
    d = briefs.extract("tiktok https://youtu.be/aaaaaaaaaaa at least 30 seconds, max 2 minutes, 5 posts per day")
    assert d.data["rules"]["min_duration_sec"] == 30
    assert d.data["rules"]["max_duration_sec"] == 120


def test_no_whop_link_means_direct_pay_with_warning():
    d = briefs.extract("tiktok https://youtu.be/aaaaaaaaaaa 10-50 seconds 2 clips per day")
    assert d.data["source_kind"] == "discord_direct"
    assert any("escrow" in n for n in d.notes)


def test_rendered_draft_keeps_the_brief_verbatim():
    out = briefs.render(briefs.extract(BRIEF), BRIEF)
    assert "# Tag @creator and use #creator #ad" in out


def test_bounty_id_is_extracted():
    d = briefs.extract(BRIEF + "\nbounty: bnty_ABC123")
    assert d.data["whop"]["bounty_id"] == "bnty_ABC123"
    assert parse(d.data).whop.bounty_id == "bnty_ABC123"


# --- campaign whop/cadence validation -----------------------------------------


def base():
    return briefs.extract(BRIEF).data


def test_cadence_for_unlisted_platform_rejected():
    raw = {**base(), "platforms": ["tiktok"], "cadence": {"instagram": 2}}
    with pytest.raises(CampaignError, match="cadence"):
        parse(raw)


def test_bad_bounty_id_rejected():
    with pytest.raises(CampaignError, match="bnty_"):
        parse({**base(), "whop": {"bounty_id": "1234"}})


def test_non_whop_url_rejected():
    with pytest.raises(CampaignError, match="whop.com"):
        parse({**base(), "whop": {"campaign_url": "https://whop.evil.com/x"}})


# --- discord ------------------------------------------------------------------


def test_first_poll_records_head_without_flooding():
    http = FakeHttp().on("GET", r"/channels/C1/messages", [{"id": "500", "content": "old brief", "author": {"username": "a"}}])
    r = BriefReader(http, token="T", channel_ids=("C1",))
    cursors: dict = {}
    assert r.fetch_new(cursors) == []
    assert cursors == {"C1": "500"}


def test_followed_announcement_crossposts_are_read_but_other_bots_are_not():
    msgs = [
        {"id": "502", "content": "", "author": {"username": "Streamer", "bot": True}, "webhook_id": "w",
         "embeds": [{"title": "New campaign", "description": "15-60 seconds"}]},
        {"id": "501", "content": "random bot spam", "author": {"username": "MEE6", "bot": True}},
    ]
    http = FakeHttp().on("GET", r"/messages", msgs)
    out = BriefReader(http, token="T", channel_ids=("C1",)).fetch_new({"C1": "500"})
    assert [m.id for m in out] == ["502"]
    assert "15-60 seconds" in out[0].content
    assert http.calls[0]["params"]["after"] == "500"


def test_unreadable_channel_does_not_crash():
    http = FakeHttp().on("GET", r"/messages", HttpError(403, "u", "Missing Access"))
    assert BriefReader(http, token="T", channel_ids=("C1",)).fetch_new({"C1": "1"}) == []


def test_notifier_never_pings_everyone_and_splits_long_messages():
    http = FakeHttp().on("POST", r"hooks", {})
    Notifier(http, webhook="https://discord.com/api/webhooks/x").send("@everyone " + "a\n" * 1500)
    assert len(http.calls) == 2
    assert all(c["json"]["allowed_mentions"] == {"parse": []} for c in http.calls)
    assert all(len(c["json"]["content"]) <= 2000 for c in http.calls)


def test_chunks_roundtrip():
    text = "\n".join(f"line {i}" for i in range(1000))
    assert all(len(c) <= 100 for c in _chunks(text, 100))
