#!/usr/bin/env python3
"""A/B report — compare feeds that carry the same `ab_group`.

The point of the harness is that an experiment is a config change, not a code
change: publish two feeds with the same `ab_group`, differ in exactly one
parameter, and let `likes per 1,000 serves` decide.

    python3 ab_report.py              # every group, 7 days
    python3 ab_report.py --days 14
    python3 ab_report.py --group media --json

Honesty rules baked in:
  * a rate with fewer than 30 likes is marked "inconclusive" — the interval is
    wider than any difference you are likely to see;
  * the interval is Poisson on the like count, not a t-test on a rate, because
    the denominator (serves) is a count of deliveries, not a sample of people;
  * feeds outside a group are listed under "(ungrouped)" so the table is a
    complete picture of the deployment, not a filtered one.
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("FEEDGEN_CONFIG", os.path.join(HERE, "config.json"))

import feedgen as fg  # noqa: E402

CONFIG_PATH = os.environ.get("FEEDGEN_CONFIG") or "config.json"


def poisson_ci(likes, serves, z=1.96):
    """95% interval for likes per 1,000 serves, from the like count alone."""
    if not serves:
        return (0.0, 0.0)
    rate = 1000.0 * likes / serves
    half = z * math.sqrt(max(int(likes), 0)) * 1000.0 / serves
    return (round(max(0.0, rate - half), 1), round(rate + half, 1))


def collect(days=7.0):
    stats = fg.likes_per_1k(days)
    try:
        with fg.CACHE_LOCK:
            boards = {k: len(v) for k, v in (fg.CACHE.get("feeds") or {}).items()}
    except Exception:                                  # noqa: BLE001
        boards = {}
    rows = []
    for f in fg.CFG.get("feeds") or []:
        rk = f["rkey"]
        s = stats.get(rk) or {}
        likes = int(s.get("likes", 0) or 0)
        serves = int(s.get("serves", 0) or 0)
        lo, hi = poisson_ci(likes, serves)
        rows.append({
            "group": f.get("ab_group") or "(ungrouped)",
            "feed": rk,
            "name": f.get("display_name") or rk,
            "board": boards.get(rk),
            "serves": serves,
            "requesters": int(s.get("requesters", 0) or 0),
            "likes": likes,
            "per_1k": round(1000.0 * likes / serves, 1) if serves else 0.0,
            "ci": [lo, hi],
            "conclusive": likes >= 30,
        })
    rows.sort(key=lambda r: (r["group"], -r["per_1k"]))
    return rows


def summarise(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    out = []
    for g, rs in groups.items():
        lines = [f"group {g}:"]
        for r in rs:
            mark = "" if r["conclusive"] else "  (inconclusive: <30 likes)"
            lines.append(
                f"  {r['feed']:<22} board={r['board'] if r['board'] is not None else '-':<5} "
                f"serves={r['serves']:<6} likes={r['likes']:<5} "
                f"per1k={r['per_1k']:<6} ci=[{r['ci'][0]}, {r['ci'][1]}]{mark}")
        if g != "(ungrouped)" and len(rs) < 2:
            lines.append("  ! a group needs at least two feeds to be an experiment")
        out.append("\n".join(lines))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="A/B report by ab_group")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--group")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = collect(args.days)
    if args.group:
        rows = [r for r in rows if r["group"] == args.group]
        if not rows:
            sys.exit(f"no feeds with ab_group={args.group!r} in {CONFIG_PATH}")
    if args.json:
        print(json.dumps({"days": args.days, "feeds": rows}, indent=1))
        return
    print(f"# A/B report over {args.days:g} days (config: {CONFIG_PATH})")
    print(summarise(rows))


if __name__ == "__main__":
    main()
