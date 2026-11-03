"""Drive publikclip and collect its rendered clips.

Thin on purpose. publikclip owns ingest, ASR, diarization, scoring, camera
tracking, captioning and rendering, and it already has its own job queue with
per-stage checkpoints. This module does three things: spawn it, follow its
JSONL, and read the artefacts back as `RenderedClip`s.

What we deliberately do NOT do here is retry a failed run from scratch. A run
is minutes of GPU-bound work over gigabytes of intermediate audio, and
publikclip resumes from the last good stage. `run()` therefore returns the job
id on failure so the caller can `resume()` rather than pay for the whole thing
again. That is not a nicety: the first real run died on a broken 1.6 GB model
download partway through ASR, and resume picked up from the completed ingest
checkpoint.

Checkpoint shapes here were read off a real run, not the README:
  <job_dir>/<stage>.json == {"stage", "schema_version", "created_at", "data"}
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .models import Campaign, RenderedClip

ProgressFn = Callable[[str, float, str], None]


class GenerateError(RuntimeError):
    """publikclip could not be driven at all (missing binary, bad invocation)."""


@dataclass(frozen=True)
class RunResult:
    ok: bool
    job_id: str | None
    clips: tuple[RenderedClip, ...] = ()
    error: str | None = None

    @property
    def resumable(self) -> bool:
        """A failure with a job id can be resumed; one without cannot, because
        publikclip never got far enough to create the job."""
        return not self.ok and self.job_id is not None


def run(
    source: str,
    campaign: Campaign,
    publikclip_dir: Path,
    venv: str | None = None,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    args = ["run", source, "--llm", campaign.publikclip.llm]
    if campaign.publikclip.captions:
        args += ["--captions", campaign.publikclip.captions]
    if campaign.publikclip.camera:
        args += ["--camera", campaign.publikclip.camera]
    return _drive(args, source, publikclip_dir, venv, on_progress)


def resume(
    job_id: str,
    source: str,
    campaign: Campaign,
    publikclip_dir: Path,
    venv: str | None = None,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    return _drive(["resume", job_id], source, publikclip_dir, venv, on_progress)


def _drive(
    subcommand: list[str],
    source: str,
    publikclip_dir: Path,
    venv: str | None,
    on_progress: ProgressFn | None,
) -> RunResult:
    env = dict(os.environ)
    if venv:
        env["UV_PROJECT_ENVIRONMENT"] = venv

    cmd = ["uv", "run", "publikclip", "--jsonl", *subcommand]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(publikclip_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as err:
        raise GenerateError(f"cannot launch uv/publikclip in {publikclip_dir}: {err}") from err

    job_id: str | None = None
    job_dir: Path | None = None
    result: dict | None = None

    assert proc.stdout is not None
    for event in _events(proc.stdout):
        kind = event.get("event")
        if kind == "job":
            job_id = event.get("job_id")
            raw_dir = event.get("dir")
            job_dir = Path(raw_dir) if raw_dir else None
        elif kind == "progress" and on_progress:
            on_progress(
                str(event.get("stage", "")),
                float(event.get("fraction", -1) or -1),
                str(event.get("message", "")),
            )
        elif kind == "result":
            result = event

    stderr = (proc.stderr.read() if proc.stderr else "") or ""
    proc.wait()

    if result is None:
        # No result line at all: publikclip died hard (traceback, OOM, killed).
        # Surface its stderr, because the exit code alone is never actionable.
        return RunResult(
            ok=False,
            job_id=job_id,
            error=f"publikclip exited {proc.returncode} without a result: {_last_line(stderr)}",
        )
    if not result.get("ok"):
        return RunResult(ok=False, job_id=job_id, error=str(result.get("error") or "unknown"))
    if job_dir is None:
        return RunResult(ok=False, job_id=job_id, error="publikclip reported ok but gave no job dir")

    return RunResult(ok=True, job_id=job_id, clips=tuple(read_outputs(job_dir, source)))


def _events(stream) -> Iterator[dict]:
    """Yield JSON objects from publikclip's --jsonl stdout.

    Non-JSON lines are skipped rather than fatal: a warning printed by a
    dependency onto stdout must not abort a run that is otherwise fine.
    """
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "(no stderr)"


def read_checkpoint(job_dir: Path, stage: str) -> dict | None:
    """Read one stage checkpoint's `data` payload, or None if absent."""
    path = Path(job_dir) / f"{stage}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def read_outputs(job_dir: Path, source: str, origin: str = "") -> list[RenderedClip]:
    """Turn `<job_dir>/render.json` into RenderedClips.

    Clips whose file has vanished are skipped rather than returned: publikclip
    verified them at render time, but the ledger and the poster both assume the
    path is real, and a missing file should fail here where it is diagnosable.
    """
    job_dir = Path(job_dir)
    render = read_checkpoint(job_dir, "render")
    if not render:
        return []
    hooks = _hooks_by_index(job_dir)
    job_id = job_dir.name

    clips: list[RenderedClip] = []
    for entry in render.get("outputs") or []:
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get("path", "")))
        if not path.exists():
            continue
        index = int(entry.get("clip", len(clips)))
        clips.append(
            RenderedClip(
                job_id=job_id,
                index=index,
                path=path,
                score=float(entry.get("score") or 0.0),
                best_platform=str(entry.get("best_platform") or "instagram"),
                duration=float(entry.get("duration") or 0.0),
                source_video=source,
                hook=hooks.get(index, ""),
                origin=origin,
            )
        )
    return clips


def _hooks_by_index(job_dir: Path) -> dict[int, str]:
    """Best-effort caption hook per clip, pulled from the score checkpoint.

    Defensive about the field name: the scoring payload is publikclip's, it is
    not part of any contract with us, and a caption hook is a nice-to-have. If
    the shape changes, captions fall back to the campaign template's other
    fields rather than the run failing.
    """
    score = read_checkpoint(job_dir, "score")
    if not score:
        return {}
    out: dict[int, str] = {}
    for i, clip in enumerate(score.get("clips") or []):
        if not isinstance(clip, dict):
            continue
        for key in ("hook", "title", "summary", "text", "transcript"):
            value = clip.get(key)
            if isinstance(value, str) and value.strip():
                out[i] = value.strip()
                break
    return out
