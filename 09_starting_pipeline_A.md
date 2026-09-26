# Starting Pipeline A: MEGA → Drive + YouTube

This is for videos **already sitting in your 12 original MEGA accounts**
(not the ones going through the Drive landing folder — that's Pipeline B,
separate).

## Prerequisites checklist

- [ ] Repo created and pushed (previous guide)
- [ ] `config.yaml` filled in locally on your PC — 12 Mega accounts (with
      `alias` set for each, since this is now a public repo), 5 YouTube
      channel credential paths, `google_drive:` section filled in and
      `enabled: true` if you want the Drive backup too,
      `allowed_extensions: [".mkv"]`, `made_for_kids: false`,
      `privacy_status: "private"`
- [ ] One-time local OAuth authorization already run
      (`python mega_to_youtube.py --authorize-only`), producing your 5
      `token_channel*.json` files (and `token_drive.json` if Drive backup
      is on)
- [ ] `CONFIG_YAML` secret added on GitHub with your filled-in
      `config.yaml` contents
- [ ] `CLIENT_SECRET_CHANNEL1-5` / `TOKEN_CHANNEL1-5` secrets added
- [ ] `CLIENT_SECRET_DRIVE` / `TOKEN_DRIVE` secrets added (only if using
      the Drive backup)
- [ ] `mega-to-youtube-workflow.yml` is in `.github/workflows/` in the repo

## Starting it — first run, manual (recommended before trusting the schedule)

1. Go to your repo on GitHub → **Actions** tab
2. On the left, click the workflow named **mega2yt**
3. Click **Run workflow** (top right) → **Run workflow** (confirm)
4. Click into the run that appears to watch it live — expand each step to
   see real-time logs, same detail as `run.log` would show

## What to watch for on this first run

- **"Reconstruct config.yaml and credentials from Secrets"** step should
  complete without errors — if it fails, a secret name likely doesn't
  match exactly what the workflow file expects
- **"Run the pipeline"** step — watch for `Listing videos in MEGA account:
  mega_account_1` (your alias, not raw email — confirms the privacy fix is
  working) followed by files being found and uploaded
- **"Verify nothing was lost"** step — runs automatically at the end,
  reports DONE/PARTIAL/FAILED/NEVER ATTEMPTED counts (won't fail the whole
  run even if incomplete, since a first run often is)
- **"Commit updated manifest/inventory/logs"** — confirms `manifest.csv`
  gets written back to your repo, viewable directly on GitHub afterward

## After confirming it works

Nothing more to do — the `schedule:` trigger already in
`mega-to-youtube-workflow.yml` runs it automatically from here (default:
daily), PC off, no further manual steps. Check `manifest.csv` in the repo
anytime to see progress, or re-run `verify_mega_pipeline.py` locally
whenever you want a fresh completeness check.

## If you want to change the schedule frequency

Edit `.github/workflows/mega-to-youtube-workflow.yml`'s `cron:` line (in
the repo, via GitHub's web editor or locally + `git push`), e.g.:
```yaml
schedule:
  - cron: "0 */6 * * *"   # every 6 hours instead of once daily
```
