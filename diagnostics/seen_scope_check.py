#!/usr/bin/env python3
"""Is the seen memory shared across the feeds of this service, and how much?

Seen memory belongs to the SERVICE (one deployment = one database): a post
served in any feed is hidden from every other feed of the same service, bounded
by `hide_seen_ttl_h`. This prints, for the requesters with the most served posts
in the live DB, how many of their current exclusions were served by the feed
being asked (`same_feed`) and how many came from OTHER feeds of the same service
(`from_other_feeds`). `from_other_feeds > 0` is the proof the sharing is live;
with per-feed seen memory every exclusion would be same_feed.

Read-only apart from the module import's idempotent DDL.

  python3 diagnostics/seen_scope_check.py                # defaults to feedgen.py
  python3 diagnostics/seen_scope_check.py [requester_did]
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.normpath(os.path.join(HERE, ".."))
TARGET = sys.argv[1] if len(sys.argv) > 1 else "feedgen.py"
ONLY = sys.argv[2] if len(sys.argv) > 2 else None

sys.path.insert(0, SVC)
spec = importlib.util.spec_from_file_location("svc", os.path.join(SVC, TARGET))
svc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(svc)  # type: ignore[union-attr]

ttl = svc.CFG.get("hide_seen_ttl_h", 24)
floor = svc.CFG.get("hide_min_board", 20)
cutoff = time.time() - ttl * 3600
print(f"service={TARGET} db={svc.DB_PATH}")
print(f"feeds={len(svc.CFG['feeds'])} hide_seen_ttl_h={ttl} hide_min_board={floor}")

if ONLY:
    reqs = [ONLY]
else:
    reqs = [r[0] for r in svc.db().execute(
        "SELECT requester_did, COUNT(*) AS c FROM served_posts "
        "WHERE served_at >= ? GROUP BY requester_did "
        "ORDER BY c DESC LIMIT 3", (cutoff,)).fetchall()]

if not reqs:
    print("no requester has served posts inside the TTL window yet")
    sys.exit(0)

owner = (svc.CFG.get("owner") or {}).get("did")
for req in reqs:
    rows = svc.db().execute(
        "SELECT feed_rkey, post_uri FROM served_posts "
        "WHERE requester_did=? AND served_at >= ?", (req, cutoff)).fetchall()
    seen = {(fr, u) for (fr, u) in rows}
    excl = svc.user_excluded_uris(req, True, protect_did=owner)
    seen_excl = {u for (_, u) in seen if u in excl}
    print(f"\nrequester {req}")
    print(f"  served_rows={len(seen)} (inside ttl)  excluded_total={len(excl)} "
          f"of_which_seen={len(seen_excl)}")
    for f in svc.CFG["feeds"]:
        rkey = f["rkey"]
        same = len({u for (fr, u) in seen if fr == rkey and u in excl})
        cross = len({u for (fr, u) in seen if fr != rkey and u in excl})
        mark = "shared" if cross else "same-feed-only"
        print(f"  {rkey:24s} excluded_seen={len(seen_excl):4d} "
              f"(same_feed={same:4d} from_other_feeds={cross:4d})  {mark}")
