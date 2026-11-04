"""A/B harness checks: grouping, ordering, intervals, and the honesty flags."""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fg-ab-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "ab.sqlite")

# Start from the deployment config so every key feedgen expects exists, then
# replace only the lineup with two feeds in one experiment group.
BASE_CFG = "/home/alvaro/feedgen/config.json"
cfg = json.load(open(BASE_CFG))
cfg["feeds"] = [
    {"rkey": "exp-a", "display_name": "A", "ab_group": "media",
     "max_age_hours": 48, "max_posts": 50},
    {"rkey": "exp-b", "display_name": "B", "ab_group": "media",
     "max_age_hours": 48, "max_posts": 50},
    {"rkey": "solo", "display_name": "Solo", "max_age_hours": 48, "max_posts": 50},
]
CFG_PATH = os.path.join(TMP, "config.json")
json.dump(cfg, open(CFG_PATH, "w"))
os.environ["FEEDGEN_CONFIG"] = CFG_PATH

sys.path.insert(0, HERE)
import ab_report as ab  # noqa: E402
import feedgen as fg  # noqa: E402

fg.migrate_schema()
con = fg.db()
NOW = time.time()

# exp-a: 100 serves, 10 likes (100/1k). exp-b: 100 serves, 3 likes (30/1k).
for i in range(100):
    con.execute("INSERT OR REPLACE INTO served_posts VALUES (?,?,?,?,0)",
                (f"did:plc:r{i % 10}", f"at://a/{i}", "exp-a", NOW - 3600))
    con.execute("INSERT OR REPLACE INTO served_posts VALUES (?,?,?,?,0)",
                (f"did:plc:r{i % 10}", f"at://b/{i}", "exp-b", NOW - 3600))
for i in range(10):
    con.execute("INSERT INTO interaction_events"
                "(post_uri, feed_rkey, kind, created_at, requester_did, processed) "
                "VALUES (?,?,?,?,?,0)", (f"at://a/{i}", "", "like", NOW - 600,
                                         f"did:plc:r{i}"))
for i in range(3):
    con.execute("INSERT INTO interaction_events"
                "(post_uri, feed_rkey, kind, created_at, requester_did, processed) "
                "VALUES (?,?,?,?,?,0)", (f"at://b/{i}", "", "like", NOW - 600,
                                         f"did:plc:r{i}"))

rows = ab.collect(7)
by_feed = {r["feed"]: r for r in rows}
assert by_feed["exp-a"]["per_1k"] == 100.0, by_feed["exp-a"]
assert by_feed["exp-b"]["per_1k"] == 30.0, by_feed["exp-b"]
assert by_feed["solo"]["group"] == "(ungrouped)"
assert by_feed["solo"]["serves"] == 0
print("grouping and rates ok")

group_media = [r for r in rows if r["group"] == "media"]
assert [r["feed"] for r in group_media] == ["exp-a", "exp-b"], group_media
print("ordering within a group ok")

# 10 likes is far too few to call a winner: both must be flagged.
assert all(not r["conclusive"] for r in group_media)
assert "inconclusive" in ab.summarise(rows)
print("inconclusive flag ok")

lo, hi = ab.poisson_ci(100, 1000)
assert (lo, hi) == (80.4, 119.6), (lo, hi)
assert ab.poisson_ci(0, 0) == (0.0, 0.0)
print("poisson interval ok:", (lo, hi))

solo = ab.summarise([r for r in rows if r["group"] == "(ungrouped)"])
assert "needs at least two feeds" not in solo, "ungrouped is not an experiment"
print("ungrouped not warned ok")

print("ALL A/B CHECKS PASSED")
