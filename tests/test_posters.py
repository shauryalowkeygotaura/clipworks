from __future__ import annotations

from pathlib import Path

import pytest

from clipworks.http import HttpError, Response
from clipworks.posters.instagram import InstagramPoster
from clipworks.posters.tiktok import TikTokPoster
from clipworks.posters.youtube import YouTubePoster, shorts_title

from fakes import FakeHttp


@pytest.fixture
def mp4(tmp_path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 1234)
    return p


def no_sleep(_):
    pass


# --- instagram ---------------------------------------------------------------


def ig(http) -> InstagramPoster:
    return InstagramPoster(http=http, user_id="1789", token="TOKEN", sleep=no_sleep)


def test_instagram_full_resumable_flow(mp4):
    http = (
        FakeHttp()
        .on("POST", r"/1789/media$", {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/C1"})
        .on("POST", r"rupload\.facebook\.com", {"success": True})
        .on("GET", r"/C1$", {"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"})
        .on("POST", r"/media_publish$", {"id": "M9"})
        .on("GET", r"/M9$", {"permalink": "https://www.instagram.com/reel/abc/"})
    )
    res = ig(http).post(mp4, "cap #x", "t")
    assert res.ok and res.media_id == "M9" and res.url == "https://www.instagram.com/reel/abc/"
    create = http.find("POST", r"/1789/media$")[0]
    assert create["form"]["media_type"] == "REELS" and create["form"]["upload_type"] == "resumable"
    upload = http.find("POST", r"rupload")[0]
    assert upload["headers"]["file_size"] == "1234" and upload["data"] == mp4.read_bytes()
    assert upload["headers"]["Authorization"] == "OAuth TOKEN"


def test_instagram_processing_error_is_a_failure_not_a_publish(mp4):
    http = (
        FakeHttp()
        .on("POST", r"/1789/media$", {"id": "C1", "uri": "https://rupload.facebook.com/x/C1"})
        .on("POST", r"rupload", {"success": True})
        .on("GET", r"/C1$", {"status_code": "ERROR", "status": "bad codec"})
    )
    res = ig(http).post(mp4, "c", "t")
    assert not res.ok and "bad codec" in res.error
    assert not http.find("POST", r"media_publish")


def test_instagram_expired_token_is_explained(mp4):
    http = FakeHttp().on("POST", r"/media$", HttpError(400, "u", '{"error":{"code": 190,"message":"expired"}}'))
    res = ig(http).post(mp4, "c", "t")
    assert not res.ok and "190" in res.error and "Mint a new" in res.error


def test_instagram_views_from_insights():
    http = FakeHttp().on("GET", r"/M9/insights", {"data": [{"name": "views", "values": [{"value": 4321}]}]})
    assert ig(http).views("M9") == 4321


def test_tokens_never_appear_in_repr():
    assert "TOKEN" not in repr(ig(FakeHttp()))


# --- youtube -----------------------------------------------------------------


def yt(http, **kw) -> YouTubePoster:
    return YouTubePoster(http=http, client_id="cid", client_secret="sec", refresh_token="rt", **kw)


def test_youtube_resumable_upload(mp4):
    http = (
        FakeHttp()
        .on("POST", r"oauth2\.googleapis\.com/token", {"access_token": "AT"})
        .on("POST", r"/upload/youtube/v3/videos", Response(200, {"location": "https://up.example/session1"}, b""))
        .on("PUT", r"up\.example/session1", {"id": "vid123"})
    )
    res = yt(http).post(mp4, "the caption #a", "A great hook #shorts")
    assert res.ok and res.url == "https://www.youtube.com/shorts/vid123"
    init = http.find("POST", r"/upload/youtube")[0]
    assert init["json"]["snippet"]["title"] == "A great hook"
    assert init["json"]["snippet"]["description"] == "the caption #a"
    assert init["headers"]["X-Upload-Content-Length"] == "1234"


def test_youtube_revoked_refresh_token_is_explained(mp4):
    http = FakeHttp().on("POST", r"token", HttpError(400, "u", '{"error": "invalid_grant"}'))
    res = yt(http).post(mp4, "c", "t")
    assert not res.ok and "auth_youtube.py" in res.error


def test_youtube_views_prefers_api_key():
    http = FakeHttp().on("GET", r"/youtube/v3/videos", {"items": [{"statistics": {"viewCount": "987"}}]})
    assert yt(http, api_key="K").views("vid") == 987
    assert http.calls[0]["params"]["key"] == "K"


def test_shorts_title_rules():
    assert shorts_title("") == "Clip"
    assert shorts_title("<b>hi</b> there #tag") == "bhi/b there"
    assert len(shorts_title("x" * 300)) == 100


# --- tiktok ------------------------------------------------------------------


def tt(http, tmp_path, **kw) -> TikTokPoster:
    return TikTokPoster(http=http, client_key="ck", client_secret="cs", refresh_token="rt",
                        state_dir=tmp_path, sleep=no_sleep, **kw)


def test_tiktok_inbox_mode_is_flagged_manual(mp4, tmp_path):
    http = (
        FakeHttp()
        .on("POST", r"/oauth/token/", {"access_token": "AT", "refresh_token": "rt2"})
        .on("POST", r"/inbox/video/init/", {"data": {"publish_id": "p1", "upload_url": "https://up.tt/1"}, "error": {"code": "ok"}})
        .on("PUT", r"up\.tt", {})
    )
    res = tt(http, tmp_path).post(mp4, "cap", "t")
    assert res.ok and res.needs_manual_publish and res.url is None
    put = http.find("PUT", r"up\.tt")[0]
    assert put["headers"]["Content-Range"] == "bytes 0-1233/1234"


def test_tiktok_rotated_refresh_token_is_persisted(mp4, tmp_path):
    http = FakeHttp().on("POST", r"/oauth/token/", {"access_token": "AT", "refresh_token": "NEW"}) \
        .on("POST", r"/init/", {"data": {"publish_id": "p", "upload_url": "https://up.tt/1"}}).on("PUT", r"up", {})
    tt(http, tmp_path).post(mp4, "c", "t")
    reloaded = TikTokPoster.from_env(FakeHttp(), env={"TIKTOK_CLIENT_KEY": "ck", "TIKTOK_CLIENT_SECRET": "cs",
                                                      "TIKTOK_REFRESH_TOKEN": "OLD"}, state_dir=tmp_path)
    assert reloaded.refresh_token == "NEW"


def test_tiktok_direct_mode_returns_public_url(mp4, tmp_path):
    http = (
        FakeHttp()
        .on("POST", r"/oauth/token/", {"access_token": "AT"})
        .on("POST", r"/post/publish/video/init/", {"data": {"publish_id": "p1", "upload_url": "https://up.tt/1"}})
        .on("PUT", r"up\.tt", {})
        .on("POST", r"/status/fetch/", {"data": {"status": "PROCESSING_UPLOAD"}},
            {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7311]}})
    )
    res = tt(http, tmp_path, mode="direct").post(mp4, "cap", "t")
    assert res.ok and not res.needs_manual_publish and res.url.endswith("/video/7311")


def test_tiktok_bad_mode_rejected():
    with pytest.raises(ValueError):
        TikTokPoster.from_env(FakeHttp(), env={"TIKTOK_MODE": "yolo"})
