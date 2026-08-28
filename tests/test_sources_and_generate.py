from __future__ import annotations

import json
import io
from pathlib import Path

import pytest

from clipworks import sources
from clipworks.generate import RunResult, _events, read_checkpoint, read_outputs
from clipworks.sources import CookiesRequired, SourceError, _classify, _is_single_video

# --- sources: URL whitelist -------------------------------------------------


def test_non_youtube_source_is_rejected():
    """Whitelist, not sanitize: these strings reach a subprocess argv."""
    cfg = sources.ResolveConfig()
    for bad in (
        "https://evil.example.com/x",
        "file:///etc/passwd",
        "ftp://youtube.com/x",
        "https://youtube.evil.com/watch?v=aaaaaaaaaaa",
    ):
        with pytest.raises(SourceError, match="unsupported source"):
            sources._expand(bad, cfg)


def test_youtube_hosts_are_accepted():
    assert _is_single_video("https://youtu.be/Ks-_Mh1QhMc")
    assert _is_single_video("https://www.youtube.com/watch?v=Ks-_Mh1QhMc")
    assert not _is_single_video("https://www.youtube.com/@somecreator")


def test_single_video_needs_no_network_expansion():
    url = "https://www.youtube.com/watch?v=Ks-_Mh1QhMc"
    assert sources._expand(url, sources.ResolveConfig()) == [url]


# --- sources: yt-dlp error classification -----------------------------------


def test_bot_check_becomes_a_distinct_actionable_exception():
    """This is the real failure the first live run hit. yt-dlp reports it as a
    bare exit 1, and 'needs cookies' vs 'bad URL' need opposite responses."""
    stderr = (
        "ERROR: [youtube] Ks-_Mh1QhMc: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )
    err = _classify("https://youtu.be/x", stderr)
    assert isinstance(err, CookiesRequired)
    assert "cookies_from_browser" in str(err)


def test_private_video_is_not_mistaken_for_a_bot_check():
    err = _classify("u", "ERROR: [youtube] x: Private video. Sign in if you've been granted access")
    assert not isinstance(err, CookiesRequired)


def test_unavailable_video_classified():
    err = _classify("u", "ERROR: [youtube] x: Video unavailable")
    assert isinstance(err, SourceError) and not isinstance(err, CookiesRequired)
    assert "unavailable" in str(err)


def test_unknown_stderr_still_surfaces_the_first_line():
    err = _classify("u", "ERROR: something nobody predicted\nmore detail")
    assert "something nobody predicted" in str(err)


def test_empty_stderr_does_not_produce_a_useless_message():
    assert "no stderr" in str(_classify("u", ""))


# --- generate: JSONL parsing ------------------------------------------------


def test_events_skips_non_json_noise():
    """A dependency printing a warning to stdout must not abort the run."""
    stream = io.StringIO(
        '{"event": "job", "job_id": "j1", "dir": "/tmp/j1"}\n'
        "some library warning that is not json\n"
        "\n"
        '{"event": "progress", "stage": "asr", "fraction": 0.5, "message": "x"}\n'
        '{"event": "result", "ok": true}\n'
    )
    events = list(_events(stream))
    assert [e["event"] for e in events] == ["job", "progress", "result"]


def test_events_ignores_json_that_is_not_an_object():
    stream = io.StringIO('"just a string"\n[1,2,3]\n{"event": "result", "ok": true}\n')
    assert [e["event"] for e in _events(stream)] == ["result"]


def test_run_result_resumability():
    """A failure WITH a job id is resumable; without one publikclip never got
    far enough to create the job, so resuming is impossible."""
    assert RunResult(ok=False, job_id="j1").resumable
    assert not RunResult(ok=False, job_id=None).resumable
    assert not RunResult(ok=True, job_id="j1").resumable


# --- generate: reading checkpoints off a real job dir shape -----------------


def write_checkpoint(job_dir: Path, stage: str, data: dict) -> None:
    """Mirrors the real wrapper observed on a live run:
    {"stage", "schema_version", "created_at", "data"}"""
    (job_dir / f"{stage}.json").write_text(
        json.dumps({"stage": stage, "schema_version": 1, "created_at": 0.0, "data": data}),
        encoding="utf-8",
    )


def test_read_checkpoint_unwraps_the_data_envelope(tmp_path: Path):
    write_checkpoint(tmp_path, "ingest", {"title": "t", "probe": {"width": 1920}})
    assert read_checkpoint(tmp_path, "ingest") == {"title": "t", "probe": {"width": 1920}}


def test_read_checkpoint_returns_none_for_missing_stage(tmp_path: Path):
    assert read_checkpoint(tmp_path, "render") is None


def test_read_checkpoint_survives_corrupt_json(tmp_path: Path):
    (tmp_path / "render.json").write_text("{not json", encoding="utf-8")
    assert read_checkpoint(tmp_path, "render") is None


def test_read_outputs_builds_clips(tmp_path: Path):
    job_dir = tmp_path / "20260815-215030-c7e457"
    job_dir.mkdir()
    mp4 = job_dir / "clip_00.mp4"
    mp4.write_bytes(b"x")
    write_checkpoint(job_dir, "render", {"outputs": [
        {"clip": 0, "path": str(mp4), "score": 0.82, "best_platform": "tiktok", "duration": 41.5},
    ]})
    write_checkpoint(job_dir, "score", {"clips": [{"hook": "the good bit"}]})

    clips = read_outputs(job_dir, source="https://youtu.be/src")
    assert len(clips) == 1
    c = clips[0]
    assert c.job_id == "20260815-215030-c7e457"
    assert c.clip_id == "20260815-215030-c7e457:00"
    assert c.score == 0.82
    assert c.best_platform == "tiktok"
    assert c.duration == 41.5
    assert c.hook == "the good bit"
    assert c.source_video == "https://youtu.be/src"


def test_read_outputs_skips_clips_whose_file_vanished(tmp_path: Path):
    """publikclip verified it at render time, but the poster assumes the path
    is real. Better to drop it here where it is diagnosable."""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    write_checkpoint(job_dir, "render", {"outputs": [
        {"clip": 0, "path": str(job_dir / "gone.mp4"), "score": 0.9,
         "best_platform": "instagram", "duration": 30.0},
    ]})
    assert read_outputs(job_dir, source="s") == []


def test_read_outputs_without_a_render_checkpoint_is_empty(tmp_path: Path):
    assert read_outputs(tmp_path, source="s") == []


def test_hook_falls_back_across_field_names(tmp_path: Path):
    """The scoring payload is publikclip's, not a contract with us. A renamed
    field should cost the caption hook, not the run."""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    mp4 = job_dir / "clip_00.mp4"
    mp4.write_bytes(b"x")
    write_checkpoint(job_dir, "render", {"outputs": [
        {"clip": 0, "path": str(mp4), "score": 0.9, "best_platform": "instagram", "duration": 30.0},
    ]})
    write_checkpoint(job_dir, "score", {"clips": [{"summary": "fallback text"}]})
    assert read_outputs(job_dir, "s")[0].hook == "fallback text"


def test_unknown_score_shape_yields_empty_hook_not_a_crash(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    mp4 = job_dir / "clip_00.mp4"
    mp4.write_bytes(b"x")
    write_checkpoint(job_dir, "render", {"outputs": [
        {"clip": 0, "path": str(mp4), "score": 0.9, "best_platform": "instagram", "duration": 30.0},
    ]})
    write_checkpoint(job_dir, "score", {"clips": [{"mystery_field": 123}]})
    assert read_outputs(job_dir, "s")[0].hook == ""
