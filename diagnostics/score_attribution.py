#!/usr/bin/env python3
"""Why is this post on the board? Read the decomposition the ranker stored.

`board_scores` is written every refresh with the factors rank_score() applied to
each board member: log engagement, the velocity term, the topic/taste/prior
multipliers and the final score. That makes "the score is a product of seven
multipliers nobody can audit" a table instead of an argument.

  python3 diagnostics/score_attribution.py --db ~/.local/share/feedgen/feedgen.sqlite
  python3 diagnostics/score_attribution.py --db ... --feed leftist-week --n 15

Read-only.
"""
import argparse
import os
import sqlite3


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--feed", default="")
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args(argv)

    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(args.db), uri=True)
    con.row_factory = sqlite3.Row
    q = con.execute("SELECT COUNT(*) FROM board_scores").fetchone()[0]
    if not q:
        print("no decomposition stored yet — wait for one refresh after the engine update")
        return 1

    print("%-20s %5s %8s %8s %8s %8s %9s" % (
        "feed", "n", "log_eng", "velocity", "topic", "taste", "prior"))
    print("-" * 74)
    for r in con.execute("""
            SELECT feed_rkey, COUNT(*) n,
                   AVG(COALESCE(log_eng, 0)) le, AVG(COALESCE(velocity, 1)) v,
                   AVG(COALESCE(topic_mult, 1)) tm, AVG(COALESCE(taste_mult, 1)) ta,
                   AVG(COALESCE(prior, 1)) pr
            FROM board_scores GROUP BY feed_rkey ORDER BY feed_rkey"""):
        if args.feed and r["feed_rkey"] != args.feed:
            continue
        print("%-20s %5d %8.2f %8.3f %8.3f %8.3f %9.3f" % (
            r["feed_rkey"], r["n"], r["le"], r["v"], r["tm"], r["ta"], r["pr"]))

    # Non-engagement lift: what the multipliers add on top of engagement alone.
    print("\nnon-engagement lift (topic x taste x prior), by feed:")
    rows = con.execute("""
        SELECT feed_rkey, COUNT(*) n,
               AVG(COALESCE(topic_mult,1) * COALESCE(taste_mult,1)
                   * COALESCE(prior,1)) avg_lift,
               MAX(COALESCE(topic_mult,1) * COALESCE(taste_mult,1)
                   * COALESCE(prior,1)) max_lift
        FROM board_scores GROUP BY feed_rkey ORDER BY avg_lift DESC""").fetchall()
    for r in rows:
        if args.feed and r["feed_rkey"] != args.feed:
            continue
        flag = "  <- a multiplier stack is doing the ranking" if r["max_lift"] > 2.0 else ""
        print("  %-20s n=%-4d avg %.2fx  max %.2fx%s"
              % (r["feed_rkey"], r["n"], r["avg_lift"], r["max_lift"], flag))

    print("\nowner posts on the board (score percentile of the board):")
    for r in con.execute("""
            SELECT feed_rkey, SUM(owner) n_own, COUNT(*) n,
                   MAX(CASE WHEN owner=1 THEN pos END) max_pos,
                   MIN(CASE WHEN owner=1 THEN pos END) min_pos
            FROM board_scores GROUP BY feed_rkey ORDER BY feed_rkey"""):
        if args.feed and r["feed_rkey"] != args.feed:
            continue
        if r["n_own"]:
            print("  %-20s %3d/%-3d owner posts, positions %s-%s"
                  % (r["feed_rkey"], r["n_own"], r["n"], r["min_pos"], r["max_pos"]))

    feed = args.feed or "leftist-24h"
    print("\nweakest members of %s (lowest score first):" % feed)
    for r in con.execute("""
            SELECT pos, uri, score, log_eng, velocity, topic_mult, taste_mult,
                   prior, owner, track FROM board_scores
            WHERE feed_rkey=? ORDER BY score ASC LIMIT ?""", (feed, args.n)):
        print("  #%-3d score %7.3f  log_eng %5.2f  vel %5.3f  topic %.2f  taste %.2f"
              "  prior %.2f%s%s"
              % (r["pos"], r["score"], r["log_eng"] or 0, r["velocity"] or 0,
                 r["topic_mult"] or 1, r["taste_mult"] or 1, r["prior"] or 1,
                 "  OWNER" if r["owner"] else "",
                 "  [" + r["track"] + "]" if r["track"] else ""))
        print("       %s" % r["uri"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
