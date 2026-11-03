"""Append-only record of everything posted, plus the earnings reconciliation.

Append-only on purpose. This file is the only durable evidence of what was
posted where, and it is what eventually answers the question the whole project
exists to answer: what does a clip actually earn. Rewriting rows in place would
let a buggy reconciliation pass destroy the history it was meant to enrich, so
updates are written as new rows and the current state is folded on read.

JSONL rather than SQLite: the file is appended by a cron job that can be killed
mid-write, and a truncated final line costs one record instead of corrupting a
database. `load()` skips unparseable lines rather than raising, for the same
reason.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .models import PostJob
from .scheduler import LedgerView


@dataclass(frozen=True)
class PostRecord:
    """One post. `views` and `payout_usd` stay None until reconciled."""
    post_id: str
    clip_id: str
    job_id: str
    clip_index: int
    campaign: str
    platform: str
    source_video: str
    score: float
    caption: str
    posted_at: str                    # ISO 8601 UTC
    url: str | None = None
    media_id: str | None = None       # platform's own id, for insights lookups
    error: str | None = None          # set when the post attempt failed
    views: int | None = None
    reconciled_at: str | None = None
    payout_usd: float | None = None
    # Whop escrows campaign budget; a streamer paying direct does not. Recording
    # which kind each post belonged to is how the realised payment rate of each
    # channel becomes visible instead of being a hunch.
    campaign_source: str = "whop"     # "whop" | "discord_direct"
    # Submission lifecycle. `whop_status` mirrors Whop's own values
    # (submitted / approved / denied) or "manual_pending" when Content Rewards
    # gave us no API and the operator has to paste the URL in themselves.
    whop_submission_id: str | None = None
    whop_status: str | None = None
    denial_reason: str | None = None

    @property
    def posted_day(self) -> str:
        return self.posted_at[:10]

    @property
    def succeeded(self) -> bool:
        return self.error is None


_FIELDS = frozenset(PostRecord.__dataclass_fields__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_today() -> str:
    """Today's date in UTC, matching the date embedded in `posted_at`.

    Never `date.today()`: that is local, and every timezone east or west of UTC
    then has a window each day where the daily-cap counter disagrees with the
    stored records.
    """
    return datetime.now(timezone.utc).date().isoformat()


def post_id_for(job: PostJob) -> str:
    """Deterministic, so a retry after a crash cannot double-count a post."""
    return f"{job.clip.clip_id}:{job.platform}"


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    # --- writing ------------------------------------------------------------

    def append(self, record: PostRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def record_result(
        self,
        job: PostJob,
        campaign: str,
        url: str | None,
        media_id: str | None,
        error: str | None = None,
        campaign_source: str = "whop",
    ) -> PostRecord:
        record = PostRecord(
            post_id=post_id_for(job),
            clip_id=job.clip.clip_id,
            job_id=job.clip.job_id,
            clip_index=job.clip.index,
            campaign=campaign,
            platform=job.platform,
            source_video=job.clip.source_video,
            score=job.clip.score,
            caption=job.caption,
            posted_at=now_iso(),
            url=url,
            media_id=media_id,
            error=error,
            campaign_source=campaign_source,
        )
        self.append(record)
        return record

    def reconcile(self, post_id: str, views: int, payout_usd: float | None = None) -> None:
        """Record observed views as a NEW row. `load` keeps the latest.

        `payout_usd=None` keeps whatever payout is already recorded. The
        automated view refresh runs daily and knows nothing about money, so
        letting it write None would erase a payout entered by hand last week.
        """
        current = self.current().get(post_id)
        if current is None:
            raise KeyError(f"cannot reconcile unknown post {post_id!r}")
        self.append(
            replace(
                current,
                views=views,
                payout_usd=payout_usd if payout_usd is not None else current.payout_usd,
                reconciled_at=now_iso(),
            )
        )

    _UPDATABLE = frozenset({"whop_submission_id", "whop_status", "denial_reason", "payout_usd", "url"})

    def update(self, post_id: str, **changes) -> PostRecord:
        """Append a new row with `changes` applied. Whitelisted fields only:
        identity fields (clip, platform, posted_at) are never rewritten."""
        bad = set(changes) - self._UPDATABLE
        if bad:
            raise ValueError(f"cannot update {sorted(bad)} on a ledger row")
        current = self.current().get(post_id)
        if current is None:
            raise KeyError(f"cannot update unknown post {post_id!r}")
        new = replace(current, **changes)
        self.append(new)
        return new

    # --- reading ------------------------------------------------------------

    def load(self) -> list[PostRecord]:
        """Every row in file order. Unparseable lines are skipped, not raised:
        a cron job killed mid-write leaves a truncated final line, and losing
        one record must not make the whole ledger unreadable."""
        if not self.path.exists():
            return []
        out: list[PostRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    # Tolerate rows written by a newer version: unknown keys
                    # are dropped rather than making the row unreadable.
                    row = {k: v for k, v in row.items() if k in _FIELDS}
                    out.append(PostRecord(**row))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue
        return out

    def current(self) -> dict[str, PostRecord]:
        """Fold the append-only log into the latest state per post_id."""
        state: dict[str, PostRecord] = {}
        for record in self.load():
            state[record.post_id] = record
        return state

    def view_for(self, today: str | None = None) -> LedgerView:
        """The read-only slice the scheduler needs.

        Only SUCCESSFUL posts count. A failed attempt must not consume a daily
        slot or block a retry of the same clip, which is exactly what would
        happen if errors were folded into these two structures.
        """
        # UTC, matching `posted_at`. Using date.today() here (local) would
        # disagree with the stored UTC date for the whole 00:00-05:30 IST
        # window, during which the counter reads zero and the scheduler happily
        # posts straight past the campaign's daily cap.
        today = today or utc_today()
        posted_today: dict[str, int] = {}
        pairs: set[tuple[str, str]] = set()
        for record in self.current().values():
            if not record.succeeded:
                continue
            pairs.add((record.clip_id, record.platform))
            if record.posted_day == today:
                posted_today[record.platform] = posted_today.get(record.platform, 0) + 1
        return LedgerView(posted_today=posted_today, posted_clip_platform=frozenset(pairs))

    # --- analysis -----------------------------------------------------------

    def realised_rate(self, campaign_source: str | None = None) -> float | None:
        """Actual USD per 1,000 views, or None if nothing has been paid yet.

        This is the number that decides whether the project is worth running,
        and it is deliberately computed from reconciled rows only. Quoting a
        campaign's advertised rate back at yourself is not evidence.
        """
        views = 0
        paid = 0.0
        for record in self.current().values():
            if record.views is None or record.payout_usd is None:
                continue
            if campaign_source and record.campaign_source != campaign_source:
                continue
            views += record.views
            paid += record.payout_usd
        if views == 0:
            return None
        return paid / views * 1000

    def unreconciled(self) -> Iterator[PostRecord]:
        """Successful posts still missing a view count."""
        for record in self.current().values():
            if record.succeeded and record.views is None:
                yield record
