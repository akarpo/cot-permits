#!/usr/bin/env python3
"""verify_bsa_guids.py — daily spot-check that stored BSA Online record GUIDs
still point at the right Troy permits.

Samples N permits with resolved bsa_guid, fetches each BSA detail page, and
confirms the permit number appears in the rendered HTML. Designed to catch
silent drift — if BS&A rebuilds their index and re-issues GUIDs, every old
deep-link in our index.html would lead to the wrong record. This script is
the canary.

Exit codes:
  0  No mismatches above threshold (transient errors don't count).
  1  Real mismatches exceeded threshold — GUIDs likely drifted.
  2  Cannot verify (no DB, no resolved permits, etc.) — workflow should
     note but not alarm.
"""
import argparse
import os
import re
import sqlite3
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "permits.db")
REPORT_PATH = os.path.join(HERE, "verify-report.txt")
BSA_DETAIL_URL = "https://bsaonline.com/CD_RecordDetails/Permit"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}


def check_guid(session, permit_number, guid):
    """Returns one of 'OK', 'MISMATCH', 'ERROR' plus a short detail string."""
    try:
        r = session.get(BSA_DETAIL_URL,
                        params={"permitId": guid, "uid": 406},
                        timeout=25)
        r.raise_for_status()
    except requests.RequestException as e:
        return "ERROR", str(e)[:120]
    # Strip tags to a flat text blob and look for the permit number literally.
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"\s+", " ", text)
    if permit_number in text:
        return "OK", ""
    return "MISMATCH", f"permit number absent (HTML len={len(r.text):,})"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=20,
                    help="how many permits to spot-check (default 20)")
    ap.add_argument("--throttle", type=float, default=0.6,
                    help="seconds between BSA calls (default 0.6)")
    ap.add_argument("--mismatch-threshold", type=int, default=3,
                    help="exit 1 if real mismatches >= this many (default 3)")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"permits.db not found at {DB_PATH}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT permit_number, bsa_guid FROM permits "
        "WHERE bsa_guid IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?", (args.sample,)).fetchall()
    conn.close()

    if not rows:
        print("no permits with resolved BSA GUIDs — nothing to verify",
              file=sys.stderr)
        return 2

    print(f"verifying {len(rows)} random BSA GUID(s)...")
    session = requests.Session()
    session.headers.update(HEADERS)

    counts = {"OK": 0, "MISMATCH": 0, "ERROR": 0}
    mismatches = []
    last_call = 0.0
    for pn, guid in rows:
        gap = time.time() - last_call
        if gap < args.throttle:
            time.sleep(args.throttle - gap)
        last_call = time.time()
        verdict, detail = check_guid(session, pn, guid)
        counts[verdict] += 1
        flag = {"OK": "  ok ", "MISMATCH": " MISS", "ERROR": " err "}[verdict]
        print(f"  {flag} {pn:14s} {guid}  {detail[:90]}")
        if verdict == "MISMATCH":
            mismatches.append((pn, guid, detail))

    summary = (f"\nverify-summary: ok={counts['OK']} "
               f"mismatch={counts['MISMATCH']} error={counts['ERROR']} "
               f"sample={len(rows)} threshold={args.mismatch_threshold}")
    print(summary)

    if counts["MISMATCH"] >= args.mismatch_threshold:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(f"BSA GUID verification failed at "
                    f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n")
            f.write(f"Sample: {len(rows)}\n")
            f.write(f"Mismatches: {counts['MISMATCH']}\n")
            f.write(f"Errors:     {counts['ERROR']}\n\n")
            f.write("Mismatched permits (BSA's response did not contain the "
                    "expected permit number):\n")
            for pn, guid, detail in mismatches:
                f.write(f"  - {pn} -> {guid}\n      {detail}\n")
            f.write("\nLikely cause: BS&A re-issued record GUIDs (e.g. after "
                    "a system rebuild). Every deep-link in index.html based on "
                    "an old GUID now points at the wrong record. Run "
                    "`python update_permits.py --backfill-guids "
                    "--retry-guid-misses` to re-resolve.\n")
        print(f"FAIL: {counts['MISMATCH']} mismatch(es) "
              f">= threshold {args.mismatch_threshold} — wrote {REPORT_PATH}")
        return 1

    # Clean up any stale report from a previous failure
    if os.path.exists(REPORT_PATH):
        os.remove(REPORT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
