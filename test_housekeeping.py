"""Housekeeping checks: stats shape, the space guard, and a real VACUUM INTO
on a synthetic file."""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fg-house-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "h.sqlite")
os.environ.setdefault("FEEDGEN_CONFIG", "/home/alvaro/feedgen/config.json")
sys.path.insert(0, HERE)

import db_housekeeping as hk  # noqa: E402
import feedgen as fg  # noqa: E402

fg.migrate_schema()
con = fg.db()
NOW = time.time()
for i in range(500):
    con.execute("INSERT OR REPLACE INTO served_posts VALUES (?,?,?,?,0)",
                (f"did:plc:r{i % 7}", f"at://p/{i}", "f", NOW - i * 60))
con.executemany("INSERT INTO interaction_events"
                "(post_uri, feed_rkey, kind, created_at, requester_did, processed) "
                "VALUES (?,?,?,?,?,0)",
                [(f"at://p/{i}", "", "like", NOW - 60, f"did:plc:r{i % 7}")
                 for i in range(20)])

s = hk.stats()
assert s["path"].endswith("h.sqlite"), s
assert s["served_posts"] == 500, s
assert s["interaction_events"] == 20, s
assert "free_mb" in s and "reclaimable_pct" in s
print("stats ok:", {k: s[k] for k in ("file_mb", "free_mb", "served_posts")})

ok, free, need = hk.can_vacuum(s["path"], free_bytes=10)
assert not ok and need > free
ok2, _, _ = hk.can_vacuum(s["path"], free_bytes=10 ** 12)
assert ok2
print("space guard ok")

# Real compaction of a synthetic file: payload must survive, file must shrink
# only if there was bloat, and the counts must match exactly.
for i in range(400):
    con.execute("DELETE FROM served_posts WHERE post_uri=?", (f"at://p/{i}",))
con.execute("PRAGMA freelist_count")
before = hk.stats()
r = hk.vacuum_into(s["path"])
assert os.path.exists(r["compact"]), r
assert r["counts"]["served_posts"] == before["served_posts"], (r, before)
assert r["to_mb"] <= r["from_mb"] + 0.1, r
print("vacuum into ok:", r["from_mb"], "->", r["to_mb"], "MB",
      "counts", r["counts"])

# And the compact copy is a working database, not just a file.
c2 = sqlite3 = __import__("sqlite3").connect(r["compact"])
assert c2.execute("PRAGMA quick_check").fetchone()[0] == "ok"
c2.close()
print("compact copy is a database")

print("ALL HOUSEKEEPING CHECKS PASSED")
