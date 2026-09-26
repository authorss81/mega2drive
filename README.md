# mega2drive

Two independent, self-hosted video backup pipelines that run **headless on
GitHub Actions** — your PC can be off. Videos move between MEGA, Google
Drive, and YouTube automatically, with a CSV manifest tracking every file.

```
Videos already sitting in your MEGA accounts
        └── Pipeline A ──► YouTube (private backup)
                       └► Google Drive (optional 2nd backup)

Videos still on your PC
        └── rclone ──► Google Drive landing folder
                          └── Pipeline B ──► MEGA (many accounts, parallel)
                                        └──► YouTube (private backup)
```

**The one rule:** a given video goes through **one** pipeline only. Already
in MEGA → Pipeline A. Still on your PC → Drive → Pipeline B. Never both, or
it lands on YouTube twice.

---

## Quick start

Full detail lives in the guides (see [Documentation](#documentation)). The
short version:

| Step | What | Guide |
|---|---|---|
| 1 | Install rclone, upload local videos to a Drive landing folder | `06_rclone_fast_upload_guide.md` |
| 2 | Create a Google Cloud project + OAuth client per YouTube channel / Drive account | `01_google_cloud_youtube_oauth_setup.md` |
| 3 | Run the one-time local OAuth authorization to produce `token_*.json` | `01_google_cloud_youtube_oauth_setup.md` §6 |
| 4 | Fill in the config templates **locally** | `config.yaml.template`, `config_drive_source.yaml.template` |
| 5 | Add everything as GitHub Actions **Secrets** | [Secrets](#github-secrets) below |
| 6 | Run each workflow once manually, then let the schedule take over | `09_starting_pipeline_A.md` |

Both workflows are already scheduled — Pipeline A daily at 01:00 UTC,
Pipeline B daily at 03:00 UTC — and both can be triggered by hand from the
Actions tab (**Run workflow**). Edit the `cron:` line in
`.github/workflows/*.yml` to change that.

---

## GitHub Secrets

Secrets are encrypted and hidden **regardless of repo visibility** — a public
repo does not expose them. This repo is public on purpose so the guides and
scripts are readable; credentials live only in Secrets.

### Pipeline A — `mega2yt` (`.github/workflows/mega-to-youtube-workflow.yml`)

| Secret | Contents |
|---|---|
| `CONFIG_YAML` | Your filled-in `config.yaml`, entire file as one value |
| `CLIENT_SECRET_CHANNEL1` … `CHANNEL5` | Each channel's `client_secret_*.json` |
| `TOKEN_CHANNEL1` … `TOKEN_CHANNEL5` | Each channel's `token_*.json` |
| `CLIENT_SECRET_DRIVE` / `TOKEN_DRIVE` | Only if the Drive backup is enabled |

### Pipeline B — `drive2mega-yt` (`.github/workflows/drive-to-mega-youtube-workflow.yml`)

| Secret | Contents |
|---|---|
| `CONFIG_DRIVE_SOURCE_YAML` | Your filled-in `config_drive_source.yaml` — **all MEGA account credentials live in here**, not as separate secrets, because there can be 30 of them |
| `CLIENT_SECRET_DRIVE_SOURCE` / `TOKEN_DRIVE_SOURCE` | The Drive landing folder account |
| `CLIENT_SECRET_CHANNEL1..5` / `TOKEN_CHANNEL1..5` | Same YouTube secrets as Pipeline A — reuse them, no need to duplicate the OAuth setup |

The two pipelines share the same five YouTube channel secrets. One OAuth
setup per channel covers both.

Add them at **Settings → Secrets and variables → Actions → New repository
secret**. Unused secrets can be left empty — the workflow writes empty
files, and the script skips channels it can't build a client for.

---

## Adding more accounts later

Yes — accounts are added by editing config, not code. Nothing to redeploy.

**More MEGA destination accounts (Pipeline B):** append entries to
`mega_destination_accounts` in `config_drive_source.yaml`:

```yaml
  - email: "storage31@example.com"
    password: "..."
    alias: "storage_31"
    enabled: true
    cap_gb: 20
    starting_used_gb: 0
    remote_dir: "/"
```

Then update the `CONFIG_DRIVE_SOURCE_YAML` secret with the new file
contents. The capacity packer picks them up on the next run and keeps
filling accounts in order, so it never needs to know which account a given
file belongs to.

**More MEGA source accounts (Pipeline A):** same thing, append to
`mega_accounts` in `config.yaml` and update the `CONFIG_YAML` secret.

**More YouTube channels:** append to `youtube_channels` in both configs
(cap is 5 per config as written — raise it by adding a `CLIENT_SECRET_CHANNEL6`
/ `TOKEN_CHANNEL6` pair to the workflow's `env:` block and the matching
`echo` line). One Google Cloud OAuth client per channel, and the channel
must list your Google account as a test user on the consent screen.

**Important details when adding accounts:**

- Set `alias` on every MEGA account. Logs and the committed manifest are
  visible on a public repo, so they show `storage_31`, never the real email.
- `cap_gb` should match the account's real free-tier cap. The packer treats
  it as hard and stops filling at that point.
- `starting_used_gb`: if the account already holds files from outside this
  pipeline, put the current used GB here so the packer doesn't overfill it.
- `enabled: false` permanently excludes a dead or deleted account. Do this
  rather than leaving it enabled — Pipeline B's packer can't tell "full" from
  "unreachable", so a dead-but-enabled account silently strands files.

---

## What a public repo does and doesn't expose

| Thing | Public? |
|---|---|
| GitHub Secrets (MEGA passwords, OAuth tokens/secrets) | **No** — encrypted, never rendered in logs or the UI |
| Actions run logs | **Yes** — scripts print aliases, never raw emails or passwords |
| `manifest.csv`, `manifest_drive_source.csv`, `inventory.csv`, `logs/` | **Yes** — committed back by the workflows, aliases only |
| Video filenames | **Yes** — accepted trade-off |

`.gitignore` blocks `config.yaml`, `config_drive_source.yaml`,
`credentials/`, `*.json`, and `downloads/` as a second line of defence
behind the Secrets-only design.

---

## Reliability

- **Failed logins don't stall a run.** An account that can't log in is
  logged by alias, skipped for the rest of that run, and retried fresh next
  run. It isn't re-tried once per file.
- **Transient errors** (network, timeouts) retry 3× with 10s/20s/40s backoff,
  then are marked `failed` — never retried automatically, so nothing can
  loop forever. Re-attempt with `--retry-failed`.
- **Quota errors** (a channel hit its daily upload cap) skip to the next
  channel with no manifest row, so the file is picked up automatically on the
  next scheduled run. Every path terminates in success, failed, or
  "try next run".
- **Missing mega-cmd** is caught explicitly with a clear message instead of a
  traceback.

### Verify nothing was lost

```bash
python verify_mega_pipeline.py     # Pipeline A
python verify_drive_pipeline.py    # Pipeline B
```

Both re-scan the real source independently rather than trusting the
manifest, and report `DONE` / `PARTIAL` / `FAILED` / `NEVER ATTEMPTED` per
file. Pipeline B also flags anything that doesn't fit any enabled account.
Both run automatically at the end of every scheduled workflow as
non-blocking informational steps.

`match_local_files.py` cross-checks local files against either manifest, using
the recorded file sizes to tell duplicate filenames apart.

---

## YouTube upload settings

Every upload, both pipelines: `privacy_status: "private"` and
`made_for_kids: false`. The `made_for_kids` flag is a required COPPA
self-declaration on every single upload via the API — it's set
automatically, so there's no manual draft step.

---

## CLI modes

Both scripts take the same flags:

| Flag | What it does |
|---|---|
| `--authorize-only` | One-time OAuth browser flow for Drive and each YouTube channel |
| `--list-only` | Dry preview: what's in the source, in what order, and the account-packing plan. No transfers. |
| `--test-single` | One file end to end — the smoke test before a real run |
| `--run-all` | Full run |
| `--retry-failed` | Re-attempt only rows marked `failed` in the manifest |

---

## Documentation

| Guide | Covers |
|---|---|
| `MASTER_GUIDE.md` | Complete reference, setup order, security, robustness |
| `01_google_cloud_youtube_oauth_setup.md` | Google Cloud project, API enablement, OAuth clients, tokens |
| `06_rclone_fast_upload_guide.md` | Fastest PC → Drive uploads, tuned for this machine |
| `07_checking_upload_progress.md` | What's uploaded vs. remaining |
| `08_git_terminal_upload_guide.md` | Creating and pushing the repo (done — reference only) |
| `09_starting_pipeline_A.md` | First manual run, and what to watch for |

## Repo layout

```
.github/workflows/
  mega-to-youtube-workflow.yml          Pipeline A schedule
  drive-to-mega-youtube-workflow.yml     Pipeline B schedule
config.yaml.template                    Pipeline A config template
config_drive_source.yaml.template       Pipeline B config template
mega_to_youtube.py                      Pipeline A script (MEGA → YouTube/Drive)
drive_to_mega_youtube.py                Pipeline B script (Drive → MEGA + YouTube)
verify_mega_pipeline.py                 Pipeline A completeness check
verify_drive_pipeline.py                Pipeline B completeness check
match_local_files.py                    Cross-check local files vs. manifests
requirements.txt                        Python dependencies
```

## Local requirements

Python 3.11+, `pip install -r requirements.txt`, and
[mega-cmd](https://mega.nz/en/cmd) (Linux: the workflow installs the `.deb`
for you; local testing needs it on `PATH`).
