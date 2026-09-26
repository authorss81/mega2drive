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

### What CLIENT_SECRET vs TOKEN actually is

These are two different halves of one OAuth handshake, and you need both:

| | What it is | Where it comes from | Changes |
|---|---|---|---|
| `CLIENT_SECRET_…` | Your app's identity: "this upload is from my Google Cloud project". A JSON file you download once. | Google Cloud console → Credentials → Create OAuth client ID → **Download JSON** | Once per channel, never changes |
| `TOKEN_…` | Your permission grant: "I, the owner of this channel, allow that app to upload". Created after you approve access in the browser. | Produced locally by `--authorize-only` | Once per channel; refreshes itself |

Neither works without the other. The client secret identifies the *app*; the
token proves *you* authorized it for that specific channel. That is why there
is one pair per YouTube channel — a token is bound to the account that
approved it, so channel 2 needs its own approval and therefore its own pair.

Concretely, for one channel:

1. Google Cloud → your project → **Download JSON** → this file is
   `client_secret_channel1.json` → paste its whole contents into the
   `CLIENT_SECRET_CHANNEL1` secret.
2. Put that file in your local `credentials/` folder alongside `config.yaml`.
3. Run `python mega_to_youtube.py --authorize-only` → approve in the browser →
   it writes `token_channel1.json` → paste its contents into `TOKEN_CHANNEL1`.

Do that per channel, and the same for Drive (`client_secret_drive_source.json`
→ `CLIENT_SECRET_DRIVE_SOURCE`, `token_drive_source.json` →
`TOKEN_DRIVE_SOURCE`).

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

## Auto-dispatch — continuing until the backlog is clear

`dispatcher.yml` watches both pipelines and re-triggers a pipeline when it
finishes with work still outstanding, so a 500-file backlog drains across
several runs instead of only whatever fits in one 6-hour window.

It re-triggers the **same** pipeline, never the other one. Videos in MEGA are
Pipeline A's job; videos in the Drive landing folder are Pipeline B's. Chaining
one into the other would put the same file on YouTube twice.

**It will not loop forever.** A re-dispatch happens *only* when the run that
just finished actually completed new files. If a run completes nothing while
work remains, the cause is external — every YouTube channel hit its daily
quota, a MEGA account is dead or 2FA-locked, no capacity left — and re-running
would achieve nothing while burning Actions minutes indefinitely. So the chain
stops and the daily cron picks it up tomorrow.

Progress is not guessed from git history. Each run writes
`logs/last_run.json` stating its own `run_id` and how many files it finished;
the dispatcher trusts it only if the `run_id` matches the run that just
finished. A missing, stale, or corrupt report is treated as "no progress".
That matters because a run that completes nothing leaves the manifest
unchanged, skips the commit, and would leave the previous commit looking like
a comparison point — reading as progress when there was none.

Each pipeline also has a `concurrency` group, so a dispatched run queues
behind the one still finishing instead of racing it on the same manifest.

**One-time setup for continuous chaining:** add a `DISPATCH_TOKEN` repo secret
holding a fine-grained PAT with `Actions: read and write`. Without it the
dispatcher falls back to the built-in `GITHUB_TOKEN`, and runs started with
that token do not raise further `workflow_run` events — so it evaluates once
per day via cron and then stops. Safe, just less automatic.

You can also run it by hand: **Actions → dispatcher → Run workflow**, with an
optional `force_pipeline`.

---

## Adding more accounts later

Yes — accounts are added by editing config, not code. Nothing to redeploy.

**More MEGA destination accounts (Pipeline B):** if your addresses follow a
pattern, use the compact form — one block instead of 30 near-identical
entries:

```yaml
mega_destination_accounts:
  - email_pattern: "yourbase+{n}@gmail.com"
    count: 30                  # or: numbers: [1, 2, 3, 4, 5, 45]
    start_at: 1                # only with count
    password: "your-password"  # one entry, applied to all 30
    alias_prefix: "storage_"   # -> storage_1 ... storage_30
    cap_gb: 20                 # copied onto every generated account
    starting_used_gb: 0
    remote_dir: "/"
    enabled: true
```

Then update the `CONFIG_DRIVE_SOURCE_YAML` secret with the new file contents.
The capacity packer picks them up on the next run and keeps filling accounts
in order, so it never needs to know which account a given file belongs to.

**More MEGA source accounts (Pipeline A):** identical block under
`mega_accounts` in `config.yaml`, then update the `CONFIG_YAML` secret.

Both forms are supported and can be mixed in one list — keep the explicit
`- email: ...` entries for oddballs that don't fit the pattern.

**One shared password:** put it once in the pattern block's `password` (or in
`mega_accounts_default_password` / `mega_destination_accounts_default_password`
to cover explicit entries too). Resolution is most-specific-wins: the
account's own `password`, then the pattern block's, then the default. An
account that ends up with none is named in a warning at startup rather than
failing silently mid-run.

**More YouTube channels:** append to `youtube_channels` in both configs
(cap is 5 per config as written — raise it by adding a `CLIENT_SECRET_CHANNEL6`
/ `TOKEN_CHANNEL6` pair to the workflow's `env:` block and the matching
`echo` line). One Google Cloud OAuth client per channel, and the channel
must list your Google account as a test user on the consent screen.

**Important details when adding accounts:**

- Set `alias` on every MEGA account, or use `alias_prefix` in a pattern block.
  Logs and the committed manifest are visible on a public repo, so they show
  `storage_31`, never the real email. A pattern block sets this for free.
- `cap_gb` should match the account's real free-tier cap. The packer treats
  it as hard and stops filling at that point.
- `starting_used_gb`: if the account already holds files from outside this
  pipeline, put the current used GB here so the packer doesn't overfill it.
- `enabled: false` permanently excludes a dead or deleted account. Do this
  rather than leaving it enabled — Pipeline B's packer can't tell "full" from
  "unreachable", so a dead-but-enabled account silently strands files.
- **Test one generated account before generating thirty.** `--list-only`
  expands the config and logs into each account without transferring
  anything, which confirms MEGA accepts the address format and the password
  before you depend on a 30-account run.

---

## What a public repo does and doesn't expose

| Thing | Public? |
|---|---|
| GitHub Secrets (MEGA passwords, OAuth tokens/secrets) | **No** — encrypted, never rendered in logs or the UI |
| Actions run logs | **Yes** — every log line passes through `redact()` first |
| `manifest.csv`, `manifest_drive_source.csv`, `inventory.csv`, `logs/` | **Yes** — committed back by the workflows; every cell is redacted on write |
| Video filenames | **Yes** — accepted trade-off |

Third-party tools echo the credentials you hand them back in their errors —
`mega-login` includes the account identifier in its failure message, and Google
API tracebacks can carry tokens. Since logs and manifests are world-readable
here, both scripts run every log line and every manifest cell through
`redact()` (`mega_to_youtube.py`, `drive_to_mega_youtube.py`), which strips
anything email-shaped plus any `password=` / `refresh_token=` /
`client_secret=` assignment. Aliases, filenames, paths, and error text survive
intact, so debugging is unaffected.

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
| **`10_full_setup_walkthrough.md`** | **Start here — complete field-by-field setup for MEGA → YouTube + Drive** |
| `MASTER_GUIDE.md` | Complete reference, setup order, security, robustness |
| `01_google_cloud_youtube_oauth_setup.md` | Google Cloud project, API enablement, OAuth clients, tokens |
| `06_rclone_fast_upload_guide.md` | Fastest PC → Drive uploads, tuned for this machine |
| `07_checking_upload_progress.md` | What's uploaded vs. remaining |
| `08_git_terminal_upload_guide.md` | Creating and pushing the repo (done — reference only) |
| `09_starting_pipeline_A.md` | First manual run, and what to watch for |

---

## Keeping credentials out of the project folder

`.gitignore` stops credentials reaching GitHub. It does **not** stop a tool
running inside this folder from reading them — an AI assistant, a backup
utility, or a stray script can open any file here, gitignored or not. If you
want the credentials to be genuinely out of scope rather than merely
uncommitted, put them somewhere else entirely.

```powershell
# anywhere outside the repo, e.g. C:\Users\USER\.secrets\mega2drive\
$env:MEGA2DRIVE_CONFIG      = "C:\Users\USER\.secrets\mega2drive\config.yaml"
$env:MEGA2DRIVE_CREDENTIALS = "C:\Users\USER\.secrets\mega2drive\credentials"
```

Both scripts honour these. With them set, nothing holding a credential needs to
exist inside the project directory — the paths declared in the config
(`client_secret_file`, `token_file`) are resolved against
`MEGA2DRIVE_CREDENTIALS` instead of the repo.

To make it permanent for your shell:

```powershell
notepad $PROFILE
# add the two lines above
```

Unset in GitHub Actions, where the workflow writes `config.yaml` and
`credentials/` into the runner's own checkout — that checkout is destroyed when
the job ends.

### Safely sharing a config for help

Print it with every secret masked. Keys, nesting, paths, and non-secret values
like `folder_id` stay readable; passwords, tokens, and account addresses do not:

```powershell
python guard_secrets.py --show config.yaml
```

Paste that output instead of the file. This is the safe way to ask for help
with a config.

---

## Credential guard

Real credentials must never enter this repo: a GitHub Secret is encrypted and
un-leakable, but a committed secret is served from a CDN cache within minutes
and stays in git history permanently. Two layers enforce that:

**Local pre-commit hook** — install once per clone:

```powershell
.\scripts\install_hooks.ps1
```

**CI check** — `secret-guard.yml` runs on every push and pull request and fails
the build. This is the authoritative one, since a local hook can be bypassed
with `--no-verify` or skipped entirely by pushing from another machine.

`guard_secrets.py` blocks filled-in configs, `credentials/`, key files, real
email addresses, non-placeholder `password:` values, and credential-shaped
tokens (GitHub, Google OAuth, Google API keys). It is tuned to allow the
`*.yaml.template` files, which exist precisely to hold example structure, and
to allow `example.com`, `actions@users.noreply.github.com`, and similar.

**If it ever fires on something real:** treat that value as compromised and
rotate it. Deleting the file or rewriting history is not sufficient —
unreachable commits stay fetchable by SHA, and anything already scraped is
gone.

## Repo layout

```
.github/workflows/
  mega-to-youtube-workflow.yml          Pipeline A schedule
  drive-to-mega-youtube-workflow.yml     Pipeline B schedule
  dispatcher.yml                         Re-triggers a pipeline while its backlog lasts
config.yaml.template                    Pipeline A config template
config_drive_source.yaml.template       Pipeline B config template
mega_to_youtube.py                      Pipeline A script (MEGA → YouTube/Drive)
drive_to_mega_youtube.py                Pipeline B script (Drive → MEGA + YouTube)
run_report.py                           Per-run progress report the dispatcher reads
verify_mega_pipeline.py                 Pipeline A completeness check
verify_drive_pipeline.py                Pipeline B completeness check
match_local_files.py                    Cross-check local files vs. manifests
requirements.txt                        Python dependencies
```

## Local requirements

Python 3.11+, `pip install -r requirements.txt`, and
[mega-cmd](https://mega.nz/en/cmd) (Linux: the workflow installs the `.deb`
for you; local testing needs it on `PATH`).
