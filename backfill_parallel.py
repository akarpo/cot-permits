"""Parallel BSA GUID backfill — N independent BsaResolver instances, each with
its own HTTP session, sharing a work queue and writing back to permits.db
under a lock. Designed for one-off backfill, not steady-state daily runs.

Usage:
    python backfill_parallel.py --workers 4 --throttle 0.6

Each worker self-throttles. Total throughput ~= workers / throttle lookups/sec.
"""
import argparse
import sqlite3
import sys
import threading
import time
from datetime import datetime
from queue import Queue, Empty

# Import the resolver from the production script
sys.path.insert(0, ".")
from update_permits import BsaResolver, BSA_RESOLVE_ORDER_BY  # noqa: E402


DB_PATH = "permits.db"
BATCH_SIZE = 50


def worker(wid, work, conn, write_lock, counters, counter_lock, throttle, stop_evt):
    resolver = BsaResolver(throttle=throttle)
    batch = []
    now = datetime.now().isoformat(timespec="seconds")
    local_a = local_r = local_e = 0
    try:
        while not stop_evt.is_set():
            try:
                pn = work.get_nowait()
            except Empty:
                break
            try:
                guid = resolver.resolve(pn)
                local_a += 1
                if guid:
                    local_r += 1
                # Resolver returned cleanly: write the result (real GUID, or
                # None meaning "permit confirmed not in BSA").
                batch.append((guid, now, pn))
            except Exception as e:                          # noqa: BLE001
                local_a += 1
                local_e += 1
                print(f"  [w{wid}] {pn} error: {type(e).__name__}: {e}",
                      file=sys.stderr)
                # HTTP error: don't poison the DB. Leave bsa_resolved_at NULL
                # so the permit is retried on a future run.
            if len(batch) >= BATCH_SIZE:
                with write_lock:
                    conn.executemany(
                        "UPDATE permits SET bsa_guid=?, bsa_resolved_at=? "
                        "WHERE permit_number=?", batch)
                    conn.commit()
                batch.clear()
                with counter_lock:
                    counters["attempted"] += local_a
                    counters["resolved"]  += local_r
                    counters["errors"]    += local_e
                    local_a = local_r = local_e = 0
    finally:
        # Flush remainder so a Ctrl-C doesn't lose the last partial batch.
        if batch:
            with write_lock:
                conn.executemany(
                    "UPDATE permits SET bsa_guid=?, bsa_resolved_at=? "
                    "WHERE permit_number=?", batch)
                conn.commit()
            with counter_lock:
                counters["attempted"] += local_a
                counters["resolved"]  += local_r
                counters["errors"]    += local_e


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--throttle", type=float, default=0.6,
                    help="seconds between calls *per worker* (default 0.6)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap total permits resolved this run (0 = unlimited)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)

    q = f"SELECT permit_number FROM permits WHERE bsa_resolved_at IS NULL {BSA_RESOLVE_ORDER_BY}"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    pending = [r[0] for r in conn.execute(q).fetchall()]
    if not pending:
        print("nothing to resolve")
        return

    total = len(pending)
    print(f"workers={args.workers}  throttle={args.throttle}s/worker  "
          f"max-rate={args.workers/args.throttle:.1f} lookups/sec  pending={total:,}")
    print(f"projected runtime (if no throttling): "
          f"{total * args.throttle / args.workers / 3600:.1f}h floor, "
          f"~{total / (args.workers / (args.throttle + 0.5)) / 3600:.1f}h realistic")

    work = Queue()
    for pn in pending:
        work.put(pn)

    write_lock = threading.Lock()
    counter_lock = threading.Lock()
    counters = {"attempted": 0, "resolved": 0, "errors": 0}
    stop_evt = threading.Event()

    threads = [
        threading.Thread(target=worker, name=f"w{i}",
                         args=(i, work, conn, write_lock, counters,
                               counter_lock, args.throttle, stop_evt))
        for i in range(args.workers)
    ]
    start = time.time()
    for t in threads:
        t.start()

    last_a = last_r = last_e = 0
    last_t = start
    PROGRESS_INTERVAL = 300        # report every 5 minutes
    MISS_RATE_TRIP    = 0.50       # >50% misses in a window -> rate-limit
    MIN_SAMPLE        = 200        # need at least this many attempts to judge
    COLLAPSE_FLOOR    = 50         # <this many attempts in a window post-warmup -> hard throttle
    WARMUP_SECS       = 600        # don't apply collapse check before this
    stopped_reason    = None
    try:
        while any(t.is_alive() for t in threads):
            # Sleep in small ticks so a Ctrl-C is responsive
            for _ in range(PROGRESS_INTERVAL // 5):
                if all(not t.is_alive() for t in threads):
                    break
                time.sleep(5)
            with counter_lock:
                a, r, e = (counters["attempted"], counters["resolved"],
                           counters["errors"])
            now = time.time()
            dt = now - last_t
            delta_a = a - last_a
            delta_r = r - last_r
            delta_e = e - last_e
            rate = delta_a / dt if dt > 0 else 0
            miss = (delta_a - delta_r) / max(delta_a, 1)
            elapsed_h = (now - start) / 3600
            eta = (total - a) / rate / 3600 if rate > 0 else float("inf")
            print(f"  t+{elapsed_h:5.2f}h  {a:>7,}/{total:,} ({100*a/total:5.1f}%)  "
                  f"resolved={r:,} (+{delta_r})  err={e} (+{delta_e})  "
                  f"rate={rate:5.2f}/s  miss={miss:.1%}  eta={eta:.1f}h",
                  flush=True)
            # --- rate-limit / health checks (after we have a real sample) ---
            if delta_a >= MIN_SAMPLE and miss > MISS_RATE_TRIP:
                stopped_reason = (f"miss rate {miss:.0%} over {delta_a} attempts "
                                  f"(BSA likely throttling: returning no GUIDs)")
            elif (now - start) > WARMUP_SECS and delta_a < COLLAPSE_FLOOR:
                stopped_reason = (f"throughput collapse: only {delta_a} attempts "
                                  f"in {PROGRESS_INTERVAL}s (network or hard throttle)")
            elif delta_a >= MIN_SAMPLE and delta_e > delta_a * 0.20:
                stopped_reason = (f"error rate {100*delta_e/delta_a:.0f}% over "
                                  f"{delta_a} attempts ({delta_e} errors)")
            if stopped_reason:
                print(f"\n!! RATE-LIMIT DETECTED: {stopped_reason}", flush=True)
                print("!! signalling workers to stop and flush in-flight batches...",
                      flush=True)
                stop_evt.set()
                break
            last_a, last_r, last_e, last_t = a, r, e, now
    except KeyboardInterrupt:
        print("\ninterrupt received — signalling workers to stop...", flush=True)
        stop_evt.set()

    for t in threads:
        t.join()
    conn.close()
    if stopped_reason:
        print(f"\nSTOPPED-BY-LIMIT: {stopped_reason}", flush=True)
    print(f"DONE: attempted={counters['attempted']:,} "
          f"resolved={counters['resolved']:,} errors={counters['errors']}",
          flush=True)


if __name__ == "__main__":
    main()
