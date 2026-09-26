#!/usr/bin/env python3
"""
verify_drive_pipeline.py

The "nothing lost" check for drive_to_mega_youtube.py. Re-scans your actual
Drive landing folder (the source of truth) fresh, and cross-checks every
file found against manifest_drive_source.csv.

Reports, per file:
  - DONE            success on BOTH MEGA and YouTube
  - PARTIAL         succeeded on one destination but not the other
  - FAILED          marked failed on at least one destination
  - NEVER ATTEMPTED — exists in Drive but no manifest row at all yet

Also cross-checks your MEGA account capacity plan: flags any file that
doesn't fit in ANY configured account's remaining space (would otherwise
silently never get uploaded to MEGA — this is the one way a file could
truly get stuck, so it's called out explicitly here).

Exit code 0 = fully complete. Exit code 1 = something outstanding.

Usage:
    python3 verify_drive_pipeline.py
"""

import csv
import sys

from drive_to_mega_youtube import (
    BASE_DIR, load_config, get_google_client, list_drive_videos,
    pack_files_to_accounts, DRIVE_SOURCE_SCOPES,
)


def main():
    config = load_config()
    allowed_ext = set(e.lower() for e in config.get("allowed_extensions", [".mkv"]))

    drive_src_cfg = config["drive_source"]
    drive = get_google_client(drive_src_cfg, DRIVE_SOURCE_SCOPES, "drive", "v3")
    if not drive:
        print("ERROR: could not create Drive client. Run --authorize-only first.")
        sys.exit(2)

    print("Re-scanning Drive landing folder (source of truth) ...\n")
    files = list_drive_videos(drive, drive_src_cfg["folder_id"], allowed_ext)
    for f in files:
        f["size_bytes"] = int(f["size"]) if f.get("size") else None

    manifest_path = BASE_DIR / config["behavior"].get("manifest_file", "manifest_drive_source.csv")
    manifest_rows = {}
    if manifest_path.exists():
        with open(manifest_path, newline="") as f:
            for row in csv.DictReader(f):
                manifest_rows[row["drive_file_id"]] = row

    # Capacity check: does every file even fit somewhere?
    assignments, usage_after, caps = pack_files_to_accounts(files, config, manifest_rows)
    assigned_ids = {f["id"] for f, _ in assignments}
    unfittable = [f for f in files if f["id"] not in assigned_ids]

    total = len(files)
    done = partial = failed = never = 0
    problems = []

    for f in files:
        row = manifest_rows.get(f["id"])
        mega_ok = bool(row and row.get("mega_status") == "success")
        yt_ok = bool(row and row.get("youtube_status") == "success")

        if not row:
            never += 1
            problems.append(("NEVER ATTEMPTED", f["name"], f["size_bytes"]))
        elif mega_ok and yt_ok:
            done += 1
        elif (row.get("mega_status") == "failed") or (row.get("youtube_status") == "failed"):
            failed += 1
            problems.append(("FAILED", f["name"], f["size_bytes"]))
        else:
            partial += 1
            problems.append(("PARTIAL", f["name"], f["size_bytes"]))

    print(f"{'='*70}")
    print(f"Drive source total files: {total}")
    print(f"  DONE (success on both MEGA and YouTube): {done}")
    print(f"  PARTIAL: {partial}")
    print(f"  FAILED: {failed}")
    print(f"  NEVER ATTEMPTED (no manifest row yet): {never}")
    print(f"{'='*70}\n")

    if unfittable:
        print(f"CAPACITY WARNING: {len(unfittable)} file(s) do NOT fit in any "
              f"configured MEGA account's remaining space — these will never "
              f"upload to MEGA until you add more accounts or raise a cap_gb:")
        for f in unfittable:
            size_mb = f"{f['size_bytes']/1e6:.1f}MB" if f["size_bytes"] else "?"
            print(f"  {size_mb:>10}  {f['name']}")
        print()

    if problems:
        print(f"{'STATUS':18} {'SIZE':>10}  FILENAME")
        print("-" * 90)
        for status, name, size in problems:
            size_mb = f"{size/1e6:.1f}MB" if size else "?"
            print(f"{status:18} {size_mb:>10}  {name}")
        print(f"\n{len(problems)} file(s) not yet fully complete. A normal `--run-all` "
              f"automatically retries FAILED and NEVER ATTEMPTED files. Re-run this "
              f"verifier after the next scheduled run to confirm progress.")
        sys.exit(1)
    elif unfittable:
        sys.exit(1)
    else:
        print("Everything is fully accounted for. Every file in the Drive landing "
              "folder has succeeded on both MEGA and YouTube.")
        sys.exit(0)


if __name__ == "__main__":
    main()
