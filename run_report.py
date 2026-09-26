#!/usr/bin/env python3
"""
run_report.py

Per-run progress accounting, so the dispatcher workflow can tell whether a
pipeline run actually accomplished anything.

WHY THIS EXISTS:
    The obvious way for the dispatcher to detect progress is to diff the
    manifest against the previous git commit. That is broken, because when a
    run completes zero files the manifest does not change, the workflow's
    `git diff --quiet || git commit` skips the commit, and there is no new
    commit to compare against. The dispatcher would read the stale HEAD~1,
    conclude progress had been made, and re-dispatch forever -- burning Actions
    minutes in a loop that never terminates.

    So each run states its own outcome explicitly, keyed by run id. The
    dispatcher trusts the report only if its run_id matches the run that just
    finished; anything else is treated as "no progress" and the chain stops.
    Failing safe is the point: a false negative costs one day's cron, a false
    positive costs an infinite loop.

Usage (called from the pipeline workflows, two steps around the run):

    python3 run_report.py snapshot --manifest manifest.csv
    python3 run_report.py report --manifest manifest.csv \\
        --pipeline mega2yt --run-id "$GITHUB_RUN_ID" \\
        --run-attempt "$GITHUB_RUN_ATTEMPT"

`snapshot` must run BEFORE the pipeline; `report` AFTER. The snapshot is
stored in the runner's /tmp, which persists across steps within one job.
"""

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

# Where the pre-run manifest copy is stashed between the two steps.
SNAPSHOT_DIR = os.environ.get("RUN_REPORT_TMP", tempfile.gettempdir())
REPORT_PATH = "logs/last_run.json"

# Pipeline B rows carry mega_status/youtube_status; Pipeline A rows carry
# status (+ drive_status when the Drive backup is on). A file counts as done
# only if every destination it actually has enabled succeeded.
STATUS_COLUMNS = ("status", "drive_status", "mega_status", "youtube_status")


def read_rows(path):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f"warning: could not parse {path}: {e}", file=sys.stderr)
        return []


def row_done(row):
    """A file is done only if EVERY destination it actually has enabled
    succeeded. Checking only `status` would wrongly call a file complete when
    its YouTube upload worked but its Drive upload did not -- the pipeline
    itself tracks those independently and will retry the failed one."""
    present = [c for c in STATUS_COLUMNS if row.get(c) not in (None, "")]
    if not present:
        return False
    return all(row[c] == "success" for c in present)


def row_key(row, manifest):
    """Stable identity for a row, so a before/after comparison lines up even if
    row order shifts. Prefers the natural key each pipeline already uses."""
    if "drive_file_id" in row and row.get("drive_file_id"):
        return ("drive", row["drive_file_id"])
    if "mega_remote_path" in row:
        return ("mega", row.get("mega_account", ""), row.get("mega_remote_path", ""))
    return ("row", manifest, row.get("filename", ""), row.get("file_size_bytes", ""))


def counts(rows, manifest):
    done = sum(1 for r in rows if row_done(r))
    return done, len(rows) - done


def cmd_snapshot(args):
    dest_dir = os.path.join(SNAPSHOT_DIR, "run_report")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(args.manifest) + ".before")
    if os.path.exists(args.manifest):
        shutil.copyfile(args.manifest, dest)
        print(f"snapshot: {args.manifest} -> {dest}")
    else:
        # No manifest yet means a first run; record that explicitly so the
        # report doesn't read it as "everything is new" by accident.
        with open(dest, "w", encoding="utf-8") as f:
            json.dump({"__no_manifest__": True}, f)
        print(f"snapshot: {args.manifest} does not exist yet (first run)")


def cmd_report(args):
    before_path = os.path.join(SNAPSHOT_DIR, "run_report",
                               os.path.basename(args.manifest) + ".before")
    before_rows = []
    first_run = False
    if os.path.exists(before_path):
        raw = open(before_path, encoding="utf-8").read()
        if raw.lstrip().startswith("{"):
            first_run = True
        else:
            before_rows = read_rows(before_path)

    after_rows = read_rows(args.manifest)
    before_map = {row_key(r, args.manifest): r for r in before_rows}
    after_map = {row_key(r, args.manifest): r for r in after_rows}

    completed_this_run = 0
    for key, row in after_map.items():
        if not row_done(row):
            continue
        prev = before_map.get(key)
        if first_run or prev is None or not row_done(prev):
            completed_this_run += 1

    done_total, pending_total = counts(after_rows, args.manifest)
    done_before, pending_before = counts(before_rows, args.manifest)

    report = {
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "pipeline": args.pipeline,
        "manifest": args.manifest,
        "completed_this_run": completed_this_run,
        "completed_total": done_total,
        "pending_total": pending_total,
        "completed_before_run": done_before,
        "pending_before_run": pending_before,
        "rows_seen": len(after_rows),
        "first_run": first_run,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs(os.path.dirname(REPORT_PATH) or ".", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"run report -> {REPORT_PATH}")
    print(f"  completed this run : {completed_this_run}")
    print(f"  completed total    : {done_total}")
    print(f"  pending total      : {pending_total}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="copy the manifest before the pipeline runs")
    s.add_argument("--manifest", required=True)
    s.set_defaults(func=cmd_snapshot)

    r = sub.add_parser("report", help="write logs/last_run.json after the pipeline runs")
    r.add_argument("--manifest", required=True)
    r.add_argument("--pipeline", required=True)
    r.add_argument("--run-id", required=True)
    r.add_argument("--run-attempt", default="1")
    r.set_defaults(func=cmd_report)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
