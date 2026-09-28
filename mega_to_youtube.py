#!/usr/bin/env python3
"""
mega_to_youtube.py

Pulls video files from one or more MEGA accounts (via mega-cmd) and uploads
them to one or more YouTube channels (via the YouTube Data API v3), with:

  - round-robin or mapped distribution across channels
  - per-channel daily-quota-aware pausing (catches quotaExceeded /
    uploadLimitExceeded, marks that channel exhausted for today, and moves
    on — no retry loop; the next scheduled cron run picks it back up once
    the quota resets)
  - bounded retries for TRANSIENT failures (network blips, timeouts) —
    default 3 attempts with exponential backoff, then the file is marked
    "failed" (not retried again automatically) so it can't loop forever;
    failed files are logged and safe to re-attempt manually or on a later
    run with --retry-failed
  - a persistent CSV manifest recording, per video: which MEGA account and
    exact remote path it came from, its file size (for matching duplicate
    filenames against your local PC), which YouTube channel and video ID
    it was uploaded to, upload timestamp, and status — this IS your
    trackable table, kept in sync automatically as the script runs
  - optional oldest-first ordering based on MEGA's reported modification
    date (falls back to alphabetical if date parsing doesn't match your
    mega-cmd version's output format — verify with --list-only first)
  - streaming disk usage: downloads one file, uploads it, deletes the local
    copy, then moves to the next — never needs all 240GB on disk at once

Run modes:
  --authorize-only   walk through the one-time OAuth browser flow per channel
  --list-only        list what WOULD be processed and in what order, no
                      downloads/uploads — use this to sanity check ordering
                      and see the account/path/size table before a real run
  --test-single       process exactly one file end-to-end, for a smoke test
  --run-all           full run across all configured Mega accounts
  --retry-failed      re-attempt only files marked "failed" in the manifest

All secrets (Mega passwords, OAuth client secrets/tokens) are read from
config.yaml and the credentials/ folder on THIS machine. Nothing is ever
sent anywhere except MEGA's and Google's own APIs.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

BASE_DIR = Path(__file__).resolve().parent

# Where the config and the OAuth credentials live.
#
# Both can be moved OUTSIDE this repo with environment variables:
#
#   MEGA2DRIVE_CONFIG       absolute path to config.yaml
#   MEGA2DRIVE_CREDENTIALS  absolute path to the folder holding the
#                           client_secret_*.json / token_*.json files
#
# Why that matters: this repo is public, and a tool working inside the project
# folder (an AI assistant, a script, a backup tool) can read any file inside
# it. Keeping the real credentials outside the folder means they are never in
# scope at all, rather than merely gitignored. The filenames inside the config
# are then resolved against MEGA2DRIVE_CREDENTIALS when it is set, and against
# the repo directory otherwise.
#
# In GitHub Actions neither variable is set: the workflow writes
# config.yaml and credentials/ into the runner's own checkout, which is
# destroyed when the job ends.
def _resolve(configured_path):
    """Resolve a config-declared path against the credentials dir override."""
    p = Path(configured_path)
    if p.is_absolute() or not CREDENTIALS_DIR:
        return BASE_DIR / p
    return CREDENTIALS_DIR / p


CREDENTIALS_DIR = os.environ.get("MEGA2DRIVE_CREDENTIALS") or ""
CONFIG_PATH = Path(os.environ.get("MEGA2DRIVE_CONFIG") or (BASE_DIR / "config.yaml"))
LOG_PATH = BASE_DIR / "logs" / "run.log"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".m4v"}  # fallback default; config.yaml's
                                                                        # allowed_extensions (if set) overrides this

QUOTA_ERROR_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"}

# Google error reasons that are PERMANENT for the whole run, not transient.
# Retrying them per file is pure waste: an API that was never enabled will
# still not be enabled 20 seconds later, and a 403 permission error will not
# become permitted. Each wasted retry costs 10s + 20s of sleep per file, which
# across a 250-file library is hours of a 6-hour job doing nothing.
#
# accessNotConfigured is the common one: "Google Drive API has not been used
# in project X before or it is disabled" - the API simply isn't switched on in
# Google Cloud. Fixing it needs a human in the console, so the run should stop
# trying immediately and say so, not grind through the backlog failing.
PERMANENT_ERROR_REASONS = {"accessNotConfigured", "forbidden", "insufficientPermissions",
                           "PERMISSION_DENIED", "SERVICE_DISABLED"}
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 10  # 10s, 20s, 40s

MANIFEST_FIELDS = [
    "timestamp", "mega_account", "mega_remote_path", "filename",
    "file_size_bytes", "mega_modified", "youtube_channel",
    "youtube_video_id", "status", "attempts", "error",
    "drive_status", "drive_file_id", "drive_error",
]

# --- Redaction ---------------------------------------------------------
# This repo is PUBLIC, and Actions run logs plus every committed manifest
# file are world-readable. Third-party tools (mega-cmd, Google API client)
# echo the credentials they're given back to us in their error output, so
# anything we print can leak a real email address or password by accident.
# Everything that reaches a log line or a CSV cell goes through redact()
# first, so a leak has to be a deliberate change to this function to happen.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PASSWORD_RE = re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[=:]\s*\S+")
_TOKEN_RE = re.compile(r"(?i)\b(refresh_token|access_token|id_token|client_secret|api_key)\b\s*[=:]\s*\S+")


def redact(value):
    """Strip anything email-shaped, plus credential-looking assignments."""
    if value is None:
        return ""
    text = str(value)
    text = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _TOKEN_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return _EMAIL_RE.sub("[redacted-email]", text)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact(msg)}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def expand_accounts(entries, default_password=None, default_alias_prefix="mega_account_"):
    """Flatten the mega_accounts config into a plain list of concrete accounts.

    Two forms, mixable in the same list:

    1. Explicit - one mapping per account, each with its own email/password:

        mega_accounts:
          - email: "one@example.com"
            password: "..."

    2. Pattern - one mapping that describes N generated accounts:

        mega_accounts:
          - email_pattern: "abc+{n}@gmail.com"
            count: 30                # or: numbers: [1, 2, 3, 4, 5, 45]
            start_at: 1              # only with count
            password: "shared"       # applies to every generated account
            alias_prefix: "mega_"    # -> mega_1, mega_2, ...
            cap_gb: 20               # any other key is copied to each account
            enabled: true

    Every other key in a pattern block is copied verbatim onto each generated
    account, so per-account fields (cap_gb, remote_dir, enabled) only get typed
    once. {n} in email_pattern/alias_pattern is the account's number.

    Password resolution, most specific first: the account's own `password`,
    then the pattern block's `password`, then `default_password`. An account
    that ends up with no password is reported loudly rather than silently
    failing to log in.
    """
    out = []
    for entry in entries or []:
        if "email_pattern" not in entry:
            acct = dict(entry)
            if not acct.get("password") and default_password:
                acct["password"] = default_password
            out.append(acct)
            continue

        pattern = entry["email_pattern"]
        alias_pattern = entry.get("alias_pattern")
        alias_prefix = entry.get("alias_prefix")

        if entry.get("numbers") is not None:
            numbers = list(entry["numbers"])
        elif entry.get("count"):
            start = int(entry.get("start_at", 1))
            numbers = list(range(start, start + int(entry["count"])))
        else:
            log(f"WARNING: account block has email_pattern but no count or numbers - skipped.")
            continue

        for n in numbers:
            acct = {k: v for k, v in entry.items()
                    if k not in ("email_pattern", "count", "start_at", "numbers",
                                 "alias_pattern", "alias_prefix")}
            acct["email"] = pattern.replace("{n}", str(n))
            if alias_pattern:
                acct["alias"] = alias_pattern.replace("{n}", str(n))
            elif alias_prefix:
                acct["alias"] = f"{alias_prefix}{n}"
            else:
                acct["alias"] = f"{default_alias_prefix}{n}"
            acct.setdefault("password", default_password or "")
            out.append(acct)

    missing = [a.get("alias", a.get("email", "?")) for a in out if not a.get("password")]
    if missing:
        log(f"WARNING: {len(missing)} account(s) have no password and will fail to log in: "
            f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")
    return out


def load_config():
    if not CONFIG_PATH.exists():
        log(f"ERROR: config not found at {CONFIG_PATH}. Copy config.yaml.template to config.yaml "
            f"and fill it in, or set MEGA2DRIVE_CONFIG to its location.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    config["mega_accounts"] = expand_accounts(
        config.get("mega_accounts"),
        default_password=config.get("mega_accounts_default_password"),
        default_alias_prefix="mega_account_",
    )
    return config


# ---------------------------------------------------------------------------
# Manifest = your trackable table (CSV, opens directly in Excel/Sheets)
# One row per (mega_account, mega_remote_path) — that composite key is what
# stays unique even when filenames repeat across different accounts/folders.
# ---------------------------------------------------------------------------

def manifest_path(config):
    return BASE_DIR / config["behavior"].get("manifest_file", "manifest.csv")


def load_manifest_rows(config):
    p = manifest_path(config)
    rows = {}
    if p.exists():
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                key = (row["mega_account"], row["mega_remote_path"])
                rows[key] = row
    return rows


def save_manifest_rows(config, rows):
    p = manifest_path(config)
    with open(p, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: redact(v) for k, v in row.items() if k in MANIFEST_FIELDS})


def upsert_manifest_row(rows, **fields):
    key = (fields["mega_account"], fields["mega_remote_path"])
    existing = rows.get(key, {})
    existing.update(fields)
    for f in MANIFEST_FIELDS:
        existing.setdefault(f, "")
    rows[key] = existing


# ---------------------------------------------------------------------------
# MEGA side (via mega-cmd's `mega-*` CLI tools)
# ---------------------------------------------------------------------------

def mega_login(email, password, alias=None):
    try:
        subprocess.run(["mega-logout"], capture_output=True)
        result = subprocess.run(["mega-login", email, password], capture_output=True, text=True)
    except FileNotFoundError:
        log(f"FATAL: mega-cmd is not installed or not on PATH — cannot log into {alias or '[account]'} "
            f"or any other MEGA account. Check the 'Install mega-cmd' step in the workflow/server setup.")
        return False
    if result.returncode != 0:
        log(f"MEGA login FAILED for {alias or '[account]'} — account may not exist, be suspended, "
            f"or have wrong credentials: {redact(result.stderr.strip())}")
        return False
    return True


def mega_list_videos_with_details(remote_path="/", allowed_extensions=None):
    """
    Returns a list of dicts: {remote_path, size_bytes, modified}.
    Uses `mega-find` for the file list, then `mega-ls -l` per file for
    size/date (mega-cmd's ls output format can vary by version — if
    'modified' comes back as None for everything, sorting falls back to
    alphabetical and you'll see a warning in the log; that's safe, just
    means date-ordering isn't available on this mega-cmd build).
    """
    exts = allowed_extensions or VIDEO_EXTENSIONS
    result = subprocess.run(["mega-find", remote_path, "--pattern=*"], capture_output=True, text=True)
    if result.returncode != 0:
        log(f"mega-find failed: {redact(result.stderr.strip())}")
        return []

    paths = [line.strip() for line in result.stdout.splitlines()
             if line.strip() and Path(line.strip()).suffix.lower() in exts]

    details = []
    date_pattern = re.compile(r"(\d{2}\w{3}\d{4}\s+\d{2}:\d{2}:\d{2})")  # e.g. 01Feb2024 10:23:44
    for p in paths:
        size_bytes, modified = None, None
        ls = subprocess.run(["mega-ls", "-l", p], capture_output=True, text=True)
        if ls.returncode == 0 and ls.stdout.strip():
            line = ls.stdout.strip().splitlines()[-1]
            parts = line.split()
            for token in parts:
                if token.isdigit() and len(token) > 3:
                    size_bytes = int(token)
                    break
            m = date_pattern.search(line)
            if m:
                try:
                    modified = datetime.strptime(m.group(1), "%d%b%Y %H:%M:%S")
                except ValueError:
                    modified = None
        details.append({"remote_path": p, "size_bytes": size_bytes, "modified": modified})
    return details


def mega_download(remote_path, local_dir):
    local_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["mega-get", remote_path, str(local_dir)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"mega-get failed: {redact(result.stderr.strip())}")
    local_file = local_dir / Path(remote_path).name
    if not local_file.exists():
        raise RuntimeError(f"mega-get reported success but file not found at {local_file}")
    return local_file


# ---------------------------------------------------------------------------
# YouTube side
# ---------------------------------------------------------------------------

class QuotaExceeded(Exception):
    pass


class PermanentApiError(Exception):
    """An API/permission problem that retrying cannot fix - the API is disabled
    in the project, or the token lacks the scope. Retrying wastes the whole
    job, so these abort the run's remaining work for that destination."""


def run_oauth_flow(flow, label):
    """Complete an installed-app OAuth flow, whatever the library version.

    `InstalledAppFlow.run_console()` was deprecated and then REMOVED in
    google-auth-oauthlib 1.2+ -- calling it raises AttributeError, which used
    to break `--authorize-only` outright. The supported path is
    `run_local_server()`.

    Two details matter and are easy to get wrong:

    - `redirect_uri_trailing_slash` must be False. The library defaults it to
      True and builds `http://localhost:PORT/`, but a Desktop-app client's
      registered redirect URI is `http://localhost`. The trailing-slash
      mismatch is rejected by Google with a bare
      "400. That's an error. The server cannot process the request because it
      is malformed." and no useful detail.
    - `access_type="offline"` is requested explicitly. Without it a refresh
      token is not guaranteed, and without a refresh token the generated
      token file dies at the first expiry instead of renewing itself.

    Falls back to a print-the-URL / paste-the-code flow if the local server
    cannot start (blocked localhost binding, restrictive firewall).
    """
    if hasattr(flow, "run_local_server"):
        try:
            return flow.run_local_server(
                host="localhost",
                port=0,
                open_browser=True,
                redirect_uri_trailing_slash=False,
                access_type="offline",
                prompt="consent",
                success_message=("Authorization complete. You can close this tab "
                                 "and return to PowerShell."),
            )
        except Exception as e:
            log(f"Local redirect server could not start ({e}). "
                f"Falling back to a copy/paste code.")
    else:
        log("This version of google-auth-oauthlib has no run_local_server(); "
            "using a copy/paste code instead.")

    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent",
                                         redirect_uri="urn:ietf:wg:oauth:2.0:oob")
    print("\n" + "=" * 70)
    print(f"Open this URL in your browser to authorize {label}:\n")
    print(auth_url)
    print("\nIf Google shows a 400 error, the OAuth consent screen for this")
    print("project is probably incomplete, or your Google account is not listed")
    print("as a test user on it.")
    print("=" * 70)
    code = input("Paste the authorization code here: ").strip()
    flow.fetch_token(code=code)
    return flow.credentials


def get_youtube_client(channel_cfg, authorize_only=False):
    creds = None
    token_path = _resolve(channel_cfg["token_file"])
    secret_path = _resolve(channel_cfg["client_secret_file"])

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret_path.exists():
                log(f"ERROR: missing client secret file {secret_path} for channel {channel_cfg['name']}")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
            log(f"--- Authorize channel '{channel_cfg['name']}' (browser will open) ---")
            creds = run_oauth_flow(flow, f"channel {channel_cfg['name']}")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    if authorize_only:
        return None
    return build("youtube", "v3", credentials=creds)


def get_drive_client(drive_cfg, authorize_only=False):
    """Same OAuth pattern as get_youtube_client, but for a Google Drive
    account (usually your separate 5TB storage account, not a YouTube
    channel). Only called if config.yaml has a `google_drive:` section."""
    creds = None
    token_path = _resolve(drive_cfg["token_file"])
    secret_path = _resolve(drive_cfg["client_secret_file"])

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret_path.exists():
                log(f"ERROR: missing client secret file {secret_path} for Google Drive")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), DRIVE_SCOPES)
            log("--- Authorize Google Drive (browser will open) ---")
            creds = run_oauth_flow(flow, "Google Drive")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    if authorize_only:
        return None
    return build("drive", "v3", credentials=creds)


def upload_to_drive_once(drive, filepath, folder_id=None):
    body = {"name": filepath.name}
    if folder_id:
        body["parents"] = [folder_id]
    media = MediaFileUpload(str(filepath), resumable=True, mimetype="video/*")
    request = drive.files().create(body=body, media_body=media, fields="id")

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            reason = ""
            try:
                reason = json.loads(e.content).get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:
                pass
            # Drive's quota errors use different reason strings than YouTube's
            if reason in {"quotaExceeded", "userRateLimitExceeded", "storageQuotaExceeded"}:
                raise QuotaExceeded(reason)
            # An API that was never enabled, or a scope the token lacks, will
            # fail identically on every retry for every remaining file. Surface
            # it as permanent so the run stops guessing.
            if reason in PERMANENT_ERROR_REASONS or e.resp.status == 403:
                raise PermanentApiError(
                    f"{reason or 'HTTP 403'} - the Google Drive API may not be enabled for "
                    f"this project, or the token lacks the Drive scope. Check "
                    f"APIs & Services > Library > Google Drive API in Google Cloud."
                )
            raise
    return response.get("id")


def upload_to_drive_with_retry(drive, filepath, folder_id=None):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return upload_to_drive_once(drive, filepath, folder_id), attempt
        except QuotaExceeded:
            raise
        except PermanentApiError:
            # Not worth another attempt - see PERMANENT_ERROR_REASONS.
            raise
        except Exception as e:
            last_error = e
            log(f"  Drive upload attempt {attempt}/{MAX_RETRIES} failed for {filepath.name}: {e}")
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log(f"  Retrying Drive upload in {wait}s...")
                time.sleep(wait)
    raise last_error


def upload_video_once(youtube, filepath, title, privacy_status, category_id, made_for_kids=False):
    body = {
        "snippet": {
            "title": title,
            "description": f"Uploaded automatically from MEGA backup: {filepath.name}",
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,  # required by YouTube on every upload (COPPA)
        },
    }
    media = MediaFileUpload(str(filepath), chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            reason = ""
            try:
                reason = json.loads(e.content).get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:
                pass
            if reason in QUOTA_ERROR_REASONS:
                raise QuotaExceeded(reason)
            raise  # transient/other error — handled by retry wrapper below
    return response.get("id")


def upload_video_with_retry(youtube, filepath, title, privacy_status, category_id, made_for_kids=False):
    """
    Retries transient failures up to MAX_RETRIES with exponential backoff.
    QuotaExceeded is NOT retried here — it propagates immediately so the
    caller can mark the channel exhausted and move to the next file/channel.
    After MAX_RETRIES transient failures, raises the last error — the
    caller marks the row 'failed' in the manifest and moves on (no loop).
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return upload_video_once(youtube, filepath, title, privacy_status, category_id, made_for_kids), attempt
        except QuotaExceeded:
            raise
        except Exception as e:
            last_error = e
            log(f"  Upload attempt {attempt}/{MAX_RETRIES} failed for {filepath.name}: {e}")
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log(f"  Retrying in {wait}s...")
                time.sleep(wait)
    raise last_error


def download_with_retry(remote_path, staging_dir):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return mega_download(remote_path, staging_dir), attempt
        except Exception as e:
            last_error = e
            log(f"  Download attempt {attempt}/{MAX_RETRIES} failed for {remote_path}: {e}")
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log(f"  Retrying in {wait}s...")
                time.sleep(wait)
    raise last_error


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_worklist(config):
    """Logs into every MEGA account, lists videos with details, and returns
    a flat worklist of dicts, optionally sorted oldest-first by mega_modified.

    Stores an ALIAS (not the real email) in every field that ends up logged
    or written to manifest.csv — that file gets committed to the repo, so on
    a public repo this is what keeps your actual Mega account emails out of
    anything publicly visible. The real email is only ever held in memory,
    passed straight to mega_login(), never printed or persisted."""
    worklist = []
    for idx, mega_acc in enumerate(config["mega_accounts"]):
        alias = mega_acc.get("alias") or f"mega_account_{idx+1}"
        if not mega_acc.get("enabled", True):
            log(f"Skipping {alias} — marked enabled: false in config.")
            continue
        email = mega_acc["email"]
        log(f"Listing videos in MEGA account: {alias}")
        if not mega_login(email, mega_acc["password"]):
            continue
        allowed_ext = set(e.lower() for e in config.get("allowed_extensions", list(VIDEO_EXTENSIONS)))
        details = mega_list_videos_with_details("/", allowed_extensions=allowed_ext)
        for d in details:
            worklist.append({
                "mega_account": alias,       # used everywhere for logging/manifest
                "_mega_email": email,        # internal only — never logged, never written out
                "mega_remote_path": d["remote_path"],
                "filename": Path(d["remote_path"]).name,
                "file_size_bytes": d["size_bytes"],
                "mega_modified": d["modified"],
            })
        log(f"  -> {len(details)} video(s) found")

    sort_by_date = config["behavior"].get("sort_by_mega_date", False)
    if sort_by_date:
        undated = [w for w in worklist if w["mega_modified"] is None]
        if undated:
            log(f"WARNING: {len(undated)} file(s) had no parseable MEGA date — "
                f"they'll be sorted last. Check --list-only output / your mega-cmd version.")
        worklist.sort(key=lambda w: (w["mega_modified"] is None, w["mega_modified"] or datetime.max))
    return worklist


def list_only(config):
    """Prints the inventory AND writes it to inventory.csv — same column
    layout as manifest.csv (youtube_channel/video_id/status just blank/'pending'
    for now), so you can pull this to your PC and run match_local_files.py
    against it immediately, before any downloads or uploads have happened."""
    worklist = build_worklist(config)
    print(f"\n{'MEGA ACCOUNT':30} {'SIZE':>10}  {'MODIFIED':19}  REMOTE PATH")
    print("-" * 100)

    inventory_rows = {}
    for w in worklist:
        size_mb = f"{w['file_size_bytes']/1e6:.1f}MB" if w["file_size_bytes"] else "?"
        mod = w["mega_modified"].strftime("%Y-%m-%d %H:%M:%S") if w["mega_modified"] else "unknown"
        print(f"{w['mega_account']:30} {size_mb:>10}  {mod:19}  {w['mega_remote_path']}")
        upsert_manifest_row(
            inventory_rows, timestamp=datetime.now().isoformat(),
            mega_account=w["mega_account"], mega_remote_path=w["mega_remote_path"],
            filename=w["filename"], file_size_bytes=w["file_size_bytes"] or "",
            mega_modified=w["mega_modified"].isoformat() if w["mega_modified"] else "",
            youtube_channel="", youtube_video_id="", status="pending",
            attempts=0, error="",
        )

    inv_path = BASE_DIR / "inventory.csv"
    with open(inv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in inventory_rows.values():
            writer.writerow(row)

    print(f"\nTotal: {len(worklist)} video(s). No files were downloaded or uploaded (--list-only).")
    print(f"Wrote full inventory table to {inv_path} — pull this to your PC and run "
          f"match_local_files.py against it to see which local file is in which MEGA "
          f"account (YouTube columns will just be blank/'pending' until you actually run uploads).")


def run(config, single_file_test=False, retry_failed_only=False):
    rows = load_manifest_rows(config)
    channels = config["youtube_channels"]
    clients = {}
    for ch in channels:
        yt = get_youtube_client(ch)
        if yt:
            clients[ch["name"]] = {"client": yt, "exhausted_today": False}

    if not clients:
        log("No valid YouTube clients available. Run --authorize-only first.")
        return

    drive_client = None
    drive_exhausted_today = False
    drive_blocked_reason = None
    drive_cfg = config.get("google_drive")
    if drive_cfg and drive_cfg.get("enabled", False):
        drive_client = get_drive_client(drive_cfg)
        if not drive_client:
            log("WARNING: google_drive is enabled in config but client could not be created — skipping Drive uploads this run.")

    channel_names = list(clients.keys())
    channel_cycle_idx = 0
    staging_dir = BASE_DIR / "downloads"

    worklist = build_worklist(config)

    # Lookup so we can re-login to the CORRECT account before each download —
    # build_worklist() already logged into every account once just to list
    # files, so by now the active mega-cmd session is whichever account was
    # listed LAST, not necessarily the one this file belongs to. Without
    # re-logging in per file, downloads for every account except the last
    # one listed would silently fail or pull from the wrong account.
    password_by_email = {}
    alias_to_email = {}
    for idx, acc in enumerate(config["mega_accounts"]):
        alias = acc.get("alias") or f"mega_account_{idx+1}"
        password_by_email[acc["email"]] = acc["password"]
        alias_to_email[alias] = acc["email"]

    drive_enabled = bool(drive_client)  # only meaningful if Drive is configured+authorized this run

    current_session_alias = None  # track which account mega-cmd is actually logged into right now
    known_unavailable_aliases = set()  # accounts that failed login once this run — don't retry per-file

    for item in worklist:
        alias = item["mega_account"]
        real_email = item.get("_mega_email") or alias_to_email.get(alias)
        remote_path = item["mega_remote_path"]
        key = (alias, remote_path)
        existing = rows.get(key)

        yt_done = existing and existing.get("status") == "success"
        drive_done = (not drive_enabled) or (existing and existing.get("drive_status") == "success")
        if yt_done and drive_done:
            continue  # both destinations (that are enabled) already succeeded — truly done
        if retry_failed_only and (not existing or existing.get("status") != "failed"):
            continue  # in --retry-failed mode, skip everything except prior failures
        if not retry_failed_only and existing and existing.get("status") == "failed":
            continue  # don't auto-retry failed rows on a normal run; use --retry-failed

        if alias in known_unavailable_aliases:
            continue  # already confirmed unreachable this run — don't waste time re-attempting per file;
                       # will be retried fresh on the NEXT scheduled run, in case it's back by then

        available = [n for n in channel_names if not clients[n]["exhausted_today"]]
        if not available:
            log("All channels have hit their daily upload quota. Stopping run — cron will resume once quota resets.")
            save_manifest_rows(config, rows)
            return

        channel_cycle_idx = channel_cycle_idx % len(available)
        channel_name = available[channel_cycle_idx]
        channel_cycle_idx += 1

        # Re-login only if the session has actually moved to a different
        # account since the last file — avoids a pointless re-login on every
        # single file when consecutive worklist items share the same account.
        if current_session_alias != alias:
            log(f"Switching MEGA session to {alias} ...")
            if not real_email or not mega_login(real_email, password_by_email.get(real_email, ""), alias):
                log(f"Account {alias} is unavailable (doesn't exist, wrong credentials, or suspended) — "
                    f"skipping ALL remaining files for this account for the rest of this run.")
                known_unavailable_aliases.add(alias)
                continue
            current_session_alias = alias

        log(f"Downloading {remote_path} from {alias} ...")
        try:
            local_file, dl_attempts = download_with_retry(remote_path, staging_dir)
        except Exception as e:
            log(f"FAILED (download, {MAX_RETRIES} attempts exhausted): {remote_path}: {e}")
            upsert_manifest_row(
                rows, timestamp=datetime.now().isoformat(), mega_account=alias,
                mega_remote_path=remote_path, filename=item["filename"],
                file_size_bytes=item["file_size_bytes"] or "",
                mega_modified=item["mega_modified"].isoformat() if item["mega_modified"] else "",
                youtube_channel="", youtube_video_id="", status="failed",
                attempts=MAX_RETRIES, error=f"download: {e}",
            )
            save_manifest_rows(config, rows)
            continue

        log(f"Uploading {local_file.name} to channel '{channel_name}' ...")
        if yt_done:
            log("  (skipping — already succeeded on YouTube previously; only Drive is pending for this file)")
        else:
            try:
                video_id, ul_attempts = upload_video_with_retry(
                    clients[channel_name]["client"], local_file, title=local_file.stem,
                    privacy_status=config["upload"]["privacy_status"],
                    category_id=config["upload"]["category_id"],
                    made_for_kids=config["upload"].get("made_for_kids", False),
                )
                log(f"SUCCESS: {local_file.name} -> https://youtu.be/{video_id} (channel: {channel_name})")
                upsert_manifest_row(
                    rows, timestamp=datetime.now().isoformat(), mega_account=alias,
                    mega_remote_path=remote_path, filename=item["filename"],
                    file_size_bytes=item["file_size_bytes"] or "",
                    mega_modified=item["mega_modified"].isoformat() if item["mega_modified"] else "",
                    youtube_channel=channel_name, youtube_video_id=video_id,
                    status="success", attempts=ul_attempts, error="",
                )
            except QuotaExceeded as e:
                log(f"Channel '{channel_name}' hit its daily quota ({e}). Marking exhausted for today; will retry this file on a later run.")
                clients[channel_name]["exhausted_today"] = True
                # deliberately NOT marked failed — no row written, so a future run just tries it again
            except Exception as e:
                log(f"FAILED (upload, {MAX_RETRIES} attempts exhausted): {local_file.name}: {e}")
                upsert_manifest_row(
                    rows, timestamp=datetime.now().isoformat(), mega_account=alias,
                    mega_remote_path=remote_path, filename=item["filename"],
                    file_size_bytes=item["file_size_bytes"] or "",
                    mega_modified=item["mega_modified"].isoformat() if item["mega_modified"] else "",
                    youtube_channel="", youtube_video_id="", status="failed",
                    attempts=MAX_RETRIES, error=str(e),
                )

        # --- Google Drive upload (independent of YouTube outcome, same local file) ---
        if drive_enabled and not drive_done and not drive_exhausted_today and local_file.exists():
            log(f"Uploading {local_file.name} to Google Drive ...")
            try:
                drive_id, dr_attempts = upload_to_drive_with_retry(
                    drive_client, local_file, folder_id=drive_cfg.get("folder_id"),
                )
                log(f"SUCCESS: {local_file.name} -> Drive file id {drive_id}")
                upsert_manifest_row(
                    rows, mega_account=alias, mega_remote_path=remote_path,
                    filename=item["filename"], file_size_bytes=item["file_size_bytes"] or "",
                    drive_status="success", drive_file_id=drive_id, drive_error="",
                )
            except QuotaExceeded as e:
                log(f"Google Drive hit its quota/storage limit ({e}). Skipping Drive uploads for the rest of this run; will retry on a later run.")
                drive_exhausted_today = True
                # not marked failed — no drive_status written, so a future run retries it
            except PermanentApiError as e:
                # Needs a human in Google Cloud, not another attempt. Stop trying
                # Drive for the rest of the run and say exactly what to fix, so
                # the remaining YouTube uploads still get their 6 hours.
                log("=" * 70)
                log(f"STOPPING Google Drive uploads for this run: {e}")
                log("")
                log("This is a project/API configuration problem, not a per-file failure,")
                log("so retrying would fail identically for all 250+ remaining files.")
                log("")
                log("To fix:")
                log("  1. Google Cloud Console > your project > APIs & Services > Library")
                log("  2. Search 'Google Drive API' > Enable   (this is the usual cause)")
                log("  3. If it is already enabled, check the Drive account is added as a")
                log("     test user on the OAuth consent screen")
                log("")
                log("YouTube uploads are unaffected and will continue.")
                log("=" * 70)
                drive_exhausted_today = True
                drive_blocked_reason = str(e)
                # not marked failed — no drive_status written, so a future run retries it
            except Exception as e:
                log(f"FAILED (Drive upload, {MAX_RETRIES} attempts exhausted): {local_file.name}: {e}")
                upsert_manifest_row(
                    rows, mega_account=alias, mega_remote_path=remote_path,
                    filename=item["filename"], file_size_bytes=item["file_size_bytes"] or "",
                    drive_status="failed", drive_file_id="", drive_error=str(e),
                )

        if config["behavior"].get("delete_local_after_upload", True) and local_file.exists():
            local_file.unlink()
        save_manifest_rows(config, rows)

        if single_file_test:
            log("Test-single mode: stopping after one file.")
            return

    if drive_exhausted_today and drive_blocked_reason:
        log("Run complete, with Google Drive disabled for this run (see the reason above).")
        log("Drive copies are pending in the manifest and will be attempted on the next run.")
    else:
        log("Run complete.")


def authorize_all(config):
    for ch in config["youtube_channels"]:
        get_youtube_client(ch, authorize_only=True)
        log(f"Channel '{ch['name']}' authorized (or already had a valid token).")
    drive_cfg = config.get("google_drive")
    if drive_cfg and drive_cfg.get("enabled", False):
        get_drive_client(drive_cfg, authorize_only=True)
        log("Google Drive authorized (or already had a valid token).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-only", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--test-single", action="store_true")
    parser.add_argument("--run-all", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    cfg = load_config()

    if args.authorize_only:
        authorize_all(cfg)
    elif args.list_only:
        list_only(cfg)
    elif args.test_single:
        run(cfg, single_file_test=True)
    elif args.retry_failed:
        run(cfg, retry_failed_only=True)
    elif args.run_all:
        run(cfg, single_file_test=False)
    else:
        parser.print_help()
