# Incident: permit data frozen for 9 days (2026-05-26 → 2026-06-04)

**Status:** Resolved 2026-06-04
**Fix:** [`8f30e1a`](https://github.com/akarpo/cot-permits/commit/8f30e1a) — *fix(ci): always pass `--remote` so R2 uploads hit the real bucket*
**Bug introduced:** [`d2bf4961`](https://github.com/akarpo/cot-permits/commit/d2bf4961) (2026-05-20)
**Impact:** The public `permits.json.gz` on R2 — and therefore the live site, which loads it at page load — was frozen at the 2026-05-26 snapshot for 9 days. The daily "Update permits" Action reported **success** the entire time.

## TL;DR

`update_permits.py` uploads the data blob with `wrangler r2 object put`. A 2026-05-20 change made the `--remote` flag conditional on `CLOUDFLARE_API_TOKEN` being **unset**. `--remote` is what selects the *real* R2 bucket; the API token is only *auth* — they are orthogonal. In CI the token **is** set, so `--remote` was dropped and wrangler wrote to its **local Miniflare simulation**, exited `0`, and printed `uploaded to R2` while nothing actually uploaded. The runner was then destroyed and the bytes vanished. Fix: always pass `--remote`.

## Symptom

- The daily Action kept going **green**.
- The data on R2 / the live site had not changed since May 26.
- Run times had quietly crept **up** day over day (~1m → ~8m).

## Root cause

`d2bf4961` replaced an unconditional `--remote` with this:

```python
cmd = ["wrangler", "r2", "object", "put", DATA_R2_DEST,
       "--file", DATA_GZ_PATH, "--content-type", "application/gzip"]
if not os.environ.get("CLOUDFLARE_API_TOKEN"):   # ← inverted
    cmd.append("--remote")
```

- **CI** (token set): `not <set>` → `False` → no `--remote` → writes to **local sim**, exit 0. Silent no-op.
- **Local dev** (no token, auth via `wrangler login`): `not <unset>` → `True` → `--remote` added → real upload.

So the only path that actually wrote to R2 was a local run.

## Why it surfaced on May 26 specifically

The bug landed May 20, but daily **manual local runs** kept R2 fresh until then (local runs have no `CLOUDFLARE_API_TOKEN`, so they hit the `--remote` branch). The last such run was **2026-05-26 22:53 UTC** — commit `Update permits: +1 new (266,917 total)`, the local `git_sync` message format. After that, only the scheduled CI job ran, and every CI upload silently no-op'd.

## Red herring

The 2026-05-26 12:21 scheduled run *did* hard-fail in 34s — the `akarpo` GitHub account was briefly suspended, so checkout got `403: Your account is suspended`. That was transient, cleared the same day, and is **unrelated** to the data freeze.

## Evidence

- R2 `last-modified` stuck at `Tue, 26 May 2026 22:53:00 GMT` while every run stayed green.
- Every run re-bootstrapped the **identical** 266,917-permit snapshot from R2.
- The `+N new` backlog grew every day — because each day's scrape was never persisted, the gap to "today" widened:

  | run | bootstraps | `+new` found | runtime |
  |-----|-----------|--------------|---------|
  | May 27 | 266,917 | +2   | 1m22s |
  | May 31 | 266,917 | +166 | 3m27s |
  | Jun 3  | 266,917 | +325 | 7m0s  |
  | Jun 4  | 266,917 | +378 | 7m49s |

  Growing backlog ⇒ more GUIDs to re-resolve each run ⇒ the creeping run time.

## The fix

Always pass `--remote` (auth still comes from `CLOUDFLARE_API_TOKEN` in CI or `wrangler login` locally):

```python
cmd = ["wrangler", "r2", "object", "put", DATA_R2_DEST,
       "--file", DATA_GZ_PATH, "--content-type", "application/gzip",
       "--remote"]
```

## Validation (2026-06-04, run `26981339676`)

- Scraped **+424 new → 267,341 total**, resolved **423/425** GUIDs, wrote 19.3 MB, `uploaded to R2`, verify `ok=20 mismatch=0`.
- R2 object changed for the first time in 9 days:

  | | before | after |
  |---|---|---|
  | `last-modified` | Tue, 26 May 2026 22:53:00 GMT | Thu, 04 Jun 2026 21:49:02 GMT |
  | `content-length` | 19,296,267 | 19,334,165 |
  | `etag` | `2d8f8be957d7ad36742ccd5867e4bd8c` | `c2b3ef791cf73305506b59fde24c4083` |

- The R2 timestamp matches the upload moment exactly, and `CLOUDFLARE_API_TOKEN` still has write scope.
- The next scheduled run should bootstrap 267,341 (not 266,917) and find only a handful of new permits; run time returns to short and stable.

## Follow-ups (recommended, not yet done)

1. **Fail loudly on a stale upload.** The pipeline stayed green while broken for 9 days. After upload, `HEAD` the public URL and assert the returned `content-length`/`etag` matches the file just written (the script already computes `data_hash`). Turns a silent freeze into a red run.
2. **Pin wrangler.** The workflow runs `npm install -g wrangler` unpinned, so the CLI's behavior/defaults can drift between runs. Pin a known-good major version.
3. **Gitignore `.wrangler/`.** `.wrangler/cache/wrangler-account.json` is currently tracked; the directory is local wrangler state and should not be committed.
4. **`.git` is ~129 MB** for a text-only repo — historical large-blob bloat (the data blob was committed before being externalized to R2 on May 20). Optional `git filter-repo` cleanup if clone size becomes annoying.
