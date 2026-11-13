#!/usr/bin/env python3
"""What does the owner bonus actually do to a board? Replay both policies.

The old policy was a flat `+3.0`; measured against the live boards it sat at or
above the 90th percentile of every velocity board (p90 2.5-3.4) and below the
median of the flat Spanish boards (4.2). The new one pushes by `0.3 x the median
non-owner score`, which should put the operator a fixed fraction above the middle
of every board instead of at the top of some and the middle of others.

select() is deterministic, so the same corpus under two owner blocks is a
controlled experiment. Read-only against the live database.

  python3 diagnostics/compare_owner.py --db ~/.local/share/feedgen/feedgen.sqlite \\
      --config ~/feedgen/config.json [--limit 15000] [--feeds leftist-24h ...]
"""
import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

ENGINE_DIR = "/mnt/disk-important/personal/documents/code/python/bluesky-feed-generator"


def load_engine(config_path, tmp_db):
    os.environ["FEEDGEN_CONFIG"] = config_path
    os.environ["FEEDGEN_DB"] = tmp_db
    sys.path.insert(0, ENGINE_DIR)
    spec = importlib.util.spec_from_file_location(
        "fg_owner", os.path.join(ENGINE_DIR, "feedgen.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fg_owner"] = mod
    spec.loader.exec_module(mod)
    return mod


def taste_from_db(con):
    rows = con.execute("SELECT author_did, likes_count FROM taste").fetchall()
    top = max([r[1] for r in rows] or [1]) or 1
    return {d: n / top for d, n in rows}


def pct_rank(values, v):
    if not values:
        return None
    below = sum(1 for x in values if x < v)
    return 100.0 * below / len(values)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/alvaro/.local/share/feedgen/feedgen.sqlite")
    ap.add_argument("--config", default="/mnt/disk-important/personal/documents/code/misc/feedgen/config.json")
    ap.add_argument("--limit", type=int, default=15000)
    ap.add_argument("--feeds", nargs="*")
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="owner-cmp-")
    cfg_copy = os.path.join(tmp, "config.json")
    shutil.copy(args.config, cfg_copy)
    fg = load_engine(cfg_copy, os.path.join(tmp, "t.sqlite"))

    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(args.db), uri=True)
    posts = [json.loads(r[0]) for r in con.execute(
        "SELECT record_json FROM posts ORDER BY indexed_at DESC LIMIT ?", (args.limit,))]
    ctx = {"topic_affinity": {}, "topic_seeds": set(), "taste": taste_from_db(con),
           "topic_keywords": [k.lower() for k in fg.CFG.get("topic_keywords", [])],
           "author_priors": {}, "topic_totals": {}, "owner_liked": set()}
    try:
        priors, _m = fg.build_author_priors()
        ctx["author_priors"] = priors or {}
    except Exception:
        pass
    # Topic affinity matters for the topical feeds; load it if the deployment has it.
    try:
        ctx["topic_affinity"] = {d: a for d, a in con.execute(
            "SELECT author_did, affinity FROM author_topic")}
    except sqlite3.Error:
        pass

    now = datetime.now(timezone.utc)
    feeds = [f for f in fg.CFG["feeds"] if not args.feeds or f["rkey"] in args.feeds]
    print("%d posts, %d feeds\n" % (len(posts), len(feeds)))
    print("%-20s %11s %11s  %s" % ("feed", "sold abs", "new rel", "owner percentile / slots"))
    print("-" * 78)
    for fcfg in feeds:
        if fcfg.get("require_owner_liked") and not ctx.get("owner_liked"):
            print("%-20s n/a — needs the owner's like list, which the replay does not load"
                  % fcfg["rkey"])
            continue
        owner_did = (fg.CFG.get("owner") or {}).get("did")
        base = fg.feed_owner(fg.CFG.get("owner") or {}, fcfg)
        abs_block = dict(base, bonus_mode="absolute", bonus=3.0)
        rel_block = dict(base, bonus_mode="relative", bonus_frac=0.3, bonus=0.0)
        out = {}
        for label, blk in (("abs", abs_block), ("rel", rel_block)):
            scored, _st = fg.select(posts, fcfg, now, blk, ctx, set())
            by_uri = dict(scored)
            own = [s for u, s in scored if owner_did and u.startswith("at://" + owner_did + "/")]
            others = [s for u, s in scored if not (owner_did and u.startswith("at://" + owner_did + "/"))]
            pos = [i for i, (u, _s) in enumerate(scored)
                   if owner_did and u.startswith("at://" + owner_did + "/")]
            out[label] = {
                "board": len(scored), "owner": len(own),
                "share": round(100.0 * len(own) / max(1, len(scored)), 1),
                "pct": (round(sum(pct_rank(others, s) for s in own) / len(own))
                        if own and others else None),
                "median_pos": int(sorted(pos)[len(pos) // 2]) if pos else None,
            }
        a, r = out["abs"], out["rel"]
        print("%-20s %4d/%3d%% %4d/%3d%%  abs p%02d pos %-4s | rel p%02d pos %-4s"
              % (fcfg["rkey"], a["board"], a["share"], r["board"], r["share"],
                 a["pct"] or 0, a["median_pos"], r["pct"] or 0, r["median_pos"]))
    print("\npXX = the owner post's percentile among non-owner scores on the same board;")
    print("pos = median position. A board where rel lands at p90 is still a takeover;")
    print("one where it lands near p60 is a thumb on the scale.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
