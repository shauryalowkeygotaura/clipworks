"""Getting posted clips in front of Whop.

What Whop actually exposes (checked against docs.whop.com, Nov 2026):

* **Bounties** have a public API: `POST /api/v1/bounty_submissions` with a
  `bounty_id` and a deliverable of up to 10 URLs. It needs a *user* OAuth
  credential with `bounty:submission:create`; account API keys are refused.
  Approval stays in the dashboard; we can only read the outcome back.
* **Content Rewards** (the clipping campaigns) has NO submission API and no
  CLI command. The official `whop` CLI covers commerce, payouts, bounties and
  ads, not Content Rewards.

So a campaign with `whop.bounty_id` is submitted end to end. A Content Rewards
campaign gets the next best thing: every posted URL is queued as
`manual_pending`, pinged to Discord with the campaign link, and marked done
with `clipworks whop done <post_id>` once pasted in. We do not browser-drive
the Whop dashboard: that is the same account-termination risk as a Discord
self-bot, on the account that holds the money.
"""
from __future__ import annotations

from dataclasses import dataclass

from .http import Http, HttpError
from .ledger import Ledger, PostRecord
from .models import Campaign

API = "https://api.whop.com/api/v1"
MANUAL = "manual_pending"
TERMINAL = frozenset({"approved", "denied"})


@dataclass(frozen=True)
class SubmitOutcome:
    status: str               # submitted | manual_pending | error | skipped
    submission_id: str | None = None
    message: str = ""


@dataclass
class WhopClient:
    http: Http
    token: str = ""

    def available(self) -> bool:
        return bool(self.token)

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key[:255]
        return h

    def submit(self, record: PostRecord, campaign: Campaign) -> SubmitOutcome:
        if campaign.source_kind != "whop":
            return SubmitOutcome("skipped", message="discord_direct campaign: submit per the streamer's instructions")
        if not record.url:
            return SubmitOutcome("skipped", message="no public URL yet")
        bounty = campaign.whop.bounty_id
        if not bounty:
            return SubmitOutcome(MANUAL, message="Content Rewards has no submission API")
        if not self.available():
            return SubmitOutcome(MANUAL, message="CLIPWORKS_WHOP_USER_TOKEN not set; submit by hand")
        try:
            body = self.http.post(
                f"{API}/bounty_submissions",
                json_body={
                    "bounty_id": bounty,
                    "deliverable": {"urls": [record.url], "caption": record.caption[:500]},
                },
                # Deterministic: a retry after a crash cannot double-submit.
                headers=self._headers(idempotency_key=f"clipworks:{record.post_id}"),
            ).json()
        except HttpError as err:
            return SubmitOutcome("error", message=f"whop: {err}")
        sid = body.get("id")
        if not sid:
            return SubmitOutcome("error", message=f"whop: no submission id in {body}")
        return SubmitOutcome(str(body.get("status") or "submitted"), submission_id=str(sid))

    def status(self, submission_id: str) -> tuple[str | None, str | None]:
        body = self.http.get(f"{API}/bounty_submissions/{submission_id}", headers=self._headers()).json()
        return body.get("status"), body.get("denial_reason")


def sync(ledger: Ledger, client: WhopClient) -> list[PostRecord]:
    """Pull approval outcomes for open API submissions. Returns rows that changed."""
    if not client.available():
        return []
    changed: list[PostRecord] = []
    for rec in ledger.current().values():
        if not rec.whop_submission_id or rec.whop_status in TERMINAL or rec.whop_status == MANUAL:
            continue
        try:
            status, reason = client.status(rec.whop_submission_id)
        except HttpError:
            continue
        if status and status != rec.whop_status:
            changed.append(ledger.update(rec.post_id, whop_status=status, denial_reason=reason))
    return changed


def pending_manual(ledger: Ledger) -> list[PostRecord]:
    return [r for r in ledger.current().values() if r.whop_status == MANUAL]
