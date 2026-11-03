# clipworks

Long-form YouTube in, compliant vertical clips posted to Instagram / YouTube
Shorts / TikTok, submitted to Whop, views and payouts tracked. One command,
`clipworks cycle`, run every 30 minutes.

```
Discord brief ─▶ draft YAML ─(you approve)─▶ campaigns/active/
                                                  │
yt-dlp ─▶ publikclip ─▶ clip inventory ─▶ compliance ─▶ daily plan
                                                  │
                     posters (IG / YT / TikTok) ◀─┘ one per platform per cycle
                                │
               Whop submit ─▶ ledger ─▶ view reconcile ─▶ realised $/1k
                                │
                    Discord webhook alerts (only when you must act)
```

## What is automatic and what is not (read this first)

| Step | Status | Why |
|---|---|---|
| Finding new source videos | automatic | yt-dlp flat listing |
| Rendering clips | automatic | publikclip, resumes on failure |
| Compliance gate | automatic | blocks before posting; fail-closed |
| Instagram Reels | automatic | Graph API, needs Meta app + token |
| YouTube Shorts | automatic | Data API, refresh token from `auth_youtube.py` |
| TikTok | **one tap per post** until your app passes TikTok's audit | unaudited apps can only post privately; inbox mode uploads a draft |
| Whop **Bounty** submit | automatic | public Bounty Submissions API |
| Whop **Content Rewards** submit | **one paste per post** | Whop exposes no Content Rewards API; the official `whop` CLI does not cover it either |
| Streamer Discord campaigns | automatic **only via Follow** | Bot reads your own server; "Follow" an announcement channel to crosspost into it |
| Turning a brief into rules | **one review per campaign** | deliberate: guessed rules = rejected clips |

Not built, on purpose: Discord self-bots, browser-driving the Whop dashboard,
instagrapi-style private APIs. Each one puts the account that holds the
money at risk of termination.

## Setup

```bash
uv sync
uv run clipworks doctor      # shows exactly what is live, dry, or missing
```

Secrets come from the environment (Doppler: `doppler run -- uv run clipworks cycle`).
Every clipworks secret is prefixed `CLIPWORKS_`. clipworks shares the
`philosopher-pipeline/dev` Doppler config (10-project cap), and an
unprefixed `IG_ACCESS_TOKEN` there would one day be read by both projects:
streamer clips posted to the philosopher account, or the reverse. The prefix
makes that collision impossible instead of unlikely.
Anything missing makes that integration a **dry run**: logged to
`runs/dryrun.jsonl`, never written to the ledger.

| Integration | Env vars |
|---|---|
| publikclip | `PUBLIKCLIP_DIR`, optional `PUBLIKCLIP_VENV` |
| YouTube cookies | `YTDLP_COOKIES_FROM_BROWSER=chrome` (only if bot-checked) |
| Instagram | `CLIPWORKS_IG_USER_ID`, `CLIPWORKS_IG_ACCESS_TOKEN` (long-lived, ~60 days) |
| YouTube | `CLIPWORKS_YT_CLIENT_ID`, `CLIPWORKS_YT_CLIENT_SECRET`, `CLIPWORKS_YT_REFRESH_TOKEN`, `CLIPWORKS_YOUTUBE_API_KEY` (for views) |
| TikTok | `CLIPWORKS_TIKTOK_CLIENT_KEY`, `CLIPWORKS_TIKTOK_CLIENT_SECRET`, `CLIPWORKS_TIKTOK_REFRESH_TOKEN`, `CLIPWORKS_TIKTOK_MODE=inbox\|direct` |
| Discord alerts | `CLIPWORKS_DISCORD_WEBHOOK_URL` |
| Discord brief intake | `CLIPWORKS_DISCORD_BOT_TOKEN`, `CLIPWORKS_DISCORD_BRIEF_CHANNEL_IDS=id1,id2` (enable Message Content intent) |
| Whop bounties | `CLIPWORKS_WHOP_USER_TOKEN` (user OAuth, scope `bounty:submission:create`) |
| Knobs | `CLIPWORKS_DRY_RUN=1`, `CLIPWORKS_WINDOW_UTC=12-23`, `CLIPWORKS_VIDEOS_PER_CYCLE=1`, `CLIPWORKS_MAX_PER_DAY_<PLATFORM>` |

The YouTube refresh token: `football-shorts-autopilot/auth_youtube.py` mints it
unchanged (same scope). Sign in with the **clipping channel**, not the football
one, and store the three values as `CLIPWORKS_YT_*`, not the `YT_*` names that
script prints.

## Run it

Linux/macOS cron:
```
*/30 * * * * cd /path/to/clipworks && doppler run -- uv run clipworks cycle >> runs/cron.log 2>&1
```
Windows (Task Scheduler):
```
schtasks /create /sc minute /mo 30 /tn clipworks /tr "cmd /c cd /d C:\path\to\clipworks && doppler run -- uv run clipworks cycle >> runs\cron.log 2>&1"
```
Or no scheduler at all: `uv run clipworks loop --every 30`.

It runs on your machine, not CI: publikclip needs your GPU and models, and
the rendered MP4s live on local disk.

## Daily use

```bash
uv run clipworks status                      # today's plan, pending submits, $/1k
pbpaste | uv run clipworks brief -           # draft a campaign from a brief
uv run clipworks campaign approve campaigns/drafts/<file>.yaml
uv run clipworks whop pending                # Content Rewards URLs to paste
uv run clipworks whop done <post_id>
uv run clipworks ledger set-url <post_id> <url>    # after publishing a TikTok draft
uv run clipworks ledger set-payout <post_id> 12.40
```

## Safety properties (each has a test)

- A guessed rule never goes live: brief extraction writes `TODO`, which the loader rejects.
- No double posts: deterministic post ids; a crash mid-upload is recorded as posted-unknown and alerted, never retried.
- No bursts: one plan per campaign per day, at most one post per platform per cycle.
- Campaign cap is across platforms; an account-wide ceiling stops two campaigns stacking on one account.
- One broken campaign, API, or poster never stops the others.
- Tokens never appear in reprs, logs, or error messages.
