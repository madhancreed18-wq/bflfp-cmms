"""Bring the stand-alone PR Document Tracking app's data into Smart Factory (once, before go-live;
safe to run again — what was already brought in is skipped).

    python tools/import_pr_tracker.py --src "E:\\github-projects\\PR-Document-Tracking"

Copies: suppliers, purchase requests (+ item lines), quotation / PR / PO files, activity log
(not the "opened" rows). Users are NOT copied: give PR roles in CMMS › Manage › Users. The old
requester is matched to a CMMS user by full name when one has exactly that name.
Stop the CMMS first, or run it on a copy of data/ and swap it in.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="the PR-Document-Tracking folder")
    a = ap.parse_args()
    src_db = os.path.join(a.src, "instance", "pr_tracker.sqlite")
    src_up = os.path.join(a.src, "instance", "uploads")
    if not os.path.exists(src_db):
        sys.exit(f"not found: {src_db}")
    from server.db import init
    from server import pr
    init()
    print(f"importing {src_db} -> {pr.FILES}")
    pr.import_legacy(src_db, src_up)


if __name__ == "__main__":
    main()
