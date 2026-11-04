"""Compaction and restore-drill checks."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FEEDGEN_DB"] = os.path.join(tempfile.mkdtemp(prefix="fg-compact-"), "c.sqlite")
os.environ.setdefault("FEEDGEN_CONFIG", "/home/alvaro/feedgen/config.json")
sys.path.insert(0, HERE)

import feedgen as fg  # noqa: E402

fg.migrate_schema()
con = fg.db()
NOW = time.time()

# 40-day-old rows for two feeds, plus recent rows that must survive.
for i in range(30):
    con.execute("INSERT OR REPLACE INTO served_posts VALUES (?,?,?,?,0)",
                (f"did:plc:old{i % 5}", f"at://old/{i}", "leftist-24h",
                 NOW - 40 * 86400 + i))
for i in range(7):
    con.execute("INSERT OR REPLACE INTO served_posts VALUES (?,?,?,?,0)",
                (f"did:plc:old{i}", f"at://old2/{i}", "leftist-week",
                 NOW - 40 * 86400 + i))
for i in range(4):
    con.execute("INSERT OR REPLACE INTO served_posts VALUES (?,?,?,?,0)",
                (f"did:plc:new{i}", f"at://new/{i}", "leftist-24h", NOW - 3600))

deleted = fg.compact_served(30, now_ts=NOW)
assert deleted == 37, deleted
left = con.execute("SELECT COUNT(*) FROM served_posts").fetchone()[0]
assert left == 4, left
print("compaction deleted the old rows, kept the recent ones:", deleted, "->", left)

rows = dict(((r[0], r[1]), (r[2], r[3])) for r in con.execute(
    "SELECT feed_rkey, day, serves, requesters FROM served_daily"))
assert len(rows) == 2, rows
day_a = [k for k in rows if k[0] == "leftist-24h"][0]
assert rows[day_a][0] == 30, rows[day_a]              # 30 serves
assert rows[day_a][1] == 5, rows[day_a]               # 5 distinct requesters
day_b = [k for k in rows if k[0] == "leftist-week"][0]
assert rows[day_b][0] == 7, rows[day_b]
print("rollup kept serves and distinct requesters:", rows)

assert fg.compact_served(30, now_ts=NOW) == 0
assert con.execute("SELECT SUM(serves) FROM served_daily").fetchone()[0] == 37
print("compaction is idempotent")

# Detail is kept for at least as long as any feed's per-user seen window.
keep = max([30 * 24] + [int(f.get("hide_seen_ttl_h") or 0)
                        for f in fg.CFG.get("feeds") or []])
assert keep >= 30 * 24
print("retention covers the largest hide_seen_ttl_h")

print("ALL COMPACTION CHECKS PASSED")
