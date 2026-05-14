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
            first_seen TEXT
        )""")
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
    """Populate an empty DB from the gzipped data embedded in index.html."""
    if not os.path.exists(INDEX_PATH):
        return 0
    html = open(INDEX_PATH, encoding="utf-8").read()
    m = re.search(r'let B64 = "([A-Za-z0-9+/=]+)"', html)
    if not m:
        return 0
    data = json.loads(gzip.decompress(base64.b64decode(m.group(1))))
    n = insert_rows(conn, data["rows"])
    print(f"  bootstrapped {n:,} permits from index.html")
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


# ------------------------------------------------------- index.html output ---
def generate_index(conn):
    # explicit ORDER BY: a bare SELECT's row order is unspecified in SQLite, so
    # ordering by the primary key keeps index.html byte-deterministic run to run.
    rows = [list(r) for r in conn.execute(
        f"SELECT {DB_COLS[0]},{DB_COLS[1]},{DB_COLS[2]},{DB_COLS[3]},{DB_COLS[4]},"
        f"{DB_COLS[5]},{DB_COLS[6]},{DB_COLS[7]},{DB_COLS[8]},{DB_COLS[9]},"
        f"{DB_COLS[10]},{DB_COLS[11]} FROM permits ORDER BY {DB_COLS[1]}")]
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
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
  thead th{position:sticky;top:var(--header-h,110px);z-index:5;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line);background:#fafbfc;cursor:pointer;user-select:none;}
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
</style>
</head>
<body>
<header>
  <h1>City of Troy &mdash; Permits Issued</h1>
  <div class="sub">__COUNT__ permits &middot; scraped from apps.troymi.gov &middot; works fully offline</div>
  <div class="controls">
    <span class="step"><b>1</b>
      <select id="type" disabled><option value="">Permit type&hellip;</option></select>
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
let HEADERS = [], ROWS = [], view = [];
let sortCol = 2, sortDir = -1;          // default: Date Issued, descending
const SHOW = [0, 1, 2, 3, 4, 10];       // columns shown in the table
const NODATE = " nodate";

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
  const types = [...new Set(ROWS.map(r => r[0]))].filter(Boolean)
                  .sort((a,b) => a.localeCompare(b));
  $("type").innerHTML = '<option value="">Permit type…</option>' +
    types.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  $("thead").innerHTML = "<tr>" + SHOW.map(i => `<th data-c="${i}">${esc(HEADERS[i])}</th>`).join("") + "</tr>";
  $("type").disabled = false;
  fitHeader();
  bind();
  prompt();
  $("type").focus();
}

function bind(){
  $("type").addEventListener("change", onType);
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
    if($("type").value && $("from").value && $("to").value) run();
  });
  $("tbody").addEventListener("click", e => {
    const tr = e.target.closest("tr.row"); if(!tr) return;
    const nxt = tr.nextElementSibling;
    if(nxt && nxt.classList.contains("detail")){ nxt.remove(); return; }
    const r = ROWS[+tr.dataset.i];
    const dl = HEADERS.map((h,i) => `<dt>${esc(h)}</dt><dd>${esc(r[i]) || "&mdash;"}</dd>`).join("");
    tr.insertAdjacentHTML("afterend", `<tr class="detail"><td colspan="${SHOW.length}"><dl>${dl}</dl></td></tr>`);
  });
}

function onType(){
  const type = $("type").value, from = $("from"), to = $("to");
  $("q").value = ""; $("hl").value = "";
  if(!type){
    from.innerHTML = '<option value="">From year…</option>';
    to.innerHTML = '<option value="">To year…</option>';
    from.disabled = to.disabled = $("q").disabled = $("hl").disabled = true;
    prompt(); return;
  }
  // years available for this permit type, newest first (undated rows excluded)
  const years = [...new Set(ROWS.filter(r => r[0] === type && r._y !== NODATE)
                              .map(r => r._y))].sort().reverse();
  const opts = years.map(y => `<option value="${y}">${y}</option>`).join("");
  from.innerHTML = '<option value="">From year…</option>' + opts;
  to.innerHTML = '<option value="">To year…</option>' + opts;
  from.disabled = to.disabled = false;
  prompt();
}

function prompt(){
  const type = $("type").value, from = $("from").value, to = $("to").value;
  $("tbl").hidden = true;
  if(!type){
    $("note").hidden = false;
    $("note").innerHTML = "Step <b>1</b>: choose a permit type above.";
    $("count").textContent = "";
  } else if(!from || !to){
    $("note").hidden = false;
    $("note").innerHTML = `Permit type <b>${esc(type)}</b> selected. Step <b>2</b>: choose a from-year and a to-year.`;
    $("count").textContent = "";
  }
}

function run(){
  const type = $("type").value, fromV = $("from").value, toV = $("to").value;
  if(!type || !fromV || !toV){ prompt(); return; }
  const lo = fromV <= toV ? fromV : toV;          // forgiving if picked out of order
  const hi = fromV <= toV ? toV : fromV;
  $("q").disabled = false; $("hl").disabled = false;
  const terms = $("q").value.toLowerCase().split(/\s+/).filter(Boolean);
  view = [];
  for(let i=0;i<ROWS.length;i++){
    const r = ROWS[i];
    if(r[0] !== type || r._y < lo || r._y > hi) continue;   // year range (string cmp; undated excluded)
    let ok = true;
    for(const t of terms){ if(!r._s.includes(t)){ ok = false; break; } }
    if(ok){ r._i = i; view.push(r); }
  }
  view.sort((a,b) => {
    const av = sortCol === 2 ? a._d : a[sortCol].toLowerCase();
    const bv = sortCol === 2 ? b._d : b[sortCol].toLowerCase();
    return av < bv ? -sortDir : av > bv ? sortDir : 0;
  });
  rerender(type, lo === hi ? lo : lo + "–" + hi);
}

function rerender(type, span){
  type = typeof type === "string" ? type : $("type").value;
  if(typeof span !== "string"){                        // called from the highlight box
    const f = $("from").value, t = $("to").value;
    if(!type || !f || !t) return;
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
  $("note").hidden = true;
  $("tbl").hidden = false;
  if(!n){ $("tbody").innerHTML = `<tr><td colspan="${SHOW.length}" class="note">no permits match</td></tr>`; return; }

  const html = view.slice(0, LIMIT).map(r => {
    const cells = SHOW.map(c => {
      const raw = r[c];
      let cls = c === 10 ? "desc" : (c === 2 ? "nowrap" : "");
      if(hl && raw.toLowerCase().includes(hl)) cls += " hlcell";
      return `<td class="${cls}">${markup(raw, marks)}</td>`;
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
