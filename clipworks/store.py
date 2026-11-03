"""Everything the pipeline persists between cycles, under runs/.

  state.json            seen source videos, resumable job ids, Discord cursors
  clips/<slug>.jsonl    rendered clip inventory per campaign
  plans/<slug>/<day>.json  that day's posting plan, with per-job status
  quarantine.jsonl      clips compliance refused, with every reason
  inflight/<post_id>    marker written BEFORE an upload, removed after
  dryrun.jsonl          what would have been posted without credentials

The inflight marker is the crash-safety argument. If the process dies after
the platform accepted a video but before the ledger row is written, the next
cycle finds the marker with no ledger row and does NOT retry: retrying would
double-post, and double-posting identical clips is what gets accounts
flagged. It reports "outcome unknown, check the account" instead.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import PostJob, RenderedClip


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def clip_to_dict(c: RenderedClip) -> dict:
    d = asdict(c)
    d["path"] = str(c.path)
    return d


def clip_from_dict(d: dict) -> RenderedClip:
    fields = RenderedClip.__dataclass_fields__
    d = {k: v for k, v in d.items() if k in fields}
    d["path"] = Path(d["path"])
    return RenderedClip(**d)


class Store:
    def __init__(self, runs_dir: Path):
        self.root = Path(runs_dir)

    # --- state ---------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt state file must not stop posting. The worst case of an
            # empty state is re-resolving sources, which the clip inventory and
            # ledger dedupe anyway.
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        _atomic_write(self._state_path, json.dumps(state, indent=2, sort_keys=True))

    # --- clips ---------------------------------------------------------------

    def clips(self, slug: str) -> list[RenderedClip]:
        out: dict[str, RenderedClip] = {}
        for row in _read_jsonl(self.root / "clips" / f"{slug}.jsonl"):
            try:
                c = clip_from_dict(row)
            except (KeyError, TypeError):
                continue
            out[c.clip_id] = c
        return list(out.values())

    def add_clips(self, slug: str, clips: list[RenderedClip]) -> None:
        for c in clips:
            _append_jsonl(self.root / "clips" / f"{slug}.jsonl", clip_to_dict(c))

    # --- plans ---------------------------------------------------------------

    def _plan_path(self, slug: str, day: str) -> Path:
        return self.root / "plans" / slug / f"{day}.json"

    def plan(self, slug: str, day: str) -> list[dict] | None:
        path = self._plan_path(slug, day)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    def plans_for_day(self, day: str) -> dict[str, list[dict]]:
        out = {}
        base = self.root / "plans"
        if not base.exists():
            return out
        for d in base.iterdir():
            p = self.plan(d.name, day)
            if p is not None:
                out[d.name] = p
        return out

    def save_plan(self, slug: str, day: str, entries: list[dict]) -> None:
        _atomic_write(self._plan_path(slug, day), json.dumps(entries, indent=2))

    @staticmethod
    def job_to_entry(job: PostJob) -> dict:
        return {
            "post_id": f"{job.clip.clip_id}:{job.platform}",
            "platform": job.platform,
            "caption": job.caption,
            "scheduled_for": job.scheduled_for,
            "clip": clip_to_dict(job.clip),
            "status": "planned",
            "detail": "",
        }

    @staticmethod
    def entry_to_job(e: dict) -> PostJob:
        return PostJob(
            clip=clip_from_dict(e["clip"]),
            platform=e["platform"],
            caption=e["caption"],
            scheduled_for=e["scheduled_for"],
        )

    # --- quarantine / dry run ------------------------------------------------

    def quarantine(self, row: dict) -> bool:
        """Append unless this (clip, platform, reasons) is already there."""
        path = self.root / "quarantine.jsonl"
        key = (row.get("clip_id"), row.get("platform"), tuple(row.get("reasons") or ()))
        for existing in _read_jsonl(path):
            if (existing.get("clip_id"), existing.get("platform"), tuple(existing.get("reasons") or ())) == key:
                return False
        _append_jsonl(path, row)
        return True

    def quarantined(self) -> list[dict]:
        return _read_jsonl(self.root / "quarantine.jsonl")

    def log_dry_run(self, row: dict) -> None:
        _append_jsonl(self.root / "dryrun.jsonl", row)

    # --- inflight ------------------------------------------------------------

    def _inflight(self, post_id: str) -> Path:
        return self.root / "inflight" / post_id.replace(":", "_").replace("/", "_")

    def mark_inflight(self, post_id: str) -> None:
        _atomic_write(self._inflight(post_id), post_id)

    def is_inflight(self, post_id: str) -> bool:
        return self._inflight(post_id).exists()

    def clear_inflight(self, post_id: str) -> None:
        try:
            self._inflight(post_id).unlink()
        except FileNotFoundError:
            pass
