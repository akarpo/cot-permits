# cot-permits — City of Troy permits, searchable & self-publishing

A self-contained, searchable index of **every permit in the City of Troy's
"Permits Issued" database** (apps.troymi.gov), plus a one-command pipeline that
keeps it current and re-publishes it.

- **`index.html`** — the site. A single self-contained file: the full permit
  dataset is embedded as gzipped + base64 JSON and decompressed in-browser, so
  it works both by double-clicking locally *and* deployed on the web. Searchable,
  filterable, ~19 MB.
- **`update_permits.py`** — run this to pull new permits, rebuild `index.html`,
  and publish.

## Updating — one command

```bash
python3 update_permits.py
```

Each run:
1. Opens `permits.db` (local SQLite cache). If it's missing, **bootstraps it
   from the data already embedded in `index.html`** — so you never need to keep
   the `.db` around; the repo is the portable source of truth.
2. Scrapes apps.troymi.gov **newest-first** and stops once it has seen several
   consecutive pages of permits it already has — a routine run fetches only a
   handful of pages, not all ~10,000.
3. `INSERT OR IGNORE`s the new permits (keyed on Permit Number).
4. Regenerates `index.html` from the database.
5. `git add / commit / push` → Cloudflare Pages redeploys automatically.

Flags: `--no-git` (update files only), `--full` (scrape every page, ignore the
stop condition), `--margin N` (stop after N consecutive all-known pages; default 4).

## One-time setup (GitHub + Cloudflare)

1. **GitHub repo** — published (public) at <https://github.com/akarpo/cot-permits>.
   `origin` is configured, so `git push` from this folder publishes updates.
2. **Connect Cloudflare Pages**: Cloudflare dashboard → Workers & Pages → Create →
   Pages → Connect to Git → pick this repo. Build settings: framework preset
   **none**, build command **(empty)**, output directory **`/`** (root). Deploy.
   `.assetsignore` keeps the scripts and `.db` out of the deployment — only
   `index.html` is served.
3. After that, every `python3 update_permits.py` auto-commits and pushes, and
   Cloudflare redeploys within a minute.

## How it works (architecture)

| Piece | Role |
|---|---|
| `index.html` | The deployed site **and** the portable source of truth — embeds the whole dataset. Committed to the repo. Works offline. |
| `permits.db` | Local SQLite working cache, keyed on Permit Number. **Git-ignored** — rebuilt from `index.html` whenever it's absent, so it never bloats git history. |
| `update_permits.py` | The whole pipeline: bootstrap/open DB → incremental scrape → regenerate `index.html` → commit + push. |
| `scrape_permits.py` | The original full-database scraper. Kept as a from-scratch rebuild fallback; not needed for routine updates. |

## The search UI

- **Two required steps**: pick a **Permit Type**, then a **From-year** and a
  **To-year** — the year lists cascade from the chosen type. The table appears
  only once the type and both years are set.
- **Search box** — filters rows to matches (space-separated terms are AND).
- **Highlight box** — background-highlights every cell containing a word (e.g.
  `fiber`) *without* hiding other rows, so you can scan matches in context.
- Click a row to expand the full record; click column headers to sort.

## Notes & caveats

- The incremental scrape assumes Troy's endpoint returns newest-first (verified)
  and that you run this at least every few weeks. If a run somehow misses
  back-dated entries, `--full` forces a complete re-scrape.
- `index.html` is ~19 MB — under Cloudflare Pages' 25 MiB per-file cap, with
  headroom. Each update commits a fresh copy, so git history grows ~19 MB per
  run; run `git gc` or squash history occasionally if that ever matters.
- Needs a current browser (uses native `DecompressionStream` — Safari 16.4+,
  current Chrome/Firefox).
