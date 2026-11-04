#!/usr/bin/env python3
"""One maintenance pass, run from a systemd timer.

Why a separate process: interaction polling is network-bound (up to
`poll_budget` posts, two API calls each) and used to sit inside refresh(),
where every minute it spent delayed the next ingest. Ranking does not need it;
it only needs the events it produces to exist eventually.

    python3 maintenance.py            # one pass, exits
    python3 maintenance.py --status   # what a pass would do, no writes

Install as a user timer (see feedgen-maintenance.timer in the deployment).
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("FEEDGEN_CONFIG", os.path.join(HERE, "config.json"))

import feedgen as fg  # noqa: E402


def pass_once(verbose=True):
    """Interactions, gravity, requester likes, then retention."""
    out = {}
    t0 = time.monotonic()
    fg.poll_interactions()
    out["polled_s"] = round(time.monotonic() - t0, 1)
    fg.process_interactions()
    fg.apply_owner_gravity()
    try:
        out["requesters"] = fg.refresh_all_requester_likes()
    except Exception as e:                                 # noqa: BLE001
        out["requesters_error"] = str(e)[:120]
    out["settled"] = fg.settle_growth()
    now = time.time()
    if now - float(fg.meta_get("served_compacted_at", 0) or 0) > 86400:
        out["rolled_up"] = fg.compact_served(
            int(fg.CFG.get("served_keep_days", 30) or 30), now)
        fg.meta_set("served_compacted_at", now)
    out["pruned_seen"] = fg.prune_state(now)
    out["seconds"] = round(time.monotonic() - t0, 1)
    if verbose:
        print(f"[{fg.SVC_TAG}] maintenance: {json.dumps(out)}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        print(json.dumps({"config": os.environ.get("FEEDGEN_CONFIG"),
                          "db": fg.DB_PATH,
                          "last_poll": fg.meta_get("affinity_poll_at"),
                          "last_mine": fg.meta_get("growth_mine_at"),
                          "last_compaction": fg.meta_get("served_compacted_at")},
                         indent=1))
        return
    pass_once()


if __name__ == "__main__":
    main()
