#!/usr/bin/env python3
"""A digest post must fit, must be real, and must not be the operator's own work."""
import importlib.util
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "digest_post", os.path.join(HERE, "diagnostics", "digest_post.py"))
assert spec and spec.loader, "digest module not importable"
dp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dp)

OWNER = "did:plc:owner"
CFG = {"owner": {"did": OWNER},
       "feeds": [{"rkey": "f1", "display_name": "Feed One"}]}


def build():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE posts(uri TEXT PRIMARY KEY, author_did TEXT, "
                "author_handle TEXT, text TEXT, like_count INT, indexed_at TEXT)")
    con.execute("CREATE TABLE board_scores(ts REAL, feed_rkey TEXT, pos INT, "
                "uri TEXT, score REAL)")
    now = time.time()
    rows = []
    for i in range(30):
        did = OWNER if i == 0 else "did:plc:author%d" % i
        handle = "owner.bsky.social" if i == 0 else "a%d.bsky.social" % i
        uri = "at://%s/app.bsky.feed.post/p%d" % (did, i)
        con.execute("INSERT INTO posts VALUES (?,?,?,?,?,?)",
                    (uri, did, handle, "post number %d about something" % i,
                     1000 - i, datetime.now(timezone.utc).isoformat()))
        rows.append((now, "f1", i, uri, 100 - i))
    con.executemany("INSERT INTO board_scores VALUES (?,?,?,?,?)", rows)
    con.commit()
    return con


con = build()
digest = dp.compose(CFG, con, since_days=7, top=3, max_boards=3, budget=290)
assert digest, "nothing composed from a populated board"
text = digest["text"]
assert len(text) <= 300, len(text)
assert dp.check(text) == [], dp.check(text)
assert OWNER not in [l["uri"].split("/")[2] for l in digest["links"]], \
    "the operator's own post was digested"
assert all(l["url"].startswith("https://bsky.app/profile/") for l in digest["links"])
assert "owner.bsky.social" not in text, "owner handle leaked into the digest"
print("ok  digest fits, excludes the operator, links resolve")

# A post that is too long must be reported, not silently published.
problems = dp.check("x" * 301)
assert problems and "post cap" in problems[0], problems
print("ok  an over-length digest is flagged")

# An empty estate produces no digest rather than an empty post.
empty = sqlite3.connect(":memory:")
empty.execute("CREATE TABLE posts(uri TEXT, author_did TEXT, author_handle TEXT, "
              "text TEXT, like_count INT, indexed_at TEXT)")
empty.execute("CREATE TABLE board_scores(ts REAL, feed_rkey TEXT, pos INT, "
              "uri TEXT, score REAL)")
assert dp.compose(CFG, empty) is None
print("ok  an empty estate composes nothing")
print("ALL DIGEST CHECKS PASSED")
