#!/usr/bin/env python3
"""
drive_to_mega_youtube.py

Drive is the LANDING ZONE: you upload new videos from your PC straight to
one Drive folder (your fastest single hop). This script — run headless via
GitHub Actions, PC off — fans each video out to:

  1. MEGA (your real long-term storage), spread across many capacity-capped
     accounts (e.g. 30 accounts x 20GB free tier each), packed greedily and
     uploaded IN PARALLEL across accounts (one worker per account in use —
     this is what actually gets you speed: separate Mega accounts have
     separate throttling, so N accounts uploading at once can use more of
     your total available bandwidth than one account at a time).
  2. YouTube (backup copy), same round-robin-across-channels logic as before.

Only files matching `allowed_extensions` in config (default: .mkv only) are
ever considered — anything else in the Drive folder is ignored.

Same safety principles as the other script: bounded retries, quota errors
skipped (not retried same-run), persistent CSV manifest, no infinite loops.

Run modes:
  --authorize-only   one-time OAuth browser flow for Drive (source) and
                      each YouTube channel
  --list-only        preview what's in the Drive folder + the account
                      packing plan, no downloads/uploads
  --test-single       process exactly one file end-to-end
  --run-all           full run
  --retry-failed      re-attempt only files marked failed
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config_drive_source.yaml"
LOG_PATH = BASE_DIR / "logs" / "run_drive_source.log"

DRIVE_SOURCE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
                  "https://www.googleapis.com/auth/youtube"]

QUOTA_ERROR_REASONS_YT = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"}
QUOTA_ERROR_REASONS_DRIVE = {"quotaExceeded", "userRateLimitExceeded"}
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 10

MANIFEST_FIELDS = [
    "timestamp", "drive_file_id", "filename", "file_size_bytes",
    "mega_account", "mega_status", "mega_remote_path", "mega_error",
    "youtube_channel", "youtube_video_id", "youtube_status", "youtube_error",
    "attempts",
]

log_lock = threading.Lock()
manifest_lock = threading.Lock()

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


def log(msg, prefix=""):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]{redact(prefix)} {redact(msg)}"
    with log_lock:
        print(line)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")


def load_config():
    if not CONFIG_PATH.exists():
        log(f"ERROR: {CONFIG_PATH} not found. Copy config_drive_source.yaml.template and fill it in.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def manifest_path(config):
    return BASE_DIR / config["behavior"].get("manifest_file", "manifest_drive_source.csv")


def load_manifest_rows(config):
    p = manifest_path(config)
    rows = {}
    if p.exists():
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                rows[row["drive_file_id"]] = row
    return rows


def save_manifest_rows(config, rows):
    with manifest_lock:
        p = manifest_path(config)
        with open(p, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for row in rows.values():
                writer.writerow({k: redact(v) for k, v in row.items() if k in MANIFEST_FIELDS})


def upsert_manifest_row(rows, config, **fields):
    with manifest_lock:
        key = fields["drive_file_id"]
        existing = rows.get(key, {})
        existing.update(fields)
        for f in MANIFEST_FIELDS:
            existing.setdefault(f, "")
        rows[key] = existing
    save_manifest_rows(config, rows)


class QuotaExceeded(Exception):
    pass


# ---------------------------------------------------------------------------
# Google OAuth (shared pattern for Drive source + each YouTube channel)
# ---------------------------------------------------------------------------

def get_google_client(cfg, scopes, service, version, authorize_only=False):
    creds = None
    token_path = BASE_DIR / cfg["token_file"]
    secret_path = BASE_DIR / cfg["client_secret_file"]

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret_path.exists():
                log(f"ERROR: missing client secret file {secret_path}")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), scopes)
            log(f"--- Authorize '{cfg.get('name', service)}': follow the printed URL/instructions ---")
            creds = flow.run_console()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    if authorize_only:
        return None
    return build(service, version, credentials=creds)


# ---------------------------------------------------------------------------
# Drive (source) side
# ---------------------------------------------------------------------------

def list_drive_videos(drive, folder_id, allowed_extensions):
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed=false"
    while True:
        resp = drive.files().list(
            q=query, spaces="drive",
            fields="nextPageToken, files(id, name, size, mimeType, modifiedTime)",
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            if Path(f["name"]).suffix.lower() in allowed_extensions:
                files.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_from_drive(drive, file_id, filename, local_dir):
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / filename
    request = drive.files().get_media(fileId=file_id)
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            try:
                status, done = downloader.next_chunk()
            except HttpError as e:
                reason = ""
                try:
                    reason = json.loads(e.content).get("error", {}).get("errors", [{}])[0].get("reason", "")
                except Exception:
                    pass
                if reason in QUOTA_ERROR_REASONS_DRIVE:
                    raise QuotaExceeded(reason)
                raise
    return local_path


def download_with_retry(drive, file_id, filename, local_dir):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return download_from_drive(drive, file_id, filename, local_dir), attempt
        except QuotaExceeded:
            raise
        except Exception as e:
            last_error = e
            log(f"  Drive download attempt {attempt}/{MAX_RETRIES} failed for {filename}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise last_error


# ---------------------------------------------------------------------------
# MEGA (destination) side — via mega-cmd, with ISOLATED sessions per account
# so multiple accounts can be logged in and uploading AT THE SAME TIME.
#
# mega-cmd keeps its session/config under $HOME/.megaCmd by default. Giving
# each parallel worker its own temp HOME means each gets its own background
# mega-cmd server + session, instead of fighting over one shared login.
# This is the mechanism that makes true concurrent multi-account uploads
# possible. Test with 2 accounts before trusting it at 30-account scale.
# ---------------------------------------------------------------------------

def make_isolated_env():
    fake_home = tempfile.mkdtemp(prefix="megacmd_home_")
    env = os.environ.copy()
    env["HOME"] = fake_home
    return env


def mega_login(email, password, env, alias=None):
    try:
        subprocess.run(["mega-logout"], capture_output=True, env=env)
        result = subprocess.run(["mega-login", email, password], capture_output=True, text=True, env=env)
    except FileNotFoundError:
        log(f"FATAL: mega-cmd is not installed or not on PATH — cannot log into {alias or '[account]'}.")
        return False
    if result.returncode != 0:
        log(f"MEGA login FAILED for {alias or '[account]'} — account may not exist, be suspended, "
            f"or have wrong credentials: {redact(result.stderr.strip())}")
        return False
    return True


def mega_upload_once(local_path, remote_dir, env):
    result = subprocess.run(["mega-put", str(local_path), remote_dir], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        stderr = redact(result.stderr.strip())
        if any(w in stderr.lower() for w in ("quota", "storage", "over quota")):
            raise QuotaExceeded(stderr)
        raise RuntimeError(f"mega-put failed: {stderr}")
    return f"{remote_dir.rstrip('/')}/{local_path.name}"


def mega_upload_with_retry(local_path, remote_dir, env):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return mega_upload_once(local_path, remote_dir, env), attempt
        except QuotaExceeded:
            raise
        except Exception as e:
            last_error = e
            log(f"  MEGA upload attempt {attempt}/{MAX_RETRIES} failed for {local_path.name}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise last_error


# ---------------------------------------------------------------------------
# Capacity-aware account packing (greedy bin-packing)
# ---------------------------------------------------------------------------

def compute_account_usage(config, rows):
    """For each configured Mega account: starting_used_gb (what you told the
    config it already holds) + sum of bytes this manifest has already
    successfully uploaded to it. Returns {email: used_bytes}."""
    usage = {}
    for acc in config["mega_destination_accounts"]:
        usage[acc["email"]] = acc.get("starting_used_gb", 0) * (1024 ** 3)
    for row in rows.values():
        if row.get("mega_status") == "success" and row.get("mega_account"):
            acc = row["mega_account"]
            size = int(row["file_size_bytes"]) if row.get("file_size_bytes") else 0
            usage[acc] = usage.get(acc, 0) + size
    return usage


def pack_files_to_accounts(files_with_size, config, rows):
    """Greedy bin-packing: walk accounts in order, fill each up to its cap
    before moving to the next. Returns list of (file, account_cfg) pairs;
    files that don't fit anywhere (all accounts full, or all disabled) are
    returned separately.

    Accounts with enabled: false in config are excluded entirely — without
    this, a permanently dead/deleted account would keep being offered
    capacity every run (since it's never "full", just unreachable), which
    would silently strand files trying it forever instead of routing them
    to a working account."""
    all_accounts = config["mega_destination_accounts"]
    accounts = [a for a in all_accounts if a.get("enabled", True)]
    skipped_count = len(all_accounts) - len(accounts)
    if skipped_count:
        log(f"{skipped_count} MEGA destination account(s) marked enabled: false — excluded from packing.")
    if not accounts:
        log("WARNING: no enabled MEGA destination accounts configured — nothing can be packed this run.")
        return [], {}, {}

    usage = compute_account_usage(config, rows)
    caps = {a["email"]: a.get("cap_gb", 20) * (1024 ** 3) for a in accounts}
    by_email = {a["email"]: a for a in accounts}

    assignments = []
    unassigned = []
    acc_idx = 0

    for f in files_with_size:
        size = f["size_bytes"] or 0
        placed = False
        attempts = 0
        while attempts < len(accounts):
            acc = accounts[acc_idx % len(accounts)]
            email = acc["email"]
            if usage.get(email, 0) + size <= caps[email]:
                assignments.append((f, acc))
                usage[email] = usage.get(email, 0) + size
                placed = True
                break
            acc_idx += 1
            attempts += 1
        if not placed:
            unassigned.append(f)

    return assignments, usage, caps


# ---------------------------------------------------------------------------
# YouTube (destination) side
# ---------------------------------------------------------------------------

def upload_youtube_once(youtube, filepath, title, privacy_status, category_id, made_for_kids=False):
    body = {
        "snippet": {"title": title, "description": f"Backup upload: {filepath.name}", "categoryId": category_id},
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
            if reason in QUOTA_ERROR_REASONS_YT:
                raise QuotaExceeded(reason)
            raise
    return response.get("id")


def upload_youtube_with_retry(youtube, filepath, title, privacy_status, category_id, made_for_kids=False):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return upload_youtube_once(youtube, filepath, title, privacy_status, category_id, made_for_kids), attempt
        except QuotaExceeded:
            raise
        except Exception as e:
            last_error = e
            log(f"  YouTube upload attempt {attempt}/{MAX_RETRIES} failed for {filepath.name}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    raise last_error


# ---------------------------------------------------------------------------
# Per-account worker: downloads its assigned files from Drive, uploads each
# to its one Mega account (sequentially within the account, safe), and
# queues each for YouTube afterward. Workers for DIFFERENT accounts run
# concurrently via ThreadPoolExecutor — this is the actual parallelism.
# ---------------------------------------------------------------------------

def process_account_batch(account_cfg, file_list, config, rows, drive, staging_root):
    email = account_cfg["email"]
    # Never fall back to the real address as the alias — that alias reaches log
    # lines, the committed manifest, and the staging path, all of which are
    # public on this repo. A missing alias is a config mistake; say so loudly
    # and use a positional placeholder instead of leaking the email.
    alias = account_cfg.get("alias")
    if not alias:
        alias = f"storage_unnamed_{abs(hash(email)) % 100000}"
        log(f"WARNING: no 'alias' set for this MEGA account — using placeholder "
            f"'{alias}' in logs/manifest. Set an alias in config to keep logs meaningful.", prefix=f" [{alias}]")
    prefix = f" [{alias}]"
    env = make_isolated_env()
    if not mega_login(email, account_cfg["password"], env, alias):
        log("Login failed, skipping this account's whole batch this run.", prefix)
        return
    staging_dir = staging_root / alias.replace("@", "_at_").replace(" ", "_")

    for f in file_list:
        file_id, filename, size_bytes = f["id"], f["name"], f["size_bytes"]
        existing = rows.get(file_id)
        if existing and existing.get("mega_status") == "success":
            continue

        log(f"Downloading {filename} from Drive ...", prefix)
        try:
            local_path, _ = download_with_retry(drive, file_id, filename, staging_dir)
        except Exception as e:
            log(f"FAILED (Drive download): {filename}: {e}", prefix)
            upsert_manifest_row(rows, config, timestamp=datetime.now().isoformat(),
                                 drive_file_id=file_id, filename=filename,
                                 file_size_bytes=size_bytes or "",
                                 mega_account=alias, mega_status="failed",
                                 mega_error=f"download: {e}")
            continue

        log(f"Uploading {filename} to MEGA ...", prefix)
        try:
            remote_path, _ = mega_upload_with_retry(local_path, account_cfg.get("remote_dir", "/"), env)
            log(f"SUCCESS: {filename} -> {remote_path}", prefix)
            upsert_manifest_row(rows, config, timestamp=datetime.now().isoformat(),
                                 drive_file_id=file_id, filename=filename,
                                 file_size_bytes=size_bytes or "",
                                 mega_account=alias, mega_status="success",
                                 mega_remote_path=remote_path, mega_error="")
        except QuotaExceeded as e:
            log(f"Account full/quota hit ({e}) — unexpected, capacity planner should have avoided this. Marking failed for review.", prefix)
            upsert_manifest_row(rows, config, timestamp=datetime.now().isoformat(),
                                 drive_file_id=file_id, filename=filename,
                                 file_size_bytes=size_bytes or "",
                                 mega_account=alias, mega_status="failed", mega_error=str(e))
        except Exception as e:
            log(f"FAILED (MEGA upload): {filename}: {e}", prefix)
            upsert_manifest_row(rows, config, timestamp=datetime.now().isoformat(),
                                 drive_file_id=file_id, filename=filename,
                                 file_size_bytes=size_bytes or "",
                                 mega_account=alias, mega_status="failed", mega_error=str(e))
        finally:
            if config["behavior"].get("delete_local_after_upload", True) and local_path.exists():
                local_path.unlink()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(config, single_file_test=False, retry_failed_only=False, list_only=False):
    drive_src_cfg = config["drive_source"]
    drive = get_google_client(drive_src_cfg, DRIVE_SOURCE_SCOPES, "drive", "v3")
    if not drive:
        log("Could not create Drive source client. Run --authorize-only first.")
        return

    allowed_ext = set(e.lower() for e in config.get("allowed_extensions", [".mkv"]))
    files = list_drive_videos(drive, drive_src_cfg["folder_id"], allowed_ext)
    for f in files:
        f["size_bytes"] = int(f["size"]) if f.get("size") else None
    log(f"Found {len(files)} matching video(s) ({', '.join(sorted(allowed_ext))}) in Drive landing folder.")

    rows = load_manifest_rows(config)
    assignments, usage_after, caps = pack_files_to_accounts(files, config, rows)

    if list_only:
        print(f"\n{'ACCOUNT':30} {'SIZE':>10}  FILENAME")
        print("-" * 90)
        for f, acc in assignments:
            size_mb = f"{f['size_bytes']/1e6:.1f}MB" if f["size_bytes"] else "?"
            print(f"{acc['email']:30} {size_mb:>10}  {f['name']}")
        print(f"\nAccount fill plan (after this batch):")
        for acc in config["mega_destination_accounts"]:
            email = acc["email"]
            used_gb = usage_after.get(email, 0) / (1024**3)
            cap_gb = caps[email] / (1024**3)
            print(f"  {email:30} {used_gb:6.2f} / {cap_gb:.0f} GB")
        assigned_ids = {f["id"] for f, _ in assignments}
        skipped = [f for f in files if f["id"] not in assigned_ids]
        if skipped:
            print(f"\n{len(skipped)} file(s) don't fit in ANY configured account's remaining capacity — add more accounts.")
        return

    if retry_failed_only:
        assignments = [(f, acc) for f, acc in assignments
                        if rows.get(f["id"], {}).get("mega_status") == "failed"]

    if single_file_test:
        assignments = assignments[:1]

    # Group by account so each account's files stay sequential within that
    # account's own worker, while different accounts run concurrently.
    by_account = {}
    for f, acc in assignments:
        by_account.setdefault(acc["email"], (acc, []))[1].append(f)

    staging_root = BASE_DIR / "downloads"
    max_workers = min(config.get("parallel_workers", 5), len(by_account)) or 1
    log(f"Processing {len(assignments)} file(s) across {len(by_account)} MEGA account(s), {max_workers} in parallel ...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_account_batch, acc, flist, config, rows, drive, staging_root)
            for acc, flist in by_account.values()
        ]
        for fut in as_completed(futures):
            fut.result()  # re-raises any unhandled exception so it's visible, not swallowed

    # --- YouTube pass: after Mega is done, back up each successfully-stored
    # (or even Mega-failed, doesn't matter — YouTube is independent) file ---
    yt_channels = config["youtube_channels"]
    yt_clients = {}
    for ch in yt_channels:
        yt = get_google_client(ch, YOUTUBE_SCOPES, "youtube", "v3")
        if yt:
            yt_clients[ch["name"]] = {"client": yt, "exhausted_today": False}
    if not yt_clients:
        log("No valid YouTube clients — skipping YouTube backup pass this run.")
        return

    yt_channel_names = list(yt_clients.keys())
    yt_cycle_idx = 0

    for f, acc in assignments:
        file_id, filename = f["id"], f["name"]
        existing = rows.get(file_id)
        if existing and existing.get("youtube_status") == "success":
            continue
        if retry_failed_only and not (existing and existing.get("youtube_status") == "failed"):
            continue

        available = [n for n in yt_channel_names if not yt_clients[n]["exhausted_today"]]
        if not available:
            log("All YouTube channels exhausted for today — remaining files retry on next scheduled run.")
            break
        yt_cycle_idx = yt_cycle_idx % len(available)
        channel_name = available[yt_cycle_idx]
        yt_cycle_idx += 1

        staging_dir = staging_root / (acc.get("alias") or "storage_unnamed").replace("@", "_at_").replace(" ", "_")
        log(f"Re-downloading {filename} from Drive for YouTube backup ...")
        try:
            local_path, _ = download_with_retry(drive, file_id, filename, staging_dir)
        except Exception as e:
            log(f"FAILED (Drive re-download for YouTube): {filename}: {e}")
            continue

        log(f"Uploading {filename} to YouTube channel '{channel_name}' ...")
        try:
            video_id, _ = upload_youtube_with_retry(
                yt_clients[channel_name]["client"], local_path, title=local_path.stem,
                privacy_status=config["upload"]["privacy_status"],
                category_id=config["upload"]["category_id"],
                made_for_kids=config["upload"].get("made_for_kids", False),
            )
            log(f"SUCCESS: {filename} -> https://youtu.be/{video_id} ({channel_name})")
            upsert_manifest_row(rows, config, drive_file_id=file_id, filename=filename,
                                 youtube_channel=channel_name, youtube_video_id=video_id,
                                 youtube_status="success", youtube_error="")
        except QuotaExceeded as e:
            log(f"Channel '{channel_name}' hit its daily quota ({e}). Marking exhausted for today.")
            yt_clients[channel_name]["exhausted_today"] = True
        except Exception as e:
            log(f"FAILED (YouTube upload): {filename}: {e}")
            upsert_manifest_row(rows, config, drive_file_id=file_id, filename=filename,
                                 youtube_status="failed", youtube_error=str(e))
        finally:
            if config["behavior"].get("delete_local_after_upload", True) and local_path.exists():
                local_path.unlink()

        if single_file_test:
            break

    log("Run complete.")


def authorize_all(config):
    get_google_client(config["drive_source"], DRIVE_SOURCE_SCOPES, "drive", "v3", authorize_only=True)
    log("Drive source authorized (or already had a valid token).")
    for ch in config["youtube_channels"]:
        get_google_client(ch, YOUTUBE_SCOPES, "youtube", "v3", authorize_only=True)
        log(f"Channel '{ch['name']}' authorized (or already had a valid token).")


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
        run(cfg, list_only=True)
    elif args.test_single:
        run(cfg, single_file_test=True)
    elif args.retry_failed:
        run(cfg, retry_failed_only=True)
    elif args.run_all:
        run(cfg)
    else:
        parser.print_help()
