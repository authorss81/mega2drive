# 10 — Full setup walkthrough: MEGA → YouTube **and** Drive

This is the complete, field-by-field setup for **Pipeline A** with the Drive
backup switched on, so every video is copied to **both** YouTube and Google
Drive automatically, headless, with your PC off.

If you also want the reverse direction (files you upload to Drive getting
copied to MEGA), that is **Pipeline B**, covered at the end.

---

## What you are building

```
Videos in your MEGA accounts
        │
        └── GitHub Actions, daily (PC off)
              ├──► YouTube   (private backup, 1-5 channels)
              └──► Drive     (second backup, independent)
```

YouTube and Drive are **independent** destinations. Each file is downloaded
from MEGA once, then sent to each. If the YouTube upload fails but Drive
succeeds, that is recorded and only the YouTube half is retried later — you
never lose a file and never upload twice by accident.

---

## Step 0 — Before you start

- [ ] Python 3.11+ on your PC: `python --version`
- [ ] `pip install -r requirements.txt` (in this repo folder)
- [ ] The repo cloned and the guard hook installed:
      `.\scripts\install_hooks.ps1`
- [ ] Your MEGA accounts exist and you can log into them
- [ ] 2FA is **off** on every MEGA account this pipeline will use (see
      "Gotchas" at the bottom — this is the most common reason a first run
      fails)

---

## Step 1 — Google Cloud project + OAuth client

Full detail in `01_google_cloud_youtube_oauth_setup.md`. Summary:

1. <https://console.cloud.google.com> → **New Project** (one project can serve
   every channel, or use one per channel — either works).
2. Enable **YouTube Data API v3**. Also enable **Google Drive API** if you
   want the Drive backup.
3. **APIs & Services → OAuth consent screen** → External → add scopes:
   - YouTube: `.../auth/youtube.upload`, `.../auth/youtube`
   - Drive: `.../auth/drive.file` (narrow — only sees files it creates)
4. **Test users**: add the Gmail address that owns the channel. Leaving the app
   in "Testing" mode is fine.
5. **Credentials → Create Credentials → OAuth client ID** → **Desktop app** →
   **Download JSON**.

Repeat per channel (max 5) and once more for the Drive account. You end up
with files named like:

```
credentials/client_secret_channel1.json
credentials/client_secret_channel2.json
credentials/client_secret_drive.json
```

---

## Step 2 — The config file

> **The single most important step.**
> Copy the template to a name **without** `.template` and fill in the copy.
> The `.template` file is tracked by git and is therefore **public**. The copy
> is gitignored and stays on your PC.

```powershell
copy config.yaml.template config.yaml
```

Confirm git is ignoring it before you type anything real:

```powershell
git status --short          # config.yaml must NOT appear
git check-ignore -v config.yaml
```

> **Want the credentials out of this folder entirely?**
> `.gitignore` only stops them reaching GitHub — a tool running inside the
> project folder can still read them. To put them genuinely out of scope, keep
> the config somewhere else and point the scripts at it:
>
> ```powershell
> $env:MEGA2DRIVE_CONFIG      = "C:\Users\USER\.secrets\mega2drive\config.yaml"
> $env:MEGA2DRIVE_CREDENTIALS = "C:\Users\USER\.secrets\mega2drive\credentials"
> ```
>
> Nothing holding a credential then needs to exist inside the repo. Add those
> two lines to `$PROFILE` to make them permanent.
>
> **To ask for help with your config, share a masked copy** rather than the
> file — passwords, tokens, and addresses come out redacted while the
> structure stays readable:
>
> ```powershell
> python guard_secrets.py --show config.yaml
> ```

### Every field in `config.yaml`

#### `allowed_extensions`

```yaml
allowed_extensions:
  - ".mkv"
```

Only these are ever picked up from your MEGA accounts. Anything else in the
account is ignored, so a stray `.txt` or `.zip` won't be uploaded. Add more
extensions as needed.

#### `mega_accounts` — where the videos come from

Compact form, for addresses that follow a pattern:

```yaml
mega_accounts:
  - email_pattern: "yourbase+{n}@gmail.com"
    numbers: [1, 2, 3, 4, 5, 45]     # or: count: 12   /   count: 12, start_at: 1
    password: "your-real-password"  # applied to every account this block makes
    alias_prefix: "mega_account_"   # -> mega_account_1, mega_account_2, ...
    enabled: true
```

Or one entry per account:

```yaml
mega_accounts:
  - email: "yourbase+1@gmail.com"
    password: "your-real-password"
    alias: "mega_account_1"
    enabled: true
```

| Field | Meaning |
|---|---|
| `email_pattern` | Contains `{n}`, replaced with the account number |
| `numbers` / `count`+`start_at` | Which accounts to generate. `numbers` for a specific list, `count` for a contiguous run |
| `password` | One entry covers every account the block generates |
| `alias` / `alias_prefix` | The name that appears in logs and `manifest.csv` instead of your real address. Generated automatically if you use `alias_prefix` |
| `enabled` | `false` permanently skips a dead or deleted account |

Any other key in a pattern block (`cap_gb`, `remote_dir`, …) is copied onto
every account it generates, so you type it once.

**One shared password everywhere** instead of per block:

```yaml
mega_accounts_default_password: "your-real-password"
```

Resolution is most-specific-wins: the account's own `password`, then the
pattern block's, then this default. An account that ends up with none is named
in a warning at startup rather than failing silently mid-run.

#### `youtube_channels` — where the videos go

```yaml
youtube_channels:
  - name: "channel1"
    client_secret_file: "credentials/client_secret_channel1.json"
    token_file: "credentials/token_channel1.json"
  - name: "channel2"
    client_secret_file: "credentials/client_secret_channel2.json"
    token_file: "credentials/token_channel2.json"
```

One entry per channel, **max 5**. `client_secret_file` is the JSON you
downloaded in Step 1. `token_file` is created for you in Step 3 — just pick a
unique filename per channel.

The `name` is only a label for your own logs. With more than one channel,
uploads spread across them round-robin (see `distribute_mode`).

#### `google_drive` — the second destination (this is the part you want on)

```yaml
google_drive:
  enabled: true                 # false = YouTube only
  client_secret_file: "credentials/client_secret_drive.json"
  token_file: "credentials/token_drive.json"
  folder_id: ""                 # optional
```

| Field | Meaning |
|---|---|
| `enabled` | **`true` is what makes it copy to Drive as well as YouTube.** The default in the template is `false` |
| `folder_id` | Optional. The long string in a Drive folder's URL after `/folders/`. Blank uploads to Drive's root. Set it to keep backups tidy |
| `token_file` | Created for you in Step 3 |

#### `upload`

```yaml
upload:
  privacy_status: "private"     # private | unlisted | public
  made_for_kids: false          # REQUIRED by the API on every upload
  category_id: "22"             # 22 = "People & Blogs"
  distribute_mode: "round_robin"
```

- `privacy_status: "private"` — uploads land as private. Change to `public`
  only when you actually want them viewable.
- `made_for_kids: false` — a COPPA self-declaration YouTube requires on
  *every* upload via the API. Set automatically; no manual draft step.
- `distribute_mode` — `"round_robin"` spreads uploads evenly across all
  channels. `"mega_account_mapped"` pins specific MEGA accounts to specific
  channels using the commented-out `mega_to_channel_mapping` block.

#### `behavior`

```yaml
behavior:
  delete_local_after_upload: true
  max_local_staging_gb: 30
  manifest_file: "manifest.csv"
  sort_by_mega_date: false
```

| Field | Meaning |
|---|---|
| `delete_local_after_upload` | Deletes the temporary download after both uploads, so disk use stays flat regardless of library size. Safe: MEGA is still the source of truth, and anything that failed is re-downloaded next run |
| `max_local_staging_gb` | Safety cap on temporary disk |
| `manifest_file` | The CSV tracking table. Committed to the repo, visible in Actions, and openable in Excel |
| `sort_by_mega_date` | `true` uploads oldest MEGA files first. Best-effort — if your mega-cmd build doesn't report parseable dates it warns and falls back to alphabetical. Check with `--list-only` |

---

## Step 3 — One-time local authorization

With `config.yaml` and the `client_secret_*.json` files in place:

```powershell
python mega_to_youtube.py --authorize-only
```

A browser opens for each channel and, if `google_drive.enabled` is `true`, for
Drive. Approve with the Google account that **owns** that channel. This writes
the `token_*.json` files next to the client secrets. Genuinely one-time per
account — refresh tokens don't expire in normal use.

Then confirm the accounts and files are visible, without transferring
anything:

```powershell
python mega_to_youtube.py --list-only
```

This logs into each MEGA account and prints the account / size / date / path
table. **Every line should show an alias like `mega_account_1`, never a real
email.** If you see a real address, the config isn't set up as described above.

---

## Step 4 — Add the GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**, once
each. Paste file *contents*, not filenames.

| Secret name | What to paste |
|---|---|
| `CONFIG_YAML` | The entire contents of your `config.yaml` |
| `CLIENT_SECRET_CHANNEL1` | Contents of `client_secret_channel1.json` |
| `TOKEN_CHANNEL1` | Contents of `token_channel1.json` |
| `CLIENT_SECRET_CHANNEL2` … `CHANNEL5` | Same, per channel |
| `TOKEN_CHANNEL2` … `TOKEN_CHANNEL5` | Same, per channel |
| `CLIENT_SECRET_DRIVE` | Contents of `client_secret_drive.json` |
| `TOKEN_DRIVE` | Contents of `token_drive.json` |

Notes:

- **Open `config.yaml` in a text editor, select all, copy.** Multi-line secrets
  are fine — the workflow reconstructs the file with `echo "$CONFIG_YAML" >`.
- Secrets are encrypted and never displayed again. If you lose one, redo
  Step 3 and re-paste.
- Unused secrets can be skipped. The workflow writes empty files for them and
  the script skips channels it can't authenticate.
- The five YouTube pairs are **shared** with Pipeline B — no need to set them
  up twice.

Verify: `gh secret list` shows names only, never values.

---

## Step 5 — First run

1. **Actions** tab → **mega2yt** → **Run workflow** → **Run workflow**
2. Click into the run and watch:

| Step | Expect |
|---|---|
| Reconstruct config and credentials | Completes with no error. A failure here almost always means a secret name doesn't match the workflow exactly |
| Snapshot manifest | New — records the "before" state |
| Run the pipeline | `Listing videos in MEGA account: mega_account_1` (alias, not email), then downloads and uploads |
| Record what this run accomplished | Prints how many files this run finished |
| Verify nothing was lost | DONE / PARTIAL / FAILED / NEVER ATTEMPTED counts. Informational, won't fail the run |
| Commit updated manifest/logs | `manifest.csv` appears in the repo |

3. Confirm **both** destinations in the repo's `manifest.csv`:

```csv
filename,mega_account,status,youtube_video_id,drive_status,drive_file_id
Some Movie.mkv,mega_account_1,success,abc123XYZ,success,1AbCdEf...
```

`status` = YouTube, `drive_status` = Drive. Both `success` means it worked
end to end. If `drive_status` is empty, Drive didn't run — check
`google_drive.enabled` is `true` in the `CONFIG_YAML` secret, not just your
local file.

4. Spot-check: open the YouTube link and the Drive folder. Both should have the
   file, private on YouTube.

Once that works, the daily schedule takes over. Nothing further to do.

---

## Gotchas

**2FA breaks this pipeline.** `mega-login` is called with no TFA argument, and
every GitHub runner starts with a clean session, so there's no cached login and
no way to type a code. A 2FA-protected account fails every single run. Turn 2FA
off on the accounts this pipeline uses.

**Plus-addressing may not work.** If your accounts are `yourbase+1@gmail.com`,
Gmail delivers all of them to `yourbase@gmail.com`. MEGA may normalise the `+1`
away, reject it, or treat the addresses as duplicates. **Test one account
before generating thirty:** set `numbers: [1]`, run `--test-single`, and
confirm the file really lands in that MEGA account.

**YouTube's daily upload quota** is per channel. When a channel hits it, that
channel is skipped and the rest of the run continues; remaining files are
picked up on the next run. The dispatcher re-runs automatically while progress
is being made, but stops if a run completes nothing — check the log for the
real cause (quota, dead account, 2FA) rather than re-running blindly.

**`delete_local_after_upload: true` is safe** even when Drive fails: MEGA still
has the original, and the manifest records the Drive half as pending.

**Sanity check any time:**

```powershell
python verify_mega_pipeline.py
```

Re-scans MEGA independently of the manifest and reports per-file status.

---

## Adding Pipeline B (Drive → MEGA + YouTube) afterwards

Independent of the above — different videos, different config.

1. `copy config_drive_source.yaml.template config_drive_source.yaml`
2. Fill in `drive_source.folder_id` (the Drive folder new videos land in) and
   `mega_destination_accounts` (your storage accounts, same compact pattern
   form).
3. `python drive_to_mega_youtube.py --authorize-only`
4. Add secrets: `CONFIG_DRIVE_SOURCE_YAML` (the whole config — all MEGA
   passwords live in here), `CLIENT_SECRET_DRIVE_SOURCE`, `TOKEN_DRIVE_SOURCE`.
   The YouTube secrets are the same ones from Step 4.
5. Actions → **drive2mega-yt** → **Run workflow**.

**Keep the two apart.** A video already in MEGA belongs to Pipeline A. A video
still on your PC goes to the Drive landing folder and belongs to Pipeline B.
Running both on the same file uploads it to YouTube twice.
