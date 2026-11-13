"""Weekly report arithmetic, on a throwaway DB with numbers checkable by hand.

The report is only useful if its columns mean what their names say, so every
assertion below is a hand-computed figure:

  feed a: two cycles, boards of 100 and 90 against a cap of 100 -> short 50%
          in_window 200+180, post_gate 120+100 -> gate share 220/380 = 57.9%
          engagement_median 10.0 and 20.0 -> avg 15.0
  deliveries: 3 to readers, 1 by the owner -> owner share 33.3%
  duplicates: one reader got the same post from two feeds -> 1 of 2 pairs = 50%
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "diagnostics"))
import weekly_report as wr  # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="fg-weekly-"), "w.sqlite")
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE metrics_log (ts REAL, feed_rkey TEXT, posts INTEGER,
  unique_authors INTEGER, median_age_hours REAL, score_median REAL,
  score_p90 REAL, kw_hit_rate REAL, no_text_rate REAL, affinity_p50 REAL,
  engagement_gini REAL, engagement_median REAL, in_window INTEGER,
  post_gate INTEGER, suppressed INTEGER, final INTEGER, served INTEGER);
CREATE TABLE posts (uri TEXT PRIMARY KEY, author_did TEXT, like_count INTEGER);
CREATE TABLE served_posts (requester_did TEXT, post_uri TEXT, feed_rkey TEXT,
  served_at REAL, polled_at REAL);
CREATE TABLE growth_members (author_did TEXT, status TEXT);
CREATE TABLE growth_votes (author_did TEXT);
CREATE TABLE promotions (did TEXT, status TEXT);
""")
now = time.time()
rows = [
    (now - 3600, "a", 100, 40, 5.0, 1.0, 2.0, 0.5, 0.1, 0.2, 0.35, 10.0, 200, 120, 10, 100, 0),
    (now - 60, "a", 90, 30, 6.0, 3.0, 4.0, 0.6, 0.1, 0.2, 0.40, 20.0, 180, 100, 20, 90, 0),
]
con.executemany("INSERT INTO metrics_log VALUES (%s)" % ",".join("?" * 17), rows)
con.executemany("INSERT INTO posts VALUES (?,?,?)",
                [("at://x/1", "did:plc:owner", 4), ("at://x/2", "did:plc:other", 9)])
con.executemany("INSERT INTO served_posts VALUES (?,?,?,?,0)",
                [("did:plc:r1", "at://x/1", "a", now - 120),
                 ("did:plc:r1", "at://x/2", "a", now - 110),
                 ("did:plc:r1", "at://x/2", "b", now - 100)])
con.executemany("INSERT INTO growth_members VALUES (?,?)",
                [("did:plc:g1", "probation"), ("did:plc:g2", "promoted")])
con.executemany("INSERT INTO growth_votes VALUES (?)", [("did:plc:g1",)])
con.executemany("INSERT INTO promotions VALUES (?,?)", [("did:plc:g2", "promoted")])
con.commit()

cfg = {"publisher_did": "did:plc:owner", "feeds": [{"rkey": "a", "max_posts": 100},
                                                   {"rkey": "b", "max_posts": 50}]}
rep = wr.build(con, cfg, days=7.0, now=now)
f = rep["feeds"]["a"]

assert f["cap"] == 100, f
assert f["cycles"] == 2, f
assert f["board_avg"] == 95.0 and f["board_min"] == 90, f
assert f["short_share"] == 50.0, f            # one of two cycles below the cap
assert f["gate_share"] == 57.9, f             # 220 / 380
assert f["eng_median"] == 15.0, f             # (10 + 20) / 2
assert f["fill_share"] == 86.4, f             # 190 / 220
assert f["deliveries"] == 2 and f["readers"] == 1, f
assert f["owner_share"] == 50.0, f            # one of two deliveries is the operator
assert rep["duplicates"]["multi_feed"] == 1, rep["duplicates"]
assert rep["duplicates"]["share"] == 50.0, rep["duplicates"]
assert rep["growth"]["members"] == {"probation": 1, "promoted": 1}, rep["growth"]
assert rep["growth"]["votes"] == 1 and rep["growth"]["promotions"] == {"promoted": 1}

txt = wr.render(rep)
assert "cross-feed duplicates: 1 of 2" in txt, txt
assert "feed" in txt and "cap" in txt
print("weekly report aggregation ok")

# Missing tables must degrade, not raise: older deployments have no growth_votes.
con2 = sqlite3.connect(":memory:")
con2.executescript("CREATE TABLE metrics_log (ts REAL, feed_rkey TEXT, posts INTEGER);")
empty = wr.build(con2, {"feeds": []}, days=1.0, now=now)
assert empty["feeds"] == {} and empty["duplicates"]["pairs"] == 0, empty
print("degrades on an empty database ok")

print("ALL WEEKLY REPORT CHECKS PASSED")
