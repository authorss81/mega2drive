# MASTER GUIDE — complete reference

One private-or-public GitHub repo, two independent pipelines, running
headless (PC off) via GitHub Actions.

---

## The big picture

```
Your PC (local videos)
     │  rclone (fast, parallel) — see 06_rclone_fast_upload_guide.md
     ▼
Google Drive landing folder
     │  Pipeline B, automated
     ├──────────────► MEGA (many capacity-capped accounts, parallel upload)
     └──────────────► YouTube (backup copy, private)

Videos ALREADY in your original MEGA accounts:
     │  Pipeline A, automated
     ├──────────────► YouTube (backup copy, private)
     └──────────────► Google Drive (optional second backup)
```

**Rule:** a given video only ever goes through ONE pipeline — already in
Mega → Pipeline A; still on your PC → Drive landing folder → Pipeline B.
Never both, or it'll upload to YouTube twice.

---

## Setup order

1. **rclone** (`06_rclone_fast_upload_guide.md`) — get local videos moving
   to the Drive landing folder. Check progress with
   `07_checking_upload_progress.md`.
2. **Google Cloud + YouTube/Drive OAuth** (`01_google_cloud_youtube_oauth_setup.md`)
   — once per YouTube channel (up to 5), once more for Drive if using Pipeline A's
   optional backup or Pipeline B's landing folder.
3. **Create the GitHub repo, push files** (`08_git_terminal_upload_guide.md`)
   — public repo is fine; secrets stay encrypted either way (see Security
   section below for what public visibility actually exposes and how it's
   mitigated).
4. **Set up Pipeline B** — fill `config_drive_source.yaml.template`
   locally, run the one-time local OAuth authorization, add its GitHub
   Secrets (`CONFIG_DRIVE_SOURCE_YAML`, `CLIENT_SECRET_DRIVE_SOURCE`,
   `TOKEN_DRIVE_SOURCE`, plus the 5 YouTube channel secret pairs), add
   `drive-to-mega-youtube-workflow.yml` to `.github/workflows/`.
5. **Set up Pipeline A** — same pattern with `config.yaml.template`
   (`CONFIG_YAML` secret; reuse the same YouTube secrets from step 4).
   Start it: `09_starting_pipeline_A.md`.
6. **Run each manually once** (Actions tab → Run workflow) before trusting
   the schedule.
7. **Optional:** add a `DISPATCH_TOKEN` secret (fine-grained PAT, `Actions:
   read and write`) to let the dispatcher chain runs automatically until each
   backlog is clear.

---

## Security — what's actually exposed on a public repo, and what's not

- **GitHub Secrets stay encrypted and hidden regardless of visibility** —
  Mega passwords, OAuth client secrets/tokens are never at risk from going
  public.
- **What IS visible on public repos: Actions run logs, and any committed
  file (`manifest.csv`, `manifest_drive_source.csv`, `inventory.csv`,
  `logs/`).** These show account **aliases** (`mega_account_1`, `storage_1`,
  etc.) instead of real email addresses — set via the `alias` field in each
  config template. Real emails only ever exist in memory, passed straight to
  the login call, never printed or committed.
- **Every log line and every manifest cell is passed through `redact()`**
  before it is written. This is not belt-and-braces: `mega-login` includes
  the account identifier in its own failure output, and Google API tracebacks
  can carry tokens — so third-party error text is exactly where a real
  credential would otherwise escape. `redact()` strips anything email-shaped
  plus any `password=` / `refresh_token=` / `client_secret=` assignment, while
  leaving aliases, filenames, paths, and error text intact so debugging still
  works.
- A MEGA account with no `alias` set gets a positional placeholder and a
  loud warning, never its email address.
- Video filenames ARE visible in manifests/logs — confirmed acceptable.
- `.gitignore` blocks `config.yaml`, `config_drive_source.yaml`,
  `credentials/`, and any stray `*.json` from ever being committed
  accidentally, as a backup to the Secrets-only design.

---

## Auto-dispatch — draining a backlog without babysitting

`dispatcher.yml` fires on `workflow_run` when either pipeline completes. If
that pipeline finished new files and still has work outstanding, it is
re-triggered to continue, so a large backlog drains across several runs
instead of only what fits in one 6-hour job window.

It re-triggers the **same** pipeline, never the other one — videos in MEGA
belong to Pipeline A, videos in the Drive landing folder belong to Pipeline
B, and chaining them would upload the same file to YouTube twice.

**Loop safety.** A re-dispatch requires actual progress. A run that completes
nothing while work remains is blocked by something external (YouTube daily
quota, dead or 2FA-locked MEGA account, no capacity left); re-running it would
achieve nothing forever, so the chain stops and the daily cron retries
tomorrow. Progress is never inferred from git history — when a run completes
nothing the manifest is unchanged and the commit is skipped, which would
leave a stale `HEAD~1` that reads as progress. Instead each run writes
`logs/last_run.json` with its own `run_id`, and the dispatcher trusts it only
when the `run_id` matches the run that just finished. Missing, stale, or
corrupt means "no progress".

Each pipeline carries a `concurrency` group so a dispatched run queues behind
the one still finishing rather than racing it on the same manifest.

**For continuous chaining**, add a `DISPATCH_TOKEN` repo secret: a
fine-grained PAT with `Actions: read and write`. Without it the dispatcher
falls back to the built-in `GITHUB_TOKEN`, and runs started with that token do
not emit further `workflow_run` events — so it evaluates once per day via cron
and then stops. Safe, just not continuous.

---

## Robustness — what happens when an account is unavailable

- **Account doesn't exist / wrong password / suspended**: login fails
  cleanly, gets logged clearly (with alias, not raw email), and that
  account's files are skipped for this run — automatically retried fresh
  on the next scheduled run in case it's back.
- **Within a single run**, once an account's login has failed once, it's
  not retried again for every remaining file belonging to it — one failed
  attempt is enough to mark it unavailable for the rest of that run.
- **Permanently gone account**: set `enabled: false` on that account in
  the config to exclude it entirely — for Pipeline B this is important,
  since without it the capacity packer would keep routing files to a dead
  account forever (it doesn't know the difference between "full" and
  "unreachable" otherwise), silently stranding those files.
- **mega-cmd itself missing/not installed**: caught explicitly, logs a
  clear fatal message instead of crashing with a raw Python traceback.

---

## Verification — nothing lost

```bash
python3 verify_mega_pipeline.py     # Pipeline A
python3 verify_drive_pipeline.py    # Pipeline B
```

Both re-scan the real source independently (not just trusting the
manifest) and report DONE / PARTIAL / FAILED / NEVER ATTEMPTED per file,
plus (Pipeline B) a capacity check for anything that doesn't fit any
enabled account. Both already run automatically, non-blocking, at the end
of every scheduled workflow.

---

## YouTube compliance — fully automatic

Every upload, both pipelines: `privacy_status: "private"`,
`made_for_kids: false` (COPPA self-declaration, required by YouTube's API
on every single upload). No manual draft step, ever.

---

## Files, in order

| # | File | Purpose |
|---|---|---|
| — | `06_rclone_fast_upload_guide.md` | PC → Drive, fastest setup for your hardware |
| — | `07_checking_upload_progress.md` | What's uploaded vs. remaining |
| — | `01_google_cloud_youtube_oauth_setup.md` | YouTube + Drive API access |
| — | `08_git_terminal_upload_guide.md` | Create + push the GitHub repo |
| — | `config_drive_source.yaml.template` | Pipeline B config |
| — | `drive-to-mega-youtube-workflow.yml` | Pipeline B schedule |
| — | `drive_to_mega_youtube.py` | Pipeline B script |
| — | `09_starting_pipeline_A.md` | How to start Pipeline A |
| — | `config.yaml.template` | Pipeline A config |
| — | `mega-to-youtube-workflow.yml` | Pipeline A schedule |
| — | `mega_to_youtube.py` | Pipeline A script |
| — | `verify_mega_pipeline.py` | Pipeline A completeness check |
| — | `verify_drive_pipeline.py` | Pipeline B completeness check |
| — | `match_local_files.py` | Cross-check local files vs. either manifest |
| — | `run_report.py` | Per-run progress report the dispatcher reads |
| — | `.github/workflows/dispatcher.yml` | Re-triggers a pipeline while its backlog lasts |
| — | `.gitignore` | Safety net against committing secrets |
| — | `requirements.txt` | Python dependencies |

## Timing recap (372 files, ≤2GB each, ~500GB, ~3MiB/s sustained rclone speed)

| Stage | Estimate |
|---|---|
| rclone: PC → Drive | ~45 hours active transfer (confirmed from live testing) |
| Drive → MEGA (many accounts, parallel) | Hours, across 1–2 scheduled runs |
| Drive → YouTube (1 channel, ~100 uploads/day cap) | ~4 days for all 372 files |
| **Total, realistically** | **Well under 2 weeks**, comfortably inside a 2-month window |
