#!/usr/bin/env python3
"""
match_local_files.py

Your local PC has copies of the same videos that are scattered across 12
MEGA accounts (some in subfolders), and many share the SAME filename. That
means filename alone can't tell you which local file corresponds to which
row in manifest.csv — you need filename + file size together, since it's
extremely unlikely two different videos share both.

Usage (run this on your own PC, pointing at your local video folder AND a
copy of manifest.csv pulled down from the server):

    python3 match_local_files.py /path/to/local/videos manifest.csv

Output: a CSV `local_match_report.csv` with one row per local file, showing
which MEGA account/path it matched to (by filename+size) and, if already
uploaded, which YouTube channel and video ID it went to.

If a local file's filename appears more than once in manifest.csv with
DIFFERENT sizes, all candidate matches are listed — check those manually,
since size alone breaks the tie in the vast majority of cases but re-encoded
or re-exported duplicates could theoretically collide.
"""

import csv
import sys
from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".m4v"}


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 match_local_files.py <local_video_folder> <manifest.csv>")
        sys.exit(1)

    local_dir = Path(sys.argv[1])
    manifest_path = Path(sys.argv[2])

    if not local_dir.exists() or not manifest_path.exists():
        print("ERROR: local folder or manifest.csv path doesn't exist.")
        sys.exit(1)

    # Build lookup: filename -> list of manifest rows
    by_filename = {}
    with open(manifest_path, newline="") as f:
        for row in csv.DictReader(f):
            by_filename.setdefault(row["filename"], []).append(row)

    local_files = [p for p in local_dir.rglob("*") if p.suffix.lower() in VIDEO_EXTENSIONS]
    print(f"Found {len(local_files)} local video file(s) under {local_dir}")

    report_rows = []
    for lf in local_files:
        local_size = lf.stat().st_size
        candidates = by_filename.get(lf.name, [])
        exact = [c for c in candidates if c.get("file_size_bytes") and int(c["file_size_bytes"]) == local_size]

        if exact:
            for c in exact:
                report_rows.append({
                    "local_path": str(lf),
                    "local_size_bytes": local_size,
                    "match_confidence": "exact (filename+size)",
                    "mega_account": c["mega_account"],
                    "mega_remote_path": c["mega_remote_path"],
                    "status": c["status"],
                    "youtube_channel": c["youtube_channel"],
                    "youtube_video_id": c["youtube_video_id"],
                })
        elif candidates:
            for c in candidates:
                report_rows.append({
                    "local_path": str(lf),
                    "local_size_bytes": local_size,
                    "match_confidence": "AMBIGUOUS (filename matches, size differs — check manually)",
                    "mega_account": c["mega_account"],
                    "mega_remote_path": c["mega_remote_path"],
                    "status": c["status"],
                    "youtube_channel": c["youtube_channel"],
                    "youtube_video_id": c["youtube_video_id"],
                })
        else:
            report_rows.append({
                "local_path": str(lf),
                "local_size_bytes": local_size,
                "match_confidence": "NO MATCH FOUND in manifest",
                "mega_account": "", "mega_remote_path": "", "status": "",
                "youtube_channel": "", "youtube_video_id": "",
            })

    out_path = Path("local_match_report.csv")
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "local_path", "local_size_bytes", "match_confidence", "mega_account",
            "mega_remote_path", "status", "youtube_channel", "youtube_video_id",
        ])
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"Wrote {len(report_rows)} row(s) to {out_path.resolve()}")


if __name__ == "__main__":
    main()
