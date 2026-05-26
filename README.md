# cot-permits — City of Troy permits, searchable & self-publishing

A searchable index of **every permit in the City of Troy's "Permits
Issued" database** (apps.troymi.gov), with deep-links to BSA Online
permit records and a one-command pipeline that keeps it all current.

Live at **<https://cot-permits.karpowitsch.org>**.

## Updating — one command

```bash
python3 update_permits.py
```

Each run:
1. Opens `permits.db` (local SQLite cache). If it's missing, **bootstraps
   it from `permits.json.gz`** (local file or downloaded from R2) — so you
   never need to keep the `.db` around.
2. Scrapes apps.troymi.gov **newest-first** and stops once it has seen
   several consecutive pages of permits it already has — a routine run
   fetches only a handful of pages, not all ~10,000.
3. `INSERT OR IGNORE`s new permits (keyed on Permit Number).
4. Resolves **BSA Online GUIDs** for up to 500 new permits per run (see
   [BSA Online deep-links](#bsa-online-deep-links) below).
5. Writes `permits.json.gz` (data blob) and `index.html` (UI shell).
6. Uploads `permits.json.gz` to **Cloudflare R2**.
7. `git add / commit / push` → Cloudflare Pages redeploys `index.html`.

Flags:

| Flag | Effect |
|---|---|
| `--no-git` | Update files only, skip commit/push. |
| `--no-upload` | Skip R2 upload (local dev). |
| `--full` | Scrape every page (ignore the stop condition). |
| `--margin N` | Stop after N consecutive all-known pages (default 4). |
| `--no-guids` | Skip BSA GUID resolution this run. |
| `--backfill-guids` | Resolve every permit missing a GUID (long-running). |
| `--guid-limit N` | Cap BSA lookups per run (default 500; 0 = unlimited). |
| `--retry-guid-misses` | Re-attempt permits where a prior lookup found no match. |

## BSA Online deep-links

Each Troy permit has a corresponding record on
[BSA Online](https://bsaonline.com) (the City's building-department
records system), identified by a GUID. When a user clicks a permit number
in the search UI, they go straight to the BSA Online detail page for that
permit — no manual searching required.

### How GUID resolution works

BSA Online has no public API. `update_permits.py` contains a `BsaResolver`
class that drives BSA's advanced-record-search wizard end-to-end for each
permit number:

1. Bootstraps an HTTP session on the BSA search page and extracts the
   `AdvancedRecordSearchSessionGuid` hidden field.
2. Submits each permit number to BSA's search endpoint.
3. Parses the result for a `gotToRecord('...')` call containing the record
   GUID.
4. Stores the GUID (or a "confirmed miss" marker) in `permits.db` and
   embeds it as a 13th column in the data blob.

The 13th column is a **tristate** that preserves resolver state across CI
runs (since `permits.db` is git-ignored):

| Value | Meaning |
|---|---|
| `<36-char GUID>` | Resolved — deep-link goes directly to the BSA record. |
| `-` | Tried but not found in BSA — UI falls back to the BSA search wizard with the permit number copied to clipboard. |
| *(empty)* | Never attempted — same fallback as above. |

### Daily resolution (CI)

The GitHub Actions workflow resolves up to **500 GUIDs per daily run**.
This is conservative to avoid tripping BSA's rate limits. At this pace,
clearing a large backlog takes a long time — see bulk backfill below.

### Bulk backfill

For large-scale GUID resolution, use `backfill_parallel.py`:

```bash
python3 backfill_parallel.py --workers 4 --throttle 0.6
```

This runs N independent `BsaResolver` instances with their own HTTP
sessions, sharing a work queue. Each worker self-throttles. Features:

- **Progress reporting** every 5 minutes (rate, ETA, miss/error rates).
- **Automatic rate-limit detection** — stops if miss rate exceeds 50%,
  throughput collapses, or error rate exceeds 20%.
- **Crash-safe** — writes to the DB in batches and flushes on interrupt.
- At 4 workers / 0.6s throttle, throughput is ~2–3 lookups/sec in
  practice. BSA tends to start throttling after 30–60 minutes; run in
  chunks and wait between sessions.

After a backfill run, regenerate and push:

```bash
python3 update_permits.py --no-guids   # skip re-resolving, just rebuild + upload + push
```

### Daily GUID verification

`verify_bsa_guids.py` spot-checks a random sample of stored GUIDs to
detect silent drift (e.g. if BSA rebuilds their index and re-issues
GUIDs). The CI workflow runs this daily and opens a GitHub issue if
mismatches exceed the threshold.

```bash
python3 verify_bsa_guids.py --sample 20 --throttle 0.6
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  apps.troymi.gov          bsaonline.com                              │
│        │                       │                                     │
│        ▼                       ▼                                     │
│  update_permits.py ──── BsaResolver                                  │
│        │                                                             │
│        ▼                                                             │
│   permits.db  (local SQLite cache, git-ignored)                      │
│        │                                                             │
│        ├──▶  permits.json.gz ──▶  Cloudflare R2                      │
│        │     (data blob)          media.karpowitsch.org/cot-permits/  │
│        │                                                             │
│        └──▶  index.html ─────▶  GitHub ──▶ Cloudflare Pages          │
│              (17 KB UI shell)              cot-permits.karpowitsch.org│
└──────────────────────────────────────────────────────────────────────┘
```

| File | Role |
|---|---|
| `index.html` | 17 KB UI shell deployed via Cloudflare Pages. Fetches the data blob from R2 at load time. |
| `permits.json.gz` | Gzipped JSON data blob (~15 MB). Hosted on Cloudflare R2. Git-ignored. |
| `permits.db` | Local SQLite working cache. Git-ignored — rebuilt from `permits.json.gz` whenever absent. |
| `update_permits.py` | The whole pipeline: bootstrap DB → scrape Troy → resolve BSA GUIDs → write files → upload to R2 → commit + push. |
| `backfill_parallel.py` | Multi-worker bulk GUID resolver for one-off backfill runs. |
| `verify_bsa_guids.py` | Daily spot-check that stored GUIDs still point at the right BSA records. |
| `scrape_permits.py` | Original full-database scraper. Kept as a from-scratch rebuild fallback. |
| `.github/workflows/update-permits.yml` | Daily CI: scrape + resolve GUIDs + upload to R2 + commit + verify. The commit step fetches the latest `main` and resets to it before committing, so it never conflicts even if `main` moved during the ~30-minute scrape. |

## One-time setup

1. **GitHub repo** — <https://github.com/akarpo/cot-permits>. `origin` is
   configured.
2. **Cloudflare Pages** — connected to GitHub; deploys `index.html` on
   every push. Custom domain: `cot-permits.karpowitsch.org`.
3. **Cloudflare R2** — bucket `media`, served at
   `media.karpowitsch.org/cot-permits/`. CORS policy allows `*` origins
   for GET/HEAD.
4. **GitHub Actions secret** — `CLOUDFLARE_API_TOKEN` for R2 uploads in CI.

## The search UI

- **Multi-select permit types** with an All option, then pick a
  **from-year** and **to-year** — the year lists cascade from the chosen
  types. The table appears once both are set.
- **Search box** — filters rows to matches (space-separated terms are AND).
- **Highlight box** — background-highlights every cell containing a word
  (e.g. `fiber`) *without* hiding other rows.
- Click a **permit number** to open the BSA Online record (direct link if
  GUID is resolved; search-wizard fallback with clipboard copy otherwise).
- Click a row to expand the full record; click column headers to sort.

## Notes & caveats

- The incremental scrape assumes Troy's endpoint returns newest-first
  (verified). If a run misses back-dated entries, `--full` forces a
  complete re-scrape.
- `index.html` is ~17 KB; the data blob on R2 can grow without limit
  (no Cloudflare Pages 25 MiB file-size constraint).
- BSA GUID resolution is best-effort — BSA's search wizard isn't a
  documented API and may break if they change their JS. The daily
  verifier guards against silent drift.
- Needs a current browser (uses native `DecompressionStream` —
  Safari 16.4+, current Chrome/Firefox).
