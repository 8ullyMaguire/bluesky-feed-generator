#!/usr/bin/env python3
"""Detect the corpus cold-start that follows every restart, per feed.

Why: CORPUS is module state, so a restart begins with whatever
`corpus_seed` can load (and an empty corpus on a deployment that leaves the
key out). The first refresh then ranks a thin crawl and *every* supply-bound
board collapses — on 2026-11-12 `in_window` fell 22,298 -> 7,772 and
`leftist-3d` went 313 -> 167 while `posts` kept growing. That is
indistinguishable from a config regression or a broken crawl unless the
drop is measured against a control feed.

How: read `metrics_log`, find step changes in `in_window` inside the window,
and classify them. A drop that hits every feed at once is a corpus event; a
drop confined to one feed is that feed's own problem. A feed capped by
`max_posts` (final >= its cap) is unaffected in practice — it refills from
the same crawl — so it is reported but not counted as damage.

Exit 1 when a supply-bound board lost more than --threshold of its depth, so
a timer run surfaces it in the journal. Delivery to a human is a channel
decision the estate has not made yet; this writes a JSON report and a
one-line summary instead of pretending to alert.

Usage:
  python3 diagnostics/restart_damage.py --db ~/.local/share/feedgen/feedgen.sqlite \\
      --config config.json [--hours 24] [--threshold 0.4] [--json out.json]
"""
import argparse
import json
import os
import sqlite3
import sys
import time


def rows_since(con, since):
    return con.execute(
        "SELECT ts, feed_rkey, in_window, post_gate, final FROM metrics_log "
        "WHERE ts >= ? ORDER BY feed_rkey, ts", (since,)).fetchall()


def caps_from_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return {f["rkey"]: int(f.get("max_posts", 0) or 0) for f in cfg["feeds"]}


def find_steps(rows, threshold):
    """Consecutive-row drops larger than `threshold` of the preceding value."""
    out = []
    prev = {}
    for ts, rkey, inw, gate, final in rows:
        p = prev.get(rkey)
        prev[rkey] = (ts, inw or 0)
        if not p or not p[1]:
            continue
        before, after = p[1], (inw or 0)
        if before >= 500 and after < before * (1 - threshold):
            out.append({
                "ts": ts, "feed": rkey, "before": before, "after": after,
                "pct": round(100.0 * (after - before) / before, 1),
                "post_gate": gate, "final": final,
            })
    return out


def settled_final(rows, rkey, ts, horizon=900):
    """The board this feed held once the event finished, not at its first row.

    A restart's first refresh often still writes the board it persisted before
    the drop, so the row where in_window collapses is a poor depth reading:
    leftist-3d showed 313 there and 167 ten minutes later.
    """
    last = None
    for rts, rk, _inw, _gate, final in rows:
        if rk == rkey and ts <= rts <= ts + horizon:
            last = final
    return last


def latest_final(rows):
    """The board each feed is serving now — the only value that is still a bug."""
    out = {}
    for _ts, rkey, _inw, _gate, final in rows:
        out[rkey] = final or 0
    return out


def group_events(steps, gap_secs=600):
    """Steps within `gap_secs` of each other across >=2 feeds are one event."""
    events = []
    for s in sorted(steps, key=lambda x: x["ts"]):
        if events and s["ts"] - events[-1]["ts"] <= gap_secs:
            events[-1]["feeds"].append(s)
            events[-1]["ts"] = s["ts"]
        else:
            events.append({"ts": s["ts"], "feeds": [s]})
    return events


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    caps = caps_from_config(args.config)
    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(args.db),
                          uri=True)
    since = time.time() - args.hours * 3600
    rows = rows_since(con, since)
    steps = find_steps(rows, args.threshold)
    events = group_events(steps)
    now_final = latest_final(rows)

    report = {"generated": int(time.time()), "hours": args.hours,
              "threshold": args.threshold, "events": []}
    damage = 0
    for ev in events:
        feeds = ev["feeds"]
        # A feed whose board still sits at its cap after the event lost nothing
        # a reader can see; a feed well below its cap lost real depth.
        detail = []
        for f in feeds:
            cap = caps.get(f["feed"]) or 0
            final = settled_final(rows, f["feed"], ev["ts"]) or f["final"] or 0
            # Damage is what is *still* missing: a board that has since refilled
            # is history. Reporting history every hour for the whole window is
            # how a check earns its way onto the ignore list.
            healed = bool(cap) and now_final.get(f["feed"], 0) >= 0.9 * cap
            detail.append((f, final, cap, healed))
        capped = [f["feed"] for f, _fin, _cap, at_cap in detail if at_cap]
        supply_bound = [f for f, _fin, _cap, healed in detail if not healed]
        kind = "corpus" if len(feeds) >= 3 else "single-feed"
        report["events"].append({
            "ts": ev["ts"],
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["ts"])),
            "kind": kind,
            "feeds": [{"feed": f["feed"], "before": f["before"],
                       "after": f["after"], "pct": f["pct"],
                       "settled_final": final, "max_posts": cap,
                       "now": now_final.get(f["feed"], 0),
                       "recovered": at_cap}
                      for f, final, cap, at_cap in detail],
        })
        if supply_bound:
            damage += len(supply_bound)
        print("%s %-10s %d feeds: %s" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["ts"])), kind,
            len(feeds),
            ", ".join("%s %d->%d (%+d%%) board=%s/%s%s" % (
                f["feed"], f["before"], f["after"], f["pct"], final, cap or "?",
                " recovered" if at_cap else " [depth lost]")
                for f, final, cap, at_cap in detail)))

    if args.json:
        with open(os.path.expanduser(args.json), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")

    if not events:
        print("no corpus step change in the last %.0f h" % args.hours)
        return 0
    if damage:
        print("DAMAGE: %d supply-bound board(s) lost depth — check whether the "
              "corpus refilled before blaming the config" % damage)
        return 1
    print("every drop has since refilled to its cap: history, not damage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
