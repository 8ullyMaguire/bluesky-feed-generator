"""Measurement layer: likes per 1,000 serves, funnel persistence, /metrics.json.

Runs against a throwaway SQLite so the numbers are checkable by hand:
  10 requesters x 20 serves = 200 serves, 5 likes inside the window -> 25.0/1k
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FEEDGEN_DB"] = os.path.join(tempfile.mkdtemp(prefix="fg-metrics-"), "m.sqlite")
os.environ.setdefault("FEEDGEN_CONFIG", "/home/alvaro/feedgen/config.json")
sys.path.insert(0, HERE)

import feedgen as fg  # noqa: E402

fg.migrate_schema()

NOW = time.time()
con = fg.db()


def serve(requester, uri, rkey="x", at=None):
    con.execute("INSERT OR REPLACE INTO served_posts"
                "(requester_did, post_uri, feed_rkey, served_at, polled_at) "
                "VALUES (?,?,?,?,0)", (requester, uri, rkey, NOW if at is None else at))


def liked(requester, uri, at=None):
    # feed_rkey is empty in real events: poll_interactions does not know it.
    con.execute("INSERT INTO interaction_events"
                "(post_uri, feed_rkey, kind, created_at, requester_did, processed) "
                "VALUES (?,?,?,?,?,0)", (uri, "", "like", NOW if at is None else at,
                                         requester))


# 200 serves of feed "x" by 10 requesters, spread over the last hour.
for r in range(10):
    for i in range(20):
        serve(f"did:plc:req{r}", f"at://post/{r}/{i}", at=NOW - 3600 - i * 60)
# Five likes, each by the requester who was served that post.
for r in range(5):
    liked(f"did:plc:req{r}", f"at://post/{r}/0")

x = fg.likes_per_1k(7, now_ts=NOW)["x"]
assert x["serves"] == 200, x
assert x["requesters"] == 10, x
assert x["likes"] == 5, x
assert x["per_1k"] == 25.0, x
print("likes_per_1k ok:", x)

# A like from someone who was never served that post must not be attributed.
liked("did:plc:stranger", "at://post/7/7")
assert fg.likes_per_1k(7, now_ts=NOW)["x"]["likes"] == 5
print("requester must have been served: ok")

# A post served three days ago and liked now is outside the 24h window.
serve("did:plc:req9", "at://post/old/1", at=NOW - 3 * 86400)
liked("did:plc:req9", "at://post/old/1")
x = fg.likes_per_1k(7, now_ts=NOW)["x"]
assert x["likes"] == 5, x
assert x["serves"] == 201, x
print("24h attribution window ok")

# Funnel persistence: the stages select() counts must survive into metrics_log.
fg.log_metrics({"x": ["at://post/1/1", "at://post/1/2"]}, {},
               NOW, stats={"x": {"in_window": 120, "pass_gates": 80,
                                 "suppressed": 11, "final": 2}})
row = con.execute(
    "SELECT in_window, post_gate, suppressed, final, served FROM metrics_log "
    "WHERE feed_rkey='x' ORDER BY ts DESC LIMIT 1").fetchone()
assert row[0] == 120 and row[1] == 80 and row[2] == 11 and row[3] == 2, row
assert row[4] == 201, row          # serves since the previous row for this feed
print("funnel persisted ok:", row)

cols = {r[1] for r in con.execute("PRAGMA table_info(metrics_log)")}
assert {"in_window", "post_gate", "suppressed", "final", "served"} <= cols
fg.migrate_schema()
print("migration idempotent ok")

print("ALL METRICS CHECKS PASSED")
