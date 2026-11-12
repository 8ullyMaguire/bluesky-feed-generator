#!/usr/bin/env python3
"""What does taste_weight actually do? Replay the same corpus at several weights.

The value was set twice in one evening (0.6 -> 0.1) without a measurement, which
is the sort of thing that becomes folklore. select() is deterministic, so the same
posts and the same taste dictionary at two weights are a controlled experiment:

  * over  — how much of the board the weight changes (1.0 = identical)
  * gain/loss — posts that enter or leave relative to the baseline
  * score — mean relative change in the score of the posts that stay

Read-only against the live database (mode=ro), and the engine is imported against
a throwaway config/DB path so nothing in the deployment is touched.

  python3 diagnostics/tune_weight.py --feed leftist-es-hoy --weights 0 0.1 0.3 0.6
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
ENGINE = os.path.join(ENGINE_DIR, "feedgen.py")


def load_engine(config_path, tmp_db):
    os.environ["FEEDGEN_CONFIG"] = config_path
    os.environ["FEEDGEN_DB"] = tmp_db
    sys.path.insert(0, ENGINE_DIR)
    spec = importlib.util.spec_from_file_location("fg_tune", ENGINE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fg_tune"] = mod
    spec.loader.exec_module(mod)
    return mod


def taste_from_db(con):
    rows = con.execute("SELECT author_did, likes_count FROM taste").fetchall()
    top = max([r[1] for r in rows] or [1]) or 1
    return {d: n / top for d, n in rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", required=True)
    ap.add_argument("--db", default="/home/alvaro/.local/share/feedgen/feedgen.sqlite")
    ap.add_argument("--config", default="/mnt/disk-important/personal/documents/code/misc/feedgen/config.json")
    ap.add_argument("--weights", nargs="+", type=float, default=[0.0, 0.1, 0.3, 0.6])
    ap.add_argument("--limit", type=int, default=20000)
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="tunew-")
    cfg_copy = os.path.join(tmp, "config.json")
    shutil.copy(args.config, cfg_copy)
    fg = load_engine(cfg_copy, os.path.join(tmp, "t.sqlite"))

    con = sqlite3.connect("file:%s?mode=ro" % os.path.expanduser(args.db), uri=True)
    posts = [json.loads(r[0]) for r in con.execute(
        "SELECT record_json FROM posts ORDER BY indexed_at DESC LIMIT ?", (args.limit,))]
    taste = taste_from_db(con)
    # build_author_priors() returns (priors, list_median)
    priors, _median = fg.build_author_priors() if hasattr(fg, "build_author_priors") else ({}, 0.0)
    ctx = {"topic_affinity": {}, "topic_seeds": set(), "taste": taste,
           "topic_keywords": [k.lower() for k in fg.CFG.get("topic_keywords", [])],
           "author_priors": priors or {}, "topic_totals": {}, "owner_liked": set()}

    base = [f for f in fg.CFG["feeds"] if f["rkey"] == args.feed]
    if not base:
        sys.exit("no feed %r in the config" % args.feed)
    fcfg = dict(base[0])
    now = datetime.now(timezone.utc)
    owner = fg.feed_owner(fg.CFG.get("owner") or {}, fcfg)

    rows = []
    for w in args.weights:
        f = dict(fcfg, taste_weight=w)
        scored, st = fg.select(posts, f, now, owner, ctx, set())
        rows.append((w, scored, st))
    ref_w, ref_by_uri = rows[0][0], dict(rows[0][1])

    print("%d posts, feed %s, %d taste authors\n" % (len(posts), args.feed, len(taste)))
    print("%6s %6s %8s %8s %9s %9s" % ("weight", "board", "overlap", "churn", "mean dscore", "new authors"))
    for w, scored, st in rows:
        by_uri = dict(scored)
        keep = set(by_uri) & set(ref_by_uri)
        overlap = len(keep) / max(1, len(by_uri))
        churn = len(set(by_uri) ^ set(ref_by_uri))
        deltas = [(by_uri[u] - ref_by_uri[u]) / ref_by_uri[u]
                  for u in keep if ref_by_uri[u]]
        mean_d = sum(deltas) / len(deltas) if deltas else 0.0
        authors = {(p.get("author") or {}).get("did") for p in posts}
        print("%6.2f %6d %7.1f%% %8d %+8.1f%% %9d"
              % (w, len(scored), 100 * overlap, churn, 100 * mean_d,
                 st.get("final", len(scored))))
    print("\noverlap is against weight %.2f; a weight that barely moves the board is "
          "not doing what its comment claims." % ref_w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
