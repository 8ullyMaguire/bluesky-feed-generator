#!/usr/bin/env python3
"""Weekly estate report: is each board healthy, read, and keeping its promise?

`metrics_log` already records what every cycle produced: board size, engagement
median, concentration, author count, and the funnel from in_window to final.
Nobody read it in aggregate, so every tuning argument was an opinion. This turns
seven days of cycles into one table per deployment and answers four questions:

  1. Did each board fill, and how often did it fall short of its cap?  (a short
     board means the likes floor relaxed — the gate is supply-bound, not tuned)
  2. What does the board look like?  score level, engagement level, concentration,
     and how many distinct voices it carries.
  3. Is anyone reading, and does the owner crowd the boards out?
  4. Do the boards overlap so much that subscribing to several is pointless?

Usage:
  python3 diagnostics/weekly_report.py --db ~/.local/share/feedgen/feedgen.sqlite \\
      --config ~/feedgen/config.json [--days 7] [--json out.json]

Read-only. Safe to run from a timer while the service is serving.
"""
import argparse
import json
import os
import sqlite3
import statistics
import time


def con_ro(path):
    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


def q(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def load_metrics(con, since):
    """Per-feed aggregates over the window, from one row per cycle."""
    out = {}
    for r in q(con, """
            SELECT feed_rkey, COUNT(*) AS cycles,
                   AVG(posts) AS board_avg, MIN(posts) AS board_min,
                   AVG(score_median) AS score_median,
                   AVG(score_p90) AS score_p90,
                   AVG(engagement_median) AS eng_median,
                   AVG(engagement_gini) AS gini,
                   AVG(unique_authors) AS authors,
                   AVG(in_window) AS in_window,
                   AVG(post_gate) AS post_gate,
                   AVG(suppressed) AS suppressed,
                   AVG(final) AS final
            FROM metrics_log WHERE ts >= ? GROUP BY feed_rkey
            ORDER BY feed_rkey""", (since,)):
        d = dict(r)
        iw, pg, fin = d.get("in_window") or 0, d.get("post_gate") or 0, d.get("final") or 0
        d["gate_share"] = pct(pg, iw)
        d["fill_share"] = pct(fin, pg)
        d["suppressed_share"] = pct(d.get("suppressed") or 0, pg)
        for k in ("board_avg", "board_min", "score_median", "score_p90", "eng_median",
                  "gini", "authors", "in_window", "post_gate", "suppressed", "final"):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 2)
        out[d["feed_rkey"]] = d
    return out


def short_cycles(con, since, targets):
    """Cycles where a board served less than its cap: the gate had to relax."""
    out = {}
    for rkey, cap in targets.items():
        row = q(con, """
            SELECT COUNT(*) AS cycles,
                   SUM(CASE WHEN posts < ? THEN 1 ELSE 0 END) AS short
            FROM metrics_log WHERE ts >= ? AND feed_rkey = ?""", (cap, since, rkey))
        if row:
            out[rkey] = {"cycles": row[0]["cycles"] or 0, "short": row[0]["short"] or 0}
            out[rkey]["short_share"] = pct(out[rkey]["short"], out[rkey]["cycles"])
    return out


def delivered(con, since):
    """Per feed: what readers were actually handed, and what those posts were worth."""
    out = {}
    for r in q(con, """
            SELECT sp.feed_rkey AS rkey,
                   COUNT(*) AS deliveries,
                   COUNT(DISTINCT sp.requester_did) AS readers,
                   COUNT(DISTINCT sp.post_uri) AS posts,
                   AVG(p.like_count) AS likes_avg,
                   SUM(CASE WHEN p.like_count = 0 THEN 1 ELSE 0 END) AS zero_likes
            FROM served_posts sp LEFT JOIN posts p ON p.uri = sp.post_uri
            WHERE sp.served_at >= ? GROUP BY sp.feed_rkey""", (since,)):
        d = dict(r)
        d["zero_likes_share"] = pct(d.get("zero_likes") or 0, d.get("deliveries") or 0)
        if d.get("likes_avg") is not None:
            d["likes_avg"] = round(float(d["likes_avg"]), 1)
        out[d["rkey"]] = d
    return out


def owner_share(con, since, owner_did):
    """Share of delivered posts written by the operator, per feed."""
    if not owner_did:
        return {}
    return {r["rkey"]: {"deliveries": r["n"], "owner": r["o"],
                        "owner_share": pct(r["o"], r["n"])}
            for r in q(con, """
                SELECT sp.feed_rkey AS rkey, COUNT(*) AS n,
                       SUM(CASE WHEN p.author_did = ? THEN 1 ELSE 0 END) AS o
                FROM served_posts sp JOIN posts p ON p.uri = sp.post_uri
                WHERE sp.served_at >= ? GROUP BY sp.feed_rkey""", (owner_did, since))}


def duplicates(con, since, limit=10):
    """Posts delivered to the same reader by more than one feed."""
    row = q(con, """
        SELECT COUNT(*) AS pairs,
               SUM(CASE WHEN n_feeds > 1 THEN 1 ELSE 0 END) AS multi
        FROM (SELECT requester_did, post_uri, COUNT(DISTINCT feed_rkey) AS n_feeds
              FROM served_posts WHERE served_at >= ? AND requester_did IS NOT NULL
              GROUP BY requester_did, post_uri)""", (since,))
    total = (row[0]["pairs"] or 0) if row else 0
    multi = (row[0]["multi"] or 0) if row else 0
    top = [dict(r) for r in q(con, """
        SELECT post_uri, COUNT(DISTINCT feed_rkey) AS feeds,
               COUNT(*) AS deliveries
        FROM served_posts WHERE served_at >= ? AND requester_did IS NOT NULL
        GROUP BY post_uri HAVING feeds > 1 ORDER BY feeds DESC, deliveries DESC
        LIMIT ?""", (since, limit))]
    return {"pairs": total, "multi_feed": multi,
            "share": pct(multi, total), "top": top}


def growth(con):
    members = {r["status"]: r["n"] for r in q(
        con, "SELECT status, COUNT(*) AS n FROM growth_members GROUP BY status")}
    votes = (q(con, "SELECT COUNT(*) AS n FROM growth_votes")[0]["n"]
             if q(con, "SELECT COUNT(*) AS n FROM growth_votes") else 0)
    promotions = {r["status"]: r["n"] for r in q(
        con, "SELECT status, COUNT(*) AS n FROM promotions GROUP BY status")}
    return {"members": members, "votes": votes, "promotions": promotions}


def build(con, config, days=7.0, now=None):
    if con.row_factory is not sqlite3.Row:
        con.row_factory = sqlite3.Row
    now = time.time() if now is None else now
    since = now - days * 86400
    targets = {f["rkey"]: int(f.get("max_posts") or 0)
               for f in (config.get("feeds") or [])}
    metrics = load_metrics(con, since)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "days": days,
        "feeds": {},
        "duplicates": duplicates(con, since),
        "growth": growth(con),
    }
    short = short_cycles(con, since, targets)
    read = delivered(con, since)
    owner = owner_share(con, since, config.get("publisher_did"))
    for rkey, m in metrics.items():
        s = short.get(rkey, {})
        rep = read.get(rkey, {})
        own = owner.get(rkey, {})
        report["feeds"][rkey] = {
            "cap": targets.get(rkey),
            "cycles": m["cycles"],
            "board_avg": m["board_avg"],
            "board_min": m["board_min"],
            "short_share": s.get("short_share"),
            "score_median": m["score_median"],
            "score_p90": m["score_p90"],
            "eng_median": m["eng_median"],
            "gini": m["gini"],
            "authors": m["authors"],
            "in_window": m["in_window"],
            "post_gate": m["post_gate"],
            "gate_share": m["gate_share"],
            "final": m["final"],
            "fill_share": m["fill_share"],
            "suppressed_share": m["suppressed_share"],
            "deliveries": rep.get("deliveries", 0),
            "readers": rep.get("readers", 0),
            "delivered_likes_avg": rep.get("likes_avg"),
            "zero_likes_share": rep.get("zero_likes_share"),
            "owner_share": own.get("owner_share"),
        }
    return report


def render(rep):
    lines = []
    add = lines.append
    add("feed estate — last %g days (since %s)" % (rep["days"], rep["generated_at"]))
    add("")
    hdr = ("%-20s %5s %6s %6s %6s %6s %6s %6s %5s %5s %6s %6s %6s %7s"
           % ("feed", "cap", "board", "min", "short%", "score", "p90", "eng",
              "gini", "auth", "gate%", "fill%", "owner%", "deliv"))
    add(hdr)
    add("-" * len(hdr))
    for rkey in sorted(rep["feeds"]):
        f = rep["feeds"][rkey]

        def g(k, d="–"):
            v = f.get(k)
            return d if v is None else (("%.1f" % v) if isinstance(v, float) else str(v))

        add("%-20s %5s %6s %6s %6s %6s %6s %6s %6s %5s %6s %6s %6s %7s"
            % (rkey, f.get("cap"), g("board_avg"), g("board_min"), g("short_share"),
               g("score_median"), g("score_p90"), g("eng_median"), g("gini"),
               g("authors"), g("gate_share"), g("fill_share"), g("owner_share"),
               g("deliveries")))
    add("")
    add("column notes: board/min = posts per cycle; short% = cycles below cap (the likes")
    add("floor relaxed); score/p90/eng = board medians; gini = engagement concentration;")
    add("gate% = in_window that passed the gates; fill% = gated posts that made the board;")
    add("owner% = share of deliveries written by the operator; deliv = deliveries in the")
    add("window, so a small deliv makes owner% noise rather than a policy leak.")
    add("")
    d = rep["duplicates"]
    add("cross-feed duplicates: %s of %s reader-post pairs were served by 2+ feeds (%s%%)"
        % (d["multi_feed"], d["pairs"], d["share"]))
    for t in d["top"][:5]:
        add("   %-3s feeds  %s" % (t["feeds"], t["post_uri"]))
    gr = rep["growth"]
    add("")
    add("growth pipeline: members %s, votes %s, promotions %s"
        % (gr["members"] or "{}", gr["votes"], gr["promotions"] or "{}"))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", default="")
    ap.add_argument("--txt", default="", help="also write the human report here")
    args = ap.parse_args(argv)
    config = json.load(open(os.path.expanduser(args.config), encoding="utf-8"))
    con = con_ro(args.db)
    rep = build(con, config, args.days)
    text = render(rep)
    print(text)
    if args.txt:
        with open(os.path.expanduser(args.txt), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("text -> %s" % args.txt)
    if args.json:
        with open(os.path.expanduser(args.json), "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
        print("\njson -> %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
