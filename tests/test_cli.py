from __future__ import annotations

import pytest

from clipworks import cli

from test_intake import BRIEF


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("IG_USER_ID", "IG_ACCESS_TOKEN", "YT_CLIENT_ID", "TIKTOK_CLIENT_KEY", "DISCORD_WEBHOOK_URL",
              "DISCORD_BOT_TOKEN", "WHOP_USER_TOKEN", "PUBLIKCLIP_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLIPWORKS_HOME", str(tmp_path))
    return tmp_path


def test_brief_to_active_campaign_round_trip(home, capsys):
    brief = home / "brief.txt"
    brief.write_text(BRIEF)
    assert cli.main(["brief", str(brief), "--slug", "creator"]) == 0
    draft = home / "campaigns" / "drafts" / "creator.yaml"
    assert draft.exists()
    assert cli.main(["campaign", "approve", str(draft)]) == 0
    assert (home / "campaigns" / "active" / "creator.yaml").exists()
    assert cli.main(["campaign", "list"]) == 0
    assert "creator" in capsys.readouterr().out


def test_draft_with_todo_is_refused(home, capsys):
    brief = home / "b.txt"
    brief.write_text("clip https://www.youtube.com/@x for tiktok")
    cli.main(["brief", str(brief), "--slug", "x"])
    assert cli.main(["campaign", "approve", str(home / "campaigns" / "drafts" / "x.yaml")]) == 1
    assert "TODO" in capsys.readouterr().err


def test_doctor_and_cycle_run_with_nothing_configured(home):
    assert cli.main(["doctor"]) == 1  # reports problems, does not crash
    assert cli.main(["cycle"]) == 0
    assert cli.main(["status"]) == 0
