#!/usr/bin/env python3
"""
verify_mega_pipeline.py

The "nothing lost" check for mega_to_youtube.py. Re-scans your actual MEGA
accounts (the source of truth) fresh — completely independent of
manifest.csv's own bookkeeping — and cross-checks every file found against
manifest.csv. Run this anytime, especially after you believe a batch is
"done," to confirm it actually is.

Reports, per file:
  - DONE            success on YouTube (and on Drive too, if Drive is enabled)
  - PARTIAL         succeeded on one destination but not the other
  - FAILED          marked failed in the manifest — needs a look
  - NEVER ATTEMPTED — exists on MEGA but has no manifest row at all (would
                      still get picked up by a future --run-all, but flagged
                      here so you know it hasn't happened yet)

Exit code 0 = fully complete (every file DONE on every enabled destination).
Exit code 1 = something still outstanding — see the printed report.

Usage:
    python3 verify_mega_pipeline.py
"""

import csv
import sys
from pathlib import Path

# Reuses the exact same MEGA-listing logic as the main pipeline, so "source
# of truth" here is guaranteed to match what the pipeline itself would see.
from mega_to_youtube import (
    BASE_DIR, load_config, mega_login, mega_list_videos_with_details, VIDEO_EXTENSIONS,
)


def main():
    config = load_config()
    allowed_ext = set(e.lower() for e in config.get("allowed_extensions", list(VIDEO_EXTENSIONS)))
    drive_enabled = bool(config.get("google_drive", {}).get("enabled", False))

    manifest_path = BASE_DIR / config["behavior"].get("manifest_file", "manifest.csv")
    manifest_rows = {}
    if manifest_path.exists():
        with open(manifest_path, newline="") as f:
            for row in csv.DictReader(f):
                manifest_rows[(row["mega_account"], row["mega_remote_path"])] = row

    print("Re-scanning MEGA accounts (source of truth) ...\n")
    all_source_files = []
    for idx, acc in enumerate(config["mega_accounts"]):
        alias = acc.get("alias") or f"mega_account_{idx+1}"
        if not acc.get("enabled", True):
            print(f"Skipping {alias} — marked enabled: false in config.")
            continue
        email = acc["email"]
        if not mega_login(email, acc["password"], alias):
            print(f"WARNING: could not log into {alias} — files on this account can't be verified right now.")
            continue
        details = mega_list_videos_with_details("/", allowed_extensions=allowed_ext)
        for d in details:
            all_source_files.append((alias, d["remote_path"], d["size_bytes"]))

    total = len(all_source_files)
    done = partial = failed = never = 0
    problems = []

    for alias, remote_path, size in all_source_files:
        row = manifest_rows.get((alias, remote_path))
        yt_ok = bool(row and row.get("status") == "success")
        drive_ok = (not drive_enabled) or bool(row and row.get("drive_status") == "success")

        if not row:
            never += 1
            problems.append(("NEVER ATTEMPTED", alias, remote_path, size))
        elif yt_ok and drive_ok:
            done += 1
        elif row.get("status") == "failed" or (drive_enabled and row.get("drive_status") == "failed"):
            failed += 1
            problems.append(("FAILED", alias, remote_path, size))
        else:
            partial += 1
            problems.append(("PARTIAL", alias, remote_path, size))

    print(f"{'='*70}")
    print(f"MEGA source total files: {total}")
    print(f"  DONE (complete on all enabled destinations): {done}")
    print(f"  PARTIAL (succeeded on one destination, not both): {partial}")
    print(f"  FAILED: {failed}")
    print(f"  NEVER ATTEMPTED (no manifest row yet): {never}")
    print(f"{'='*70}\n")

    if problems:
        print(f"{'STATUS':18} {'MEGA ACCOUNT':28} {'SIZE':>10}  REMOTE PATH")
        print("-" * 100)
        for status, alias, remote_path, size in problems:
            size_mb = f"{size/1e6:.1f}MB" if size else "?"
            print(f"{status:18} {alias:28} {size_mb:>10}  {remote_path}")
        print(f"\n{len(problems)} file(s) not yet fully complete. A normal `--run-all` "
              f"will automatically retry FAILED and NEVER ATTEMPTED files — nothing "
              f"needs to be manually re-queued. Re-run this verifier after the next "
              f"scheduled run to confirm progress.")
        sys.exit(1)
    else:
        print("Everything is fully accounted for. Every file on every scanned MEGA "
              "account has succeeded on all enabled destinations.")
        sys.exit(0)


if __name__ == "__main__":
    main()
