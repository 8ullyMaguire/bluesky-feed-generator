#!/usr/bin/env python3
"""Housekeeping for a feed-generator database.

  python3 db_housekeeping.py stats
  python3 db_housekeeping.py vacuum      # VACUUM INTO a compact copy, verified
  python3 db_housekeeping.py rollup      # served_posts -> served_daily

Why this exists: the live database grows by write churn (seen rows pruned,
metrics pruned, service posts rewritten), not only by new posts. SQLite never
returns freed pages to the filesystem, so a 2 GB file held ~1 GB of live data on
this host. `vacuum` builds and verifies a compact copy; it never swaps files
under a running service — that stays a deliberate two-line operator step.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("FEEDGEN_CONFIG", os.path.join(HERE, "config.json"))

import feedgen as fg  # noqa: E402


def stats(db_path=None):
    db_path = db_path or fg.DB_PATH
    con = sqlite3.connect(db_path, timeout=30)
    try:
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
        out = {
            "path": db_path,
            "file_mb": round(os.path.getsize(db_path) / 1048576, 1),
            "page_size": page_size,
            "pages": page_count,
            "free_pages": freelist,
            "free_mb": round(freelist * page_size / 1048576, 1),
            "reclaimable_pct": round(100.0 * freelist / max(page_count, 1), 1),
        }
        for t in ("posts", "served_posts", "served_daily", "interaction_events",
                  "metrics_log", "seen"):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = None
        wal = db_path + "-wal"
        out["wal_mb"] = round(os.path.getsize(wal) / 1048576, 1) if os.path.exists(wal) else 0.0
        return out
    finally:
        con.close()


def can_vacuum(db_path, free_bytes=None):
    """A VACUUM INTO needs room for a second full copy plus slack."""
    if free_bytes is None:
        free_bytes = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    need = os.path.getsize(db_path) * 2.2
    return free_bytes >= need, free_bytes, need


def vacuum_into(db_path=None, workdir=None):
    db_path = db_path or fg.DB_PATH
    ok, free, need = can_vacuum(db_path)
    if not ok:
        raise SystemExit(f"not enough space: need {need / 1048576:.0f} MB, "
                         f"have {free / 1048576:.0f} MB")
    out = os.path.join(workdir or os.path.dirname(db_path),
                       f"compact-{int(time.time())}.sqlite")
    t0 = time.time()
    con = sqlite3.connect(db_path, timeout=60)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute(f"VACUUM INTO '{out}'")
    finally:
        con.close()
    con = sqlite3.connect(out)
    try:
        check = con.execute("PRAGMA quick_check").fetchone()[0]
        counts = {}
        for t in ("posts", "served_posts", "interaction_events"):
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    finally:
        con.close()
    src = stats(db_path)
    for t, n in counts.items():
        if src.get(t) is not None and n != src[t]:
            raise SystemExit(f"count mismatch after VACUUM INTO: {t}={n} vs {src[t]}")
    if check != "ok":
        raise SystemExit(f"quick_check on the compact copy: {check}")
    return {
        "compact": out,
        "from_mb": src["file_mb"],
        "to_mb": round(os.path.getsize(out) / 1048576, 1),
        "saved_mb": round(src["file_mb"] - os.path.getsize(out) / 1048576, 1),
        "counts": counts,
        "seconds": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("stats", "vacuum", "rollup"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.cmd == "stats":
        s = stats(a.db)
        print(json.dumps(s, indent=1) if a.json else
              "\n".join(f"{k}: {v}" for k, v in s.items()))
    elif a.cmd == "vacuum":
        r = vacuum_into(a.db)
        print(json.dumps(r, indent=1) if a.json else
              f"compact copy: {r['compact']}\n"
              f"{r['from_mb']} MB -> {r['to_mb']} MB (save {r['saved_mb']} MB) "
              f"in {r['seconds']}s\n"
              "Swap when ready: stop the service, move the compact file over "
              f"{a.db or fg.DB_PATH}, start it, then delete the old file.")
    elif a.cmd == "rollup":
        print(f"rolled up: {fg.compact_served()} rows")


if __name__ == "__main__":
    main()
