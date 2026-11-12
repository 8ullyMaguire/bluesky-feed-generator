#!/usr/bin/env python3
"""Per-feed reader report: who read what, and what the board gave back.

The spreadsheets-that-matter question for a feed estate is not "how many posts
did we publish" but "did any feed get read, and did it earn likes per reader".
`served_posts` answers the first (it is per-requester, pruned at 30 days) and
`interaction_events` the second, but only for requesters whose JWT verified —
say so in the output instead of implying total coverage.

Usage:
  python3 diagnostics/reader_report.py --db ~/.local/share/feedgen/feedgen.sqlite \\
      [--days 7] [--json out.json]

Read-only.
"""
import argparse
import json
import os
import sqlite3
import sys
import time


def q(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(args.db),
                          uri=True)
    con.row_factory = sqlite3.Row
    since = int(time.time() - args.days * 86400)

    served = {r["feed_rkey"]: r for r in q(con, """
        SELECT feed_rkey, COUNT(*) AS deliveries,
               COUNT(DISTINCT requester_did) AS readers,
               COUNT(DISTINCT post_uri) AS posts
        FROM served_posts WHERE served_at >= ? GROUP BY feed_rkey""", (since,))}

    events = {}
    try:
        for r in q(con, """
                SELECT feed_rkey, kind, COUNT(*) AS n
                FROM interaction_events WHERE created_at >= ?
                GROUP BY feed_rkey, kind""", (since,)):
            events.setdefault(r["feed_rkey"], {})[r["kind"]] = r["n"]
    except sqlite3.Error:
        pass

    totals = {r["feed_rkey"]: r["n"] for r in q(con, """
        SELECT feed_rkey, COUNT(*) AS n FROM served_posts
        WHERE served_at >= ? AND requester_did IS NOT NULL
        GROUP BY feed_rkey""", (since,))}

    rows = []
    for rkey in sorted(set(served) | set(events)):
        s = served.get(rkey)
        deliveries = s["deliveries"] if s else 0
        readers = s["readers"] if s else 0
        ev = events.get(rkey, {})
        likes = ev.get("like", 0) + ev.get("like_seen", 0)
        rows.append({
            "feed": rkey, "deliveries": deliveries, "readers": readers,
            "posts": s["posts"] if s else 0,
            "likes": likes, "reposts": ev.get("repost", 0),
            "less": ev.get("less", 0),
            "likes_per_1k": round(1000.0 * likes / deliveries, 1) if deliveries else 0.0,
        })

    readers_total = q(con, """SELECT COUNT(DISTINCT requester_did) FROM
                              served_posts WHERE served_at >= ?""", (since,))[0][0]
    width = max([len(r["feed"]) for r in rows] + [20])
    print("%-*s %10s %8s %8s %7s %7s %6s %11s" % (
        width, "feed", "deliveries", "readers", "posts", "likes", "reposts",
        "less", "likes/1k"))
    for r in rows:
        print("%-*s %10d %8d %8d %7d %7d %6d %11.1f" % (
            width, r["feed"], r["deliveries"], r["readers"], r["posts"],
            r["likes"], r["reposts"], r["less"], r["likes_per_1k"]))
    # The cost of per-feed seen scoping is a post appearing in two windows for
    # one reader. This is the monitor that makes that visible instead of a claim.
    dup = q(con, """SELECT COUNT(*) FROM (
                      SELECT requester_did, post_uri FROM served_posts
                      WHERE requester_did IS NOT NULL AND served_at >= ?
                      GROUP BY 1, 2 HAVING COUNT(DISTINCT feed_rkey) > 1)""",
            (since,))[0][0]
    print("\n%d distinct readers in the last %.0f days; %d deliveries total"
          % (readers_total, args.days, sum(totals.values())))
    print("cross-feed repeats: %d (reader, post) pairs served by 2+ feeds"
          % dup)
    print("coverage: verified JWT readers only, likes counted when the poller "
          "observed them, deliveries = distinct (reader, post) pairs")

    if args.json:
        with open(os.path.expanduser(args.json), "w", encoding="utf-8") as f:
            json.dump({"window_days": args.days, "readers": readers_total,
                       "feeds": rows}, f, indent=2)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
