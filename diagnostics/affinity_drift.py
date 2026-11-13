#!/usr/bin/env python3
"""How much of a reader's affinity is their own behaviour, and how much is drift?

`owner_gravity_daily` (0.004/day) blends every user_affinity row toward the operator's
taste, ~30% convergence in three months. That is a real editorial choice, not a bug,
but it means an affinity row can be mostly the operator's opinion wearing a reader's
name. This reports how much of each row is explainable by the reader's own likes.

    python3 diagnostics/affinity_drift.py --db ... --gravity 0.004
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone


def _median(vals):
    if not vals:
        return None
    vals = sorted(vals)
    return round(vals[len(vals) // 2], 3)


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/.local/share/feedgen/feedgen.sqlite"))
    ap.add_argument("--gravity", type=float, default=0.004)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no database at {args.db}")
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = datetime.now(timezone.utc).timestamp()

    rows = con.execute(
        "SELECT requester_did, author_did, score FROM user_affinity").fetchall()
    if not rows:
        sys.exit("no user_affinity rows")
    readers = {r[0] for r in rows}

    last = {}
    for did, ts in con.execute(
            "SELECT requester_did, MAX(created_at) FROM interaction_events "
            "WHERE kind IN ('like','repost','reply') GROUP BY 1").fetchall():
        last[did] = float(ts or 0)

    active, silent = [], []
    for did in readers:
        (active if last.get(did, 0) > now - args.days * 86400 else silent).append(did)

    # Drift is a daily blend toward the operator's taste; a reader who stopped
    # interacting n days ago has been drifting for n days since.
    drift = {}
    for did in readers:
        days = max(0.0, (now - last.get(did, 0)) / 86400.0) if last.get(did) else None
        if days is None:
            drift[did] = None            # never interacted: 100% drift by construction
        else:
            drift[did] = round(1.0 - (1.0 - args.gravity) ** days, 3)

    out = {
        "affinity_rows": len(rows),
        "readers": len(readers),
        "active_readers": len(active),
        "silent_readers": len(silent),
        "silent_share": pct(len(silent), len(readers)),
        "never_interacted": sum(1 for d in readers if d not in last),
        "median_drift": _median([v for v in drift.values() if v is not None]),
        "gravity_daily": args.gravity,
    }
    if args.json:
        print(__import__("json").dumps(out, indent=1))
        return
    print(f"affinity rows {out['affinity_rows']} over {out['readers']} readers")
    print(f"  active in {args.days}d: {out['active_readers']}  "
          f"silent: {out['silent_readers']} ({out['silent_share']}%)  "
          f"never interacted: {out['never_interacted']}")
    print(f"  gravity {args.gravity}/day: a silent reader is "
          f"{100 * (1 - (1 - args.gravity) ** 90):.0f}% the operator's taste after 90 days")
    if out["median_drift"] is not None:
        print(f"  median drift among readers with a history: {out['median_drift']}")


if __name__ == "__main__":
    main()
