#!/usr/bin/env python3
"""The watchdog must catch the failures that look like health, and not invent others."""
import importlib.util
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "watchdog", os.path.join(HERE, "diagnostics", "watchdog.py"))
assert spec and spec.loader, "watchdog not importable"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

CFG = {"feeds": [{"rkey": "a", "max_posts": 100},
                 {"rkey": "b", "max_posts": 100}]}


def build(short="b"):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE metrics_log(ts REAL)")
    con.execute("CREATE TABLE board_scores(ts REAL, feed_rkey TEXT, pos INT, "
                "uri TEXT, score REAL)")
    now = time.time()
    con.execute("INSERT INTO metrics_log VALUES (?)", (now,))
    for rkey, n in (("a", 100), ("b", 40 if short == "b" else 100)):
        for i in range(n):
            con.execute("INSERT INTO board_scores VALUES (?,?,?,?,?)",
                        (now, rkey, i, "at://x/%s%d" % (rkey, i), 1.0))
    return con, now


con, now = build()
depth = wd.board_depth(con, CFG)
assert depth["a"]["share"] == 1.0 and depth["b"]["share"] == 0.4, depth
assert abs(wd.cycle_age(con, now) - 0) < 5, wd.cycle_age(con, now)
print("ok  board depth and cycle age are measured from the database")

# A short board is not a fault on its first observation.
f1 = wd.classify("active", depth, [])
assert [f["kind"] for f in f1] == ["board_short"] and not f1[0]["page"], f1
# ... nor on the second. On the third it pages.
hist = [{"findings": [{"kind": "board_short", "feed": "b"}]} for _ in range(2)]
f3 = wd.classify("active", depth, hist)
assert f3[0]["page"], f3
print("ok  a short board pages only after three consecutive observations")

# A dead unit pages immediately, whatever the boards say.
dead = wd.classify("failed", {}, [])
assert any(f["kind"] == "unit_not_active" and f["page"] for f in dead), dead
healthy = wd.classify("active", {"a": {"cap": 100, "served": 100, "share": 1.0}}, [])
assert healthy == [], healthy

# A curated board cannot be filled by ranking; it must not page.
cur = wd.classify("active", {"c": {"cap": 300, "served": 200, "share": 0.67,
                                   "curated": True}},
                  [{"findings": [{"kind": "board_short", "feed": "c"}]}] * 5)
assert [f["kind"] for f in cur] == ["board_short_curated"] and not cur[0]["page"], cur
print("ok  a curated board is reported, never paged")
print("ok  a dead unit pages at once; a healthy one says nothing")

# A stale cycle is a finding even when the unit looks active.
con2, _ = build()
con2.execute("DELETE FROM metrics_log")
con2.execute("INSERT INTO metrics_log VALUES (?)", (now - 3600,))
age = wd.cycle_age(con2, now)
assert age > wd.STALE_CYCLE_SECS, age
print("ok  an old cycle is visible as staleness (%.0f min)" % (age / 60))
print("ALL WATCHDOG CHECKS PASSED")
