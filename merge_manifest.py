#!/usr/bin/env python3
"""Three-way merge for manifest.csv.

Why this exists
---------------
The pipeline commits manifest.csv back to the repo. If anyone pushes to main
while a run is in flight, the run's `git push` is rejected. The old code
rebased and retried, and if the rebase hit a conflict it gave up after three
attempts and printed a warning - the job still went green, but the manifest
rows for everything that run had uploaded were LOST. The next run then saw
those files as "never attempted" and re-uploaded them, creating duplicates.

A rebase conflict on a CSV is not something a human can adjudicate, and
"keep the remote version" is the wrong answer anyway: the remote may not
know about the files this run uploaded.

What this does
--------------
Proper three-way merge, keyed the same way the pipeline keys rows
(mega_account, mega_remote_path):

    base   - the manifest as it was BEFORE this run started
    ours   - the manifest the run just produced
    theirs - the manifest now on the remote (someone else's commit)

For each key:
  - only in ours      -> take ours (this run uploaded it)
  - only in theirs    -> take theirs (they added it)
  - in both, unchanged in ours vs base -> take theirs (we didn't touch it)
  - in both, changed in ours           -> take ours
  - changed in BOTH, differently       -> conflict: prefer the row with more
                                          progress, and say so loudly

Progress is measured by what has actually been achieved for a file:
drive copy done > youtube id present > status success > pending > failed.
That ordering means a row that reached "success" always wins over one that is
still pending, and a pending row wins over a failed one (a transient failure
should not undo a scheduled retry that is still in flight).

Usage:
    python merge_manifest.py --base BASE.csv --ours OURS.csv --theirs THEIRS.csv \
        --out OUT.csv
"""
import argparse
import csv
import os
import sys

FIELDS = [
    "timestamp", "mega_account", "mega_remote_path", "filename",
    "file_size_bytes", "mega_modified", "youtube_channel",
    "youtube_video_id", "status", "attempts", "error",
    "drive_status", "drive_file_id", "drive_error",
]

# Higher is better. Only compared when BOTH sides changed the same row.
STATUS_RANK = {
    "success": 40,
    "pending": 30,
    "failed": 20,
    "": 10,
}


def key_of(row):
    return (row.get("mega_account", ""), row.get("mega_remote_path", ""))


def progress(row):
    """How much has been achieved for this row. Higher wins a true conflict."""
    score = STATUS_RANK.get((row.get("status") or "").strip().lower(), 10)
    if (row.get("drive_file_id") or "").strip():
        score += 8
    if (row.get("drive_status") or "").strip().lower() == "success":
        score += 4
    if (row.get("youtube_video_id") or "").strip():
        score += 8
    if (row.get("youtube_channel") or "").strip():
        score += 2
    return score


def read_rows(path):
    """Read a manifest CSV into {key: row}. Missing file -> empty dict."""
    if not path or not os.path.exists(path):
        return {}
    rows = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[key_of(row)] = row
    return rows


def write_rows(path, ordered_keys, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for k in ordered_keys:
            row = rows.get(k)
            if row is None:
                continue
            w.writerow({f: row.get(f, "") for f in FIELDS})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--ours", required=True)
    ap.add_argument("--theirs", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    base = read_rows(args.base)
    ours = read_rows(args.ours)
    theirs = read_rows(args.theirs)

    merged = {}
    conflicts = 0
    taken_ours = 0
    taken_theirs = 0

    for k in set(base) | set(ours) | set(theirs):
        b, o, t = base.get(k), ours.get(k), theirs.get(k)

        if o is not None and t is None:
            merged[k] = o
            taken_ours += 1
            continue
        if t is not None and o is None:
            merged[k] = t
            taken_theirs += 1
            continue
        if o is None and t is None:
            continue

        # Both sides have this key.
        o_changed = (b is None) or (o != b)
        t_changed = (b is None) or (t != b)

        if not o_changed:
            merged[k] = t          # we didn't touch it; they may have
            taken_theirs += 1
            continue
        if not t_changed:
            merged[k] = o          # they didn't touch it; we did
            taken_ours += 1
            continue

        # Both changed it. Prefer whichever got further.
        if progress(o) >= progress(t):
            merged[k] = o
            taken_ours += 1
        else:
            merged[k] = t
            taken_theirs += 1
        conflicts += 1
        print(f"CONFLICT: {k[0]} {k[1]} - kept the row with more progress", flush=True)

    # Keep the order stable: base order first, then new rows from either side.
    order = []
    seen = set()
    for src in (base, ours, theirs):
        for k in src:
            if k not in seen:
                seen.add(k)
                order.append(k)

    write_rows(args.out, order, merged)

    print(f"merged: base={len(base)} ours={len(ours)} theirs={len(theirs)} "
          f"-> {len(merged)} rows "
          f"(ours={taken_ours} theirs={taken_theirs} conflicts={conflicts})",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
