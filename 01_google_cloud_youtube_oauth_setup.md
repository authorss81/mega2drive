# Google Cloud + YouTube/Drive API access

Do this once per YouTube channel (up to 5), and once more for any Google
Drive account used (Pipeline B's landing folder, and/or Pipeline A's
optional Drive backup). All in your own browser — nothing here goes in a
chat with anyone.

## 1. Create a Google Cloud project

1. https://console.cloud.google.com/ — log in as the account that owns
   this YouTube channel (or Drive account).
2. Project dropdown (top left) → **New Project**. Name it, e.g.
   `mega2yt-channel1`. Create it.

## 2. Enable the API

- For a YouTube channel: search "YouTube Data API v3" → **Enable**.
- For a Drive account (landing folder or backup): search "Google Drive
  API" → **Enable**.

## 3. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen** → User type **External** → Create.
2. App name (anything), your email for support/developer contact.
3. **Scopes** — Add or Remove Scopes:
   - YouTube: `.../auth/youtube.upload` and `.../auth/youtube`
   - Drive: `.../auth/drive.file` (narrower, only sees files it creates —
     recommended over full `drive` access)
4. Save through the remaining steps.
5. **Test users**: add the Gmail address that owns this channel/Drive
   account (keeps the app in "Testing" mode — fine, no need to publish it).

## 4. Create OAuth Client credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**. Name it, Create.
3. **Download JSON** — this is your `client_secret_*.json`. Treat it like
   a password.

## 5. Repeat

- Once per YouTube channel (up to 5) → 5 `client_secret_channel*.json` files.
- Once for your Drive landing folder account (Pipeline B) →
  `client_secret_drive_source.json`.
- Once more if using Pipeline A's optional Drive backup, on whichever
  account holds that → `client_secret_drive.json`.

## 6. One-time local authorization (produces the token files)

On your own PC, with the relevant script and a minimal filled-in config
pointing at these `client_secret_*.json` files:

```powershell
python mega_to_youtube.py --authorize-only
# and/or
python drive_to_mega_youtube.py --authorize-only
```

This opens your browser for the Google login/approval step (normal
browser flow, since it's running locally) — produces `token_*.json` files
alongside the client secrets. Keep these; you'll paste their contents into
GitHub Secrets next (`08_git_terminal_upload_guide.md` /
`09_starting_pipeline_A.md`). Refresh tokens don't expire under normal
use, so this is genuinely one-time per account.
