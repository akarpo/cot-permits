#!/usr/bin/env python3
"""
update_permits.py — the one script that maintains the Troy permits website.

Each run:
  1. Opens permits.db (SQLite). If it's missing, bootstraps it from the data
     already embedded in index.html — so the git repo is the portable source
     of truth and the .db is just a regenerable local cache.
  2. Incrementally scrapes apps.troymi.gov newest-first and stops once it has
     seen several consecutive pages of permits it already has — a routine run
     fetches only a handful of pages.
  3. INSERT OR IGNOREs new permits (keyed on Permit Number).
  4. Regenerates the self-contained index.html from the database.
  5. git add / commit / push  (so Cloudflare Pages redeploys).

Usage:
    python3 update_permits.py                 # normal incremental update
    python3 update_permits.py --no-git        # update files only, skip git
    python3 update_permits.py --full          # ignore stop-condition, scrape every page
    python3 update_permits.py --margin 8      # stop after N consecutive all-known pages

Dependencies: requests, beautifulsoup4, lxml  (all already installed).
"""
import argparse
import base64
import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "permits.db")
INDEX_PATH = os.path.join(HERE, "index.html")
RESULTS_URL = "https://apps.troymi.gov/PermitsIssued/Results"
CF_LIMIT = 25 * 1024 * 1024

# Column order as returned by the Troy endpoint == order used everywhere here.
COLUMNS = ["Permit Type", "Permit Number", "Date Issued", "Address", "Applicant",
           "Applicant Address", "Applicant City State Zip", "Parcel #", "Lot",
           "Subdivision", "Work Description", "Value"]
DB_COLS = ["permit_type", "permit_number", "date_issued", "address", "applicant",
           "applicant_address", "applicant_csz", "parcel", "lot", "subdivision",
           "work_description", "value"]
PERMIT_NO_IDX = 1

HEADERS = {  # the site 403s requests that don't look like a browser
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
}


# --------------------------------------------------------------- scraping ---
def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def fetch_page(session, page_number, max_retries=3):
    """Return the raw <table> HTML for one page of PermitType=All."""
    params = {"PageNumber": page_number, "PermitType": "All"}
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(RESULTS_URL, params=params, timeout=30)
            r.raise_for_status()
            payload = r.json()
            if not payload.get("success", False):
                raise RuntimeError(f"server reported failure: {payload.get('message')!r}")
            return payload["data"]["table"]
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as e:
            last = e
            wait = 2 * attempt
            print(f"  page {page_number}: attempt {attempt} failed ({e}); retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"page {page_number} failed after {max_retries} tries: {last}")


def parse_table(table_html):
    """Parse a table fragment -> (headers, list_of_12-col_rows)."""
    soup = BeautifulSoup(table_html, "lxml")
    table = soup.find("table")
    if table is None:
        return [], []
    headers, rows = [], []
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths:
            headers = [clean(th.get_text()) for th in ths]
            continue
        tds = tr.find_all("td")
        if tds:
            rows.append([clean(td.get_text()) for td in tds])
    return headers, rows


def total_pages(table_html):
    pager = BeautifulSoup(table_html, "lxml").find(id="Pagination")
    if pager is None:
        return 1
    nums = [int(n) for n in re.findall(r"\d+", pager.get_text(" "))]
    return max(nums) if nums else 1


# -------------------------------------------------------------- datastore ---
def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS permits (
            {DB_COLS[1]} TEXT PRIMARY KEY,
            {DB_COLS[0]} TEXT, {DB_COLS[2]} TEXT, {DB_COLS[3]} TEXT, {DB_COLS[4]} TEXT,
            {DB_COLS[5]} TEXT, {DB_COLS[6]} TEXT, {DB_COLS[7]} TEXT, {DB_COLS[8]} TEXT,
            {DB_COLS[9]} TEXT, {DB_COLS[10]} TEXT, {DB_COLS[11]} TEXT,
            first_seen TEXT,
            bsa_guid TEXT,
            bsa_resolved_at TEXT
        )""")
    # Idempotent migration: add columns if upgrading an older DB.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(permits)").fetchall()}
    if "bsa_guid" not in cols:
        conn.execute("ALTER TABLE permits ADD COLUMN bsa_guid TEXT")
    if "bsa_resolved_at" not in cols:
        conn.execute("ALTER TABLE permits ADD COLUMN bsa_resolved_at TEXT")
    conn.commit()
    return conn


def count_permits(conn):
    return conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]


def insert_rows(conn, rows):
    """INSERT OR IGNORE 12-col rows. Returns number actually inserted."""
    before = conn.total_changes
    now = datetime.now().isoformat(timespec="seconds")
    ordered = [DB_COLS[1], DB_COLS[0]] + DB_COLS[2:]    # permit_number first (PK)
    placeholders = ",".join("?" * (len(DB_COLS) + 1))
    sql = f"INSERT OR IGNORE INTO permits ({','.join(ordered)},first_seen) VALUES ({placeholders})"
    payload = []
    for r in rows:
        r = (list(r) + [""] * len(DB_COLS))[:len(DB_COLS)]
        payload.append([r[PERMIT_NO_IDX], r[0]] + r[2:] + [now])
    conn.executemany(sql, payload)
    conn.commit()
    return conn.total_changes - before


def bootstrap_from_index(conn):
    """Populate an empty DB from the gzipped data embedded in index.html.
    Also restores prior BSA GUID resolver state from the 13th column tristate
    (see generate_index docstring) so CI runs don't re-resolve every permit."""
    if not os.path.exists(INDEX_PATH):
        return 0
    html = open(INDEX_PATH, encoding="utf-8").read()
    m = re.search(r'let B64 = "([A-Za-z0-9+/=]+)"', html)
    if not m:
        return 0
    data = json.loads(gzip.decompress(base64.b64decode(m.group(1))))
    n = insert_rows(conn, data["rows"])
    print(f"  bootstrapped {n:,} permits from index.html")

    # Replay the resolver-state tristate from the 13th column, if present.
    restored_guids = restored_misses = 0
    updates = []
    for r in data["rows"]:
        if len(r) < 13:
            continue
        marker = r[12]
        pn = r[PERMIT_NO_IDX]
        if not marker:
            continue                               # never tried — leave NULL
        if marker == "-":
            updates.append((None, "bootstrap", pn))
            restored_misses += 1
        elif len(marker) == 36 and "-" in marker:  # cheap GUID shape check
            updates.append((marker, "bootstrap", pn))
            restored_guids += 1
    if updates:
        conn.executemany(
            "UPDATE permits SET bsa_guid=?, bsa_resolved_at=? WHERE permit_number=?",
            updates,
        )
        conn.commit()
        print(f"  restored BSA state: {restored_guids:,} GUIDs, "
              f"{restored_misses:,} confirmed-misses")
    return n


# ------------------------------------------------------- incremental scrape ---
def update_from_troy(conn, session, margin, full):
    """Scrape newest-first; stop after `margin` consecutive all-known pages."""
    first_html = fetch_page(session, 1)
    last_page = total_pages(first_html)
    print(f"  endpoint reports {last_page:,} pages")

    new_total, consecutive_known, page = 0, 0, 1
    while page <= last_page:
        html = first_html if page == 1 else fetch_page(session, page)
        _, rows = parse_table(html)
        if not rows:
            break
        added = insert_rows(conn, rows)
        new_total += added
        if added == 0:
            consecutive_known += 1
        else:
            consecutive_known = 0
        if page % 25 == 0 or added or page <= 3:
            print(f"  page {page}/{last_page}: +{added} new "
                  f"({new_total} this run, {consecutive_known} known pages in a row)")
        if not full and consecutive_known >= margin:
            print(f"  {margin} consecutive all-known pages — stopping (caught up)")
            break
        page += 1
        time.sleep(0.25)
    return new_total


# --------------------------------------- BSA Online (bsaonline.com) lookup ---
# Resolves Troy permit numbers to BSA Online record GUIDs by driving their
# advanced-record-search wizard end-to-end. The wizard isn't a documented API,
# so this is best-effort: it may break if BSA changes their JS. The daily
# verify_bsa_guids.py spot-check guards against silent drift.
BSA_BASE        = "https://bsaonline.com"
BSA_UID         = 406  # City of Troy
BSA_SEARCH_URL  = f"{BSA_BASE}/SiteSearch/BuildingDepartmentRecordSearch?uid={BSA_UID}"
BSA_RESULTS_URL = f"{BSA_BASE}/SiteSearch/GetPageOfFindRecordSearchResultsPartialView"
BSA_DETAIL_URL  = f"{BSA_BASE}/CD_RecordDetails/Permit"  # ?permitId=<GUID>&uid=406

# Bootstrap uses normal browser headers (the wizard JS only sends AJAX-style
# headers *after* the page loads); subsequent search calls add the AJAX flag.
BSA_BOOTSTRAP_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
BSA_AJAX_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BSA_SEARCH_URL,
}
_BSA_GUID_RE = re.compile(
    r"gotToRecord\(\s*'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'",
    re.I,
)
_BSA_SESSION_RE = re.compile(r'AdvancedRecordSearchSessionGuid[^v]*value="([^"]+)"')

# Ordering for picking which unresolved permits to look up next. Newly-scraped
# rows win on first_seen DESC. Within the bootstrap cohort (all share one
# first_seen), we sort by date_issued — parsing the "MMM DD, YYYY" string in
# SQL so we don't have to add a column. permit_number DESC is a final tiebreaker.
BSA_RESOLVE_ORDER_BY = """
ORDER BY
  first_seen DESC,
  substr(date_issued, -4) DESC,
  CASE substr(date_issued, 1, 3)
    WHEN 'Jan' THEN 1 WHEN 'Feb' THEN 2 WHEN 'Mar' THEN 3
    WHEN 'Apr' THEN 4 WHEN 'May' THEN 5 WHEN 'Jun' THEN 6
    WHEN 'Jul' THEN 7 WHEN 'Aug' THEN 8 WHEN 'Sep' THEN 9
    WHEN 'Oct' THEN 10 WHEN 'Nov' THEN 11 WHEN 'Dec' THEN 12 END DESC,
  CAST(replace(substr(date_issued, 5, 3), ',', '') AS INTEGER) DESC,
  permit_number DESC
"""


class BsaResolver:
    """Resolves Troy permit numbers to BSA record GUIDs. One bootstrap per
    session — the AdvancedRecordSearchSessionGuid is reusable for many lookups.
    Re-bootstraps automatically on transient failure or session expiry."""

    def __init__(self, throttle=0.6):
        self.session = requests.Session()
        self.sg = None
        self.throttle = throttle
        self._last_call = 0.0
        self.lookups = 0
        self.resolved = 0

    def _bootstrap(self):
        r = self.session.get(BSA_SEARCH_URL, headers=BSA_BOOTSTRAP_HEADERS, timeout=20)
        r.raise_for_status()
        m = _BSA_SESSION_RE.search(r.text)
        if not m:
            raise RuntimeError("BSA: could not extract AdvancedRecordSearchSessionGuid")
        self.sg = m.group(1)
        # Subsequent calls are XHRs; set defaults on the session.
        self.session.headers.update(BSA_AJAX_HEADERS)

    def _sleep(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.throttle:
            time.sleep(self.throttle - elapsed)
        self._last_call = time.time()

    def resolve(self, permit_number, _retry=True):
        """Return the BSA record GUID for `permit_number`, or None if no match."""
        # Throttle BEFORE bootstrap too: if BSA is degraded, the bootstrap
        # itself may raise on every call, and without this we'd spin the
        # bootstrap URL with zero throttle and worsen the upstream's day.
        self._sleep()
        if not self.sg:
            self._bootstrap()
        self.lookups += 1
        try:
            r = self.session.get(BSA_RESULTS_URL, params={
                "advancedRecordSearchSessionGuid": self.sg,
                "currentPage": 1,
                "searchText": permit_number,
                "bsaOnlineSiteSearchType": 6,  # Record Number tab
            }, timeout=25)
            r.raise_for_status()
        except requests.RequestException:
            if _retry:
                self.sg = None
                return self.resolve(permit_number, _retry=False)
            return None
        m = _BSA_GUID_RE.search(r.text)
        if m:
            self.resolved += 1
            return m.group(1)
        # No GUID — either permit isn't in BSA or our session lapsed.
        if _retry and len(r.text) < 400:
            self.sg = None
            return self.resolve(permit_number, _retry=False)
        return None


def resolve_missing_guids(conn, resolver, limit=None, batch=50, retry_misses=False):
    """Find permits we haven't tried BSA-resolving yet, resolve them, persist
    back to DB. Returns (attempted, newly_resolved). Commits every `batch`
    lookups so a long backfill survives Ctrl-C / runner timeout.

    Skips permits we've already attempted (bsa_resolved_at IS NOT NULL),
    even if the prior attempt didn't find a match — use retry_misses=True
    to retry the no-match ones."""
    cond = "bsa_resolved_at IS NULL" if not retry_misses else "bsa_guid IS NULL"
    q = f"SELECT permit_number FROM permits WHERE {cond} {BSA_RESOLVE_ORDER_BY}"
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    pending = [row[0] for row in conn.execute(q)]
    if not pending:
        print("  no permits need BSA GUID resolution")
        return 0, 0
    print(f"  resolving BSA GUIDs for {len(pending):,} permit(s)...")
    now = datetime.now().isoformat(timespec="seconds")
    found = attempted = 0
    pending_writes = []
    for pn in pending:
        try:
            guid = resolver.resolve(pn)
        except Exception as e:                          # noqa: BLE001 — keep going
            print(f"    resolve {pn} failed: {e}", file=sys.stderr)
            attempted += 1
            continue   # HTTP error: leave bsa_resolved_at NULL so we retry.
        attempted += 1
        if guid:
            found += 1
            pending_writes.append((guid, now, pn))
        else:
            pending_writes.append((None, now, pn))      # genuine "not in BSA"
        if len(pending_writes) >= batch:
            conn.executemany(
                "UPDATE permits SET bsa_guid=?, bsa_resolved_at=? WHERE permit_number=?",
                pending_writes,
            )
            conn.commit()
            pending_writes.clear()
            print(f"    progress: {attempted:,}/{len(pending):,} attempted, {found:,} resolved")
    if pending_writes:
        conn.executemany(
            "UPDATE permits SET bsa_guid=?, bsa_resolved_at=? WHERE permit_number=?",
            pending_writes,
        )
        conn.commit()
    print(f"  BSA resolver: {found:,}/{attempted:,} resolved "
          f"(total session lookups={resolver.lookups})")
    return attempted, found


# ------------------------------------------------------- index.html output ---
def generate_index(conn):
    # explicit ORDER BY: a bare SELECT's row order is unspecified in SQLite, so
    # ordering by the primary key keeps index.html byte-deterministic run to run.
    # bsa_guid is appended as a 13th column; HEADERS stays at 12, so it
    # silently powers the deep-link without showing up in any visible row.
    # 13th column is a tristate so bootstrap_from_index can preserve resolver
    # state across CI runs (permits.db is gitignored, so index.html *is* the
    # cross-run store): a full GUID = resolved, '-' = tried but not in BSA,
    # '' = never tried.
    rows = [list(r) for r in conn.execute(
        f"SELECT {DB_COLS[0]},{DB_COLS[1]},{DB_COLS[2]},{DB_COLS[3]},{DB_COLS[4]},"
        f"{DB_COLS[5]},{DB_COLS[6]},{DB_COLS[7]},{DB_COLS[8]},{DB_COLS[9]},"
        f"{DB_COLS[10]},{DB_COLS[11]},"
        f"COALESCE(bsa_guid, CASE WHEN bsa_resolved_at IS NULL THEN '' ELSE '-' END) "
        f"FROM permits ORDER BY {DB_COLS[1]}")]
    payload = json.dumps({"headers": COLUMNS, "rows": rows}, separators=(",", ":")).encode()
    b64 = base64.b64encode(gzip.compress(payload, 9)).decode()
    assert json.loads(gzip.decompress(base64.b64decode(b64)))["rows"][:1] == rows[:1]
    html = INDEX_TEMPLATE.replace("__DATA__", b64).replace("__COUNT__", f"{len(rows):,}")
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(INDEX_PATH)
    flag = "OK" if size <= CF_LIMIT else "!! OVER CLOUDFLARE 25 MiB CAP"
    print(f"  wrote index.html ({size/1e6:.1f} MB, {flag})")
    return len(rows), size


# -------------------------------------------------------------------- git ---
def git(*args, check=True):
    return subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True,
                          check=check)


def git_sync(n_new, total):
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        print("  not a git repo yet — skipping git (see README for one-time setup)")
        return
    git("add", "-A")
    if not git("status", "--porcelain", check=False).stdout.strip():
        print("  nothing changed — no commit")
        return
    msg = (f"Update permits: +{n_new} new ({total:,} total) "
           f"— {datetime.now():%Y-%m-%d}")
    git("commit", "-m", msg)
    print(f"  committed: {msg}")
    push = git("push", check=False)
    if push.returncode == 0:
        print("  pushed — Cloudflare Pages will redeploy")
    else:
        print(f"  push failed ({push.stderr.strip().splitlines()[-1] if push.stderr.strip() else '?'})")
        print("  (set up the GitHub remote — see README — then `git -C cot-permits push`)")


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-git", action="store_true", help="update files only, skip git")
    ap.add_argument("--full", action="store_true", help="scrape every page (ignore stop condition)")
    ap.add_argument("--margin", type=int, default=4,
                    help="stop after N consecutive all-known pages (default 4)")
    ap.add_argument("--no-guids", action="store_true",
                    help="skip BSA Online GUID resolution this run")
    ap.add_argument("--backfill-guids", action="store_true",
                    help="resolve BSA GUIDs for every permit missing one (long-running)")
    ap.add_argument("--guid-limit", type=int, default=500,
                    help="cap BSA GUID lookups per run (default 500; 0 = unlimited)")
    ap.add_argument("--retry-guid-misses", action="store_true",
                    help="re-attempt permits where a prior BSA lookup found no match")
    args = ap.parse_args()

    print("opening datastore...")
    conn = open_db()
    have = count_permits(conn)
    if have == 0:
        print("  permits.db empty — bootstrapping...")
        bootstrap_from_index(conn)
        have = count_permits(conn)
    print(f"  {have:,} permits in datastore")

    print("scraping Troy for new permits...")
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        n_new = update_from_troy(conn, session, args.margin, args.full or have == 0)
    except Exception as e:                         # noqa: BLE001 - keep partial progress
        print(f"  scrape error: {e} — proceeding with what was collected", file=sys.stderr)
        n_new = count_permits(conn) - have
    total = count_permits(conn)
    print(f"  +{n_new} new permits — {total:,} total")

    if not args.no_guids:
        print("resolving BSA Online GUIDs...")
        resolver = BsaResolver(throttle=0.6)
        limit = None if (args.backfill_guids or args.guid_limit == 0) else args.guid_limit
        try:
            resolve_missing_guids(conn, resolver, limit=limit,
                                  retry_misses=args.retry_guid_misses)
        except Exception as e:                          # noqa: BLE001
            print(f"  GUID resolver aborted: {e} — continuing", file=sys.stderr)
    else:
        print("--no-guids: skipping BSA GUID resolution")

    print("regenerating index.html...")
    generate_index(conn)
    conn.close()

    if args.no_git:
        print("--no-git: skipping commit/push")
    else:
        print("publishing...")
        git_sync(n_new, total)
    print("done.")


INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>City of Troy &mdash; Permits Issued &mdash; Searchable Index</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--line:#e4e7eb;--ink:#1a1d21;--muted:#6b7280;--accent:#1f6feb;}
  *{box-sizing:border-box;}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);}
  header{background:var(--card);border-bottom:1px solid var(--line);padding:13px 20px;position:sticky;top:0;z-index:20;}
  h1{margin:0;font-size:16px;}
  .sub{color:var(--muted);font-size:12px;margin-top:1px;}
  .controls{display:flex;gap:10px;margin-top:11px;flex-wrap:wrap;align-items:center;}
  .step{display:flex;align-items:center;gap:6px;}
  .step b{font-size:11px;color:var(--accent);background:#eaf1fe;border-radius:50%;width:17px;height:17px;display:inline-flex;align-items:center;justify-content:center;}
  .dash{color:var(--muted);font-size:12px;}
  select,#q,#hl{padding:8px 10px;border:1px solid var(--line);border-radius:7px;font-size:13px;background:#fff;}
  select:disabled,#q:disabled,#hl:disabled{background:#f1f2f4;color:#aab;cursor:not-allowed;}
  select{max-width:280px;}
  #q{flex:1;min-width:180px;font-size:14px;}
  #hl{flex:1;min-width:160px;font-size:14px;border-color:#f0c200;background:#fffdf3;}
  #q:focus,#hl:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px;}
  #count{color:var(--muted);font-size:12px;white-space:nowrap;}
  main{padding:14px 20px 60px;}
  /* no overflow:hidden here — it would make the table a scroll container and
     break position:sticky on thead (the header would anchor to the table). */
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;}
  /* sticky on <thead>, not <th>: sticky on individual header cells does not
     engage here; on the row group it pins reliably below the page header. */
  thead{position:sticky;top:var(--header-h,110px);z-index:5;}
  thead th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line);background:#fafbfc;cursor:pointer;user-select:none;}
  thead th:hover{color:var(--ink);}
  td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
  td.hlcell{background:#fff3bf;}
  tbody tr.row{cursor:pointer;}
  tbody tr.row:hover td{background:#f0f6ff;}
  tbody tr.row:hover td.hlcell{background:#ffe8a3;}
  .desc{max-width:440px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .nowrap{white-space:nowrap;}
  .detail td{background:#fbfcfd;padding:10px 14px;}
  .detail dl{margin:0;display:grid;grid-template-columns:180px 1fr;gap:3px 16px;}
  .detail dt{color:var(--muted);font-size:12px;}
  .detail dd{margin:0;white-space:pre-wrap;}
  mark{background:#cfe3ff;padding:0 1px;border-radius:2px;}
  mark.hl{background:#ffd43b;padding:0 1px;border-radius:2px;}
  .note{padding:44px 20px;text-align:center;color:var(--muted);}
  .note b{color:var(--ink);}
  /* multi-select permit type dropdown */
  details.multi{position:relative;}
  details.multi > summary{
    list-style:none;cursor:pointer;display:inline-block;min-width:200px;max-width:340px;
    padding:8px 26px 8px 10px;border:1px solid var(--line);border-radius:7px;background:#fff;
    font-size:13px;position:relative;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  details.multi > summary::-webkit-details-marker{display:none;}
  details.multi > summary::after{
    content:"\25BE";position:absolute;right:9px;top:50%;transform:translateY(-50%);color:var(--muted);
  }
  details.multi[open] > summary{outline:2px solid var(--accent);outline-offset:-1px;}
  details.multi.loading > summary{background:#f1f2f4;color:#aab;cursor:not-allowed;pointer-events:none;}
  details.multi .panel{
    position:absolute;top:calc(100% + 4px);left:0;z-index:30;background:#fff;
    border:1px solid var(--line);border-radius:7px;padding:6px;
    box-shadow:0 6px 18px rgba(0,0,0,.08);max-height:420px;overflow:auto;min-width:280px;
  }
  details.multi label{display:flex;align-items:center;gap:6px;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:13px;}
  details.multi label:hover{background:#f0f6ff;}
  details.multi label input{margin:0;}
  details.multi label.all{border-bottom:1px solid var(--line);margin-bottom:4px;padding-bottom:6px;font-weight:600;}
  /* permit-number -> BSA Online lookup link */
  td a.bsa{color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent);}
  td a.bsa:hover{background:#eaf1fe;}
</style>
</head>
<body>
<header>
  <h1>City of Troy &mdash; Permits Issued</h1>
  <div class="sub">__COUNT__ permits &middot; scraped from apps.troymi.gov &middot; works fully offline</div>
  <div class="controls">
    <span class="step"><b>1</b>
      <details class="multi loading" id="typewrap">
        <summary id="typesummary">Loading permits&hellip;</summary>
        <div class="panel">
          <label class="all"><input type="checkbox" id="typeall"><span>All types</span></label>
          <div id="typeopts"></div>
        </div>
      </details>
    </span>
    <span class="step"><b>2</b>
      <select id="from" disabled><option value="">From year&hellip;</option></select>
      <span class="dash">to</span>
      <select id="to" disabled><option value="">To year&hellip;</option></select>
    </span>
    <input id="q" placeholder="search within &mdash; filters rows (space = AND)" autocomplete="off" spellcheck="false" disabled>
    <input id="hl" placeholder="highlight a word &mdash; keeps all rows (e.g. fiber)" autocomplete="off" spellcheck="false" disabled>
    <span id="count"></span>
  </div>
</header>
<main>
  <div id="note" class="note">Decompressing &amp; indexing __COUNT__ permits&hellip; (one-time, a few seconds)</div>
  <table id="tbl" hidden><thead id="thead"></thead><tbody id="tbody"></tbody></table>
</main>
<script>
let B64 = "__DATA__";
const LIMIT = 1000;
const BSA_URL    = "https://bsaonline.com/SiteSearch/BuildingDepartmentRecordSearch?uid=406";
const BSA_DETAIL = "https://bsaonline.com/CD_RecordDetails/Permit?uid=406&permitId=";
let HEADERS = [], ROWS = [], view = [];
let sortCol = 2, sortDir = -1;          // default: Date Issued, descending
const SHOW = [0, 1, 2, 3, 4, 10];       // columns shown in the table
const NODATE = " nodate";
let ALL_TYPES = [];                      // every distinct permit type, sorted
let selectedTypes = new Set();           // currently checked types

const $ = id => document.getElementById(id);
function esc(s){ return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// Pin the sticky table header exactly below the (variable-height) page header.
function fitHeader(){
  document.documentElement.style.setProperty(
    "--header-h", document.querySelector("header").offsetHeight + "px");
}

// Render `raw` as safe HTML, wrapping any of `marks` ({t:lowercased term, cls}).
function markup(raw, marks){
  const low = raw.toLowerCase();
  let ranges = [];
  for(const m of marks){
    if(!m.t) continue;
    let i = 0;
    while((i = low.indexOf(m.t, i)) !== -1){ ranges.push([i, i + m.t.length, m.cls]); i += m.t.length; }
  }
  if(!ranges.length) return esc(raw);
  ranges.sort((a,b) => a[0]-b[0] || b[1]-a[1]);
  let out = "", pos = 0;
  for(const [s,e,cls] of ranges){
    if(s < pos) continue;                                  // skip overlaps
    out += esc(raw.slice(pos,s)) + `<mark class="${cls}">` + esc(raw.slice(s,e)) + "</mark>";
    pos = e;
  }
  return out + esc(raw.slice(pos));
}

async function boot(){
  const bin = Uint8Array.from(atob(B64), c => c.charCodeAt(0));
  B64 = null;
  const stream = new Blob([bin]).stream().pipeThrough(new DecompressionStream("gzip"));
  const data = JSON.parse(await new Response(stream).text());
  HEADERS = data.headers;
  ROWS = data.rows;
  for(const r of ROWS){
    r._s = r.join(" ").toLowerCase();
    r._d = Date.parse(r[2]) || 0;
    r._y = r._d ? String(new Date(r._d).getUTCFullYear()) : NODATE;
  }
  ALL_TYPES = [...new Set(ROWS.map(r => r[0]))].filter(Boolean)
                  .sort((a,b) => a.localeCompare(b));
  $("typeopts").innerHTML = ALL_TYPES.map(t =>
    `<label><input type="checkbox" class="typechk" value="${esc(t)}"><span>${esc(t)}</span></label>`).join("");
  $("thead").innerHTML = "<tr>" + SHOW.map(i => `<th data-c="${i}">${esc(HEADERS[i])}</th>`).join("") + "</tr>";
  $("typewrap").classList.remove("loading");
  $("typesummary").textContent = "Permit type…";
  fitHeader();
  bind();
  prompt();
}

function bind(){
  $("typeall").addEventListener("change", onTypeAll);
  $("typeopts").addEventListener("change", onTypeCheck);
  // Close the dropdown when clicking outside it.
  document.addEventListener("click", e => {
    const w = $("typewrap");
    if(w.open && !w.contains(e.target)) w.open = false;
  });
  $("from").addEventListener("change", run);
  $("to").addEventListener("change", run);
  window.addEventListener("resize", fitHeader);
  let t1, t2;
  $("q").addEventListener("input", () => { clearTimeout(t1); t1 = setTimeout(run, 140); });
  $("hl").addEventListener("input", () => { clearTimeout(t2); t2 = setTimeout(rerender, 120); });
  $("thead").addEventListener("click", e => {
    const th = e.target.closest("th"); if(!th) return;
    const c = +th.dataset.c;
    sortDir = (c === sortCol) ? -sortDir : (c === 2 ? -1 : 1);
    sortCol = c;
    if(selectedTypes.size > 0 && $("from").value && $("to").value) run();
  });
  $("tbody").addEventListener("click", e => {
    // BSA lookup link: when we have no GUID (search-wizard fallback), copy
    // the permit number to clipboard so the user can paste it on the BSA page.
    // When we have a GUID, the link goes straight to the record — no copy needed.
    const a = e.target.closest("a.bsa");
    if(a){
      if(a.dataset.pn && navigator.clipboard){
        navigator.clipboard.writeText(a.dataset.pn).catch(() => {});
      }
      return;
    }
    const tr = e.target.closest("tr.row"); if(!tr) return;
    const nxt = tr.nextElementSibling;
    if(nxt && nxt.classList.contains("detail")){ nxt.remove(); return; }
    const r = ROWS[+tr.dataset.i];
    const dl = HEADERS.map((h,i) => `<dt>${esc(h)}</dt><dd>${esc(r[i]) || "&mdash;"}</dd>`).join("");
    tr.insertAdjacentHTML("afterend", `<tr class="detail"><td colspan="${SHOW.length}"><dl>${dl}</dl></td></tr>`);
  });
}

function syncSelectedTypes(){
  selectedTypes = new Set();
  for(const cb of document.querySelectorAll(".typechk")) if(cb.checked) selectedTypes.add(cb.value);
}

function onTypeAll(){
  const checked = $("typeall").checked;
  for(const cb of document.querySelectorAll(".typechk")) cb.checked = checked;
  $("typeall").indeterminate = false;
  syncSelectedTypes();
  onTypeUpdate();
}

function onTypeCheck(e){
  if(!e.target.matches(".typechk")) return;
  syncSelectedTypes();
  const all = $("typeall");
  all.checked = selectedTypes.size === ALL_TYPES.length;
  all.indeterminate = selectedTypes.size > 0 && selectedTypes.size < ALL_TYPES.length;
  onTypeUpdate();
}

function typeLabel(){
  const n = selectedTypes.size, total = ALL_TYPES.length;
  if(n === 0) return "";
  if(n === total) return "All types";
  if(n === 1) return [...selectedTypes][0];
  return `${n} types`;
}

function onTypeUpdate(){
  // Refresh the summary text in the dropdown button.
  $("typesummary").textContent = selectedTypes.size === 0 ? "Permit type…" : typeLabel();

  const from = $("from"), to = $("to");
  if(selectedTypes.size === 0){
    from.innerHTML = '<option value="">From year…</option>';
    to.innerHTML = '<option value="">To year…</option>';
    from.disabled = to.disabled = $("q").disabled = $("hl").disabled = true;
    prompt(); return;
  }
  // Year list = union of years across all selected types (undated rows excluded).
  const prevFrom = from.value, prevTo = to.value;
  const years = [...new Set(ROWS.filter(r => selectedTypes.has(r[0]) && r._y !== NODATE)
                              .map(r => r._y))].sort().reverse();
  const opts = years.map(y => `<option value="${y}">${y}</option>`).join("");
  from.innerHTML = '<option value="">From year…</option>' + opts;
  to.innerHTML = '<option value="">To year…</option>' + opts;
  from.disabled = to.disabled = false;
  // Preserve any year selection that's still valid for the new type set.
  if(years.includes(prevFrom)) from.value = prevFrom;
  if(years.includes(prevTo))   to.value   = prevTo;
  if(from.value && to.value) run(); else prompt();
}

function prompt(){
  const fromV = $("from").value, toV = $("to").value;
  $("tbl").hidden = true;
  if(selectedTypes.size === 0){
    $("note").hidden = false;
    $("note").innerHTML = "Step <b>1</b>: choose one or more permit types above (or pick <b>All types</b>).";
    $("count").textContent = "";
  } else if(!fromV || !toV){
    $("note").hidden = false;
    const n = selectedTypes.size, total = ALL_TYPES.length;
    let what;
    if(n === total)     what = "<b>All types</b>";
    else if(n === 1)    what = `Permit type <b>${esc([...selectedTypes][0])}</b>`;
    else                what = `<b>${n} permit types</b>`;
    $("note").innerHTML = `${what} selected. Step <b>2</b>: choose a from-year and a to-year.`;
    $("count").textContent = "";
  }
}

function run(){
  const fromV = $("from").value, toV = $("to").value;
  if(selectedTypes.size === 0 || !fromV || !toV){ prompt(); return; }
  const lo = fromV <= toV ? fromV : toV;          // forgiving if picked out of order
  const hi = fromV <= toV ? toV : fromV;
  $("q").disabled = false; $("hl").disabled = false;
  const terms = $("q").value.toLowerCase().split(/\s+/).filter(Boolean);
  view = [];
  for(let i=0;i<ROWS.length;i++){
    const r = ROWS[i];
    if(!selectedTypes.has(r[0]) || r._y < lo || r._y > hi) continue;   // year range (string cmp; undated excluded)
    let ok = true;
    for(const t of terms){ if(!r._s.includes(t)){ ok = false; break; } }
    if(ok){ r._i = i; view.push(r); }
  }
  view.sort((a,b) => {
    const av = sortCol === 2 ? a._d : a[sortCol].toLowerCase();
    const bv = sortCol === 2 ? b._d : b[sortCol].toLowerCase();
    return av < bv ? -sortDir : av > bv ? sortDir : 0;
  });
  rerender(typeLabel(), lo === hi ? lo : lo + "–" + hi);
}

function rerender(type, span){
  type = typeof type === "string" ? type : typeLabel();
  if(typeof span !== "string"){                        // called from the highlight box
    const f = $("from").value, t = $("to").value;
    if(selectedTypes.size === 0 || !f || !t) return;
    const lo = f <= t ? f : t, hi = f <= t ? t : f;
    span = lo === hi ? lo : lo + "–" + hi;
  }
  const terms = $("q").value.toLowerCase().split(/\s+/).filter(Boolean);
  const hl = $("hl").value.toLowerCase().trim();
  const marks = terms.map(t => ({t, cls: ""}));
  if(hl) marks.push({t: hl, cls: "hl"});

  const n = view.length;
  let label = (n > LIMIT ? `${n.toLocaleString()} permits — showing first ${LIMIT}`
                         : `${n.toLocaleString()} permit${n===1?"":"s"}`) + ` · ${type} · ${span}`;
  if(hl){
    const hits = view.filter(r => r._s.includes(hl)).length;
    label += ` · ${hits.toLocaleString()} contain “${hl}”`;
  }
  $("count").textContent = label;
  fitHeader();                 // #count text can re-wrap the controls -> header height changes
  $("note").hidden = true;
  $("tbl").hidden = false;
  if(!n){ $("tbody").innerHTML = `<tr><td colspan="${SHOW.length}" class="note">no permits match</td></tr>`; return; }

  const html = view.slice(0, LIMIT).map(r => {
    const cells = SHOW.map(c => {
      const raw = r[c];
      let cls = c === 10 ? "desc" : (c === 2 ? "nowrap" : "");
      if(hl && raw.toLowerCase().includes(hl)) cls += " hlcell";
      const inner = markup(raw, marks);
      if(c === 1 && raw){
        // r[12] is the BSA tristate: full GUID -> direct deep-link; '-' or ''
        // -> fallback to the search-wizard URL + clipboard-copy helper.
        const guid = r[12];
        const direct = guid && guid.length === 36 && guid !== "-";
        const href = direct ? `${BSA_DETAIL}${encodeURIComponent(guid)}`
                            : `${BSA_URL}#${encodeURIComponent(raw)}`;
        const title = direct ? `Open BSA Online record for ${raw}`
                             : `Open BSA Online search (permit number copied to clipboard)`;
        const dataPn = direct ? "" : ` data-pn="${esc(raw)}"`;
        return `<td class="${cls}"><a class="bsa" href="${href}" target="_blank" rel="noopener noreferrer"${dataPn} title="${title}">${inner}</a></td>`;
      }
      return `<td class="${cls}">${inner}</td>`;
    }).join("");
    return `<tr class="row" data-i="${r._i}">${cells}</tr>`;
  });
  $("tbody").innerHTML = html.join("");
}

boot().catch(e => { $("note").textContent = "Failed to load: " + e; });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
