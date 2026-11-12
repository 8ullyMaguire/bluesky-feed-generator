#!/usr/bin/env python3
"""How concentrated is each board, and what would it take to widen it?

`max_per_author` is a blunt instrument: one number for every feed, and raising it
buys depth at the cost of voices (leftist-24h went 179 -> 114 distinct authors
when the cap went 3 -> 20). This prints the shape of each board so the number can
be chosen per feed instead of argued about:

  * top author share — the highest concentration on the board, in %
  * voices needed   — `max_posts / max_per_author`, the floor the candidate pool
    must clear for the board to fill at all
  * cap at 10%      — the cap that would hold every author under a tenth of the
    board, and whether the pool is big enough for it

Read-only: opens the database `mode=ro`, so it is safe from a timer.

  python3 diagnostics/author_concentration.py --db ~/.local/share/feedgen/feedgen.sqlite \
      --config /mnt/disk-important/personal/documents/code/misc/feedgen/config.json
"""
import argparse
import json
import math
import os
import sqlite3


def load_caps(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return {f["rkey"]: f for f in cfg["feeds"]}, (cfg.get("owner") or {}).get("did")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--share", type=float, default=0.10,
                    help="target maximum share for a single author (default 0.10)")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    feeds, owner_did = load_caps(args.config)
    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(args.db), uri=True)
    ts = con.execute("SELECT MAX(ts) FROM metrics_log").fetchone()[0]
    rows = con.execute(
        "SELECT feed_rkey, final, unique_authors FROM metrics_log WHERE ts=?",
        (ts,)).fetchall()

    report = {"generated": ts, "feeds": []}
    print("%-22s %6s %7s %9s %9s %8s %s"
          % ("feed", "board", "voices", "top share", "cap now", "cap@10%", "fills?"))
    for rkey, final, authors in sorted(rows):
        f = feeds.get(rkey) or {}
        cap = int(f.get("max_per_author", 0) or 0)
        board = int(final or 0)
        voices = int(authors or 0)
        if not board or not voices:
            continue
        # A board holds at most `cap` posts per voice, so the pool must be at
        # least board/cap deep. Below that the cap is not the limiter — supply is.
        needed = math.ceil(board / cap) if cap else 0
        cap10 = max(1, math.floor(board * args.share))
        # The cap that holds every author under the target share, given the
        # voices actually available on the board.
        ok = voices >= needed
        report["feeds"].append({"feed": rkey, "board": board, "voices": voices,
                                "cap": cap, "top_share": round(cap / board, 3),
                                "needed_voices": needed, "cap_at_target": cap10,
                                "pool_covers": ok})
        print("%-22s %6d %7d %8.1f%% %9d %8d %s"
              % (rkey, board, voices, 100.0 * cap / board, cap, cap10,
                 "yes" if ok else "NO — supply-bound"))

    if args.json:
        with open(os.path.expanduser(args.json), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    print("\ncap now is the ceiling, not the observed average: a board can sit well "
          "under it.\nread `top share` as the most one account could legally hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
