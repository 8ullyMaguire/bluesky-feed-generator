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

# An allowlist decides which boards may be digested; a name that matches nothing
# composes nothing rather than falling back to everything.
only = dp.compose(CFG, con, only=["f1"])
assert only and only["feeds"] == ["f1"], only
assert dp.compose(CFG, con, only=["not-a-board"]) is None
print("ok  the digest allowlist is respected in both directions")

# An empty estate produces no digest rather than an empty post.
empty = sqlite3.connect(":memory:")
empty.execute("CREATE TABLE posts(uri TEXT, author_did TEXT, author_handle TEXT, "
              "text TEXT, like_count INT, indexed_at TEXT)")
empty.execute("CREATE TABLE board_scores(ts REAL, feed_rkey TEXT, pos INT, "
              "uri TEXT, score REAL)")
assert dp.compose(CFG, empty) is None
print("ok  an empty estate composes nothing")
# Facet offsets are UTF-8 byte offsets, not character offsets. The digest text has
# emoji and accents by construction; counting characters puts every link three bytes
# early and the post renders as text with a broken link in it.
text = "♥ á https://bsky.app/profile/a.bsky.social/post/x"
facets = dp.facets_for(text)
assert len(facets) == 1, facets
b = text.encode("utf-8")
i = facets[0]["index"]
assert b[i["byteStart"]:i["byteEnd"]].decode() == \
    "https://bsky.app/profile/a.bsky.social/post/x", i
assert i["byteStart"] == len(text[:text.index("http")].encode()), i
print("ok  link facets use byte offsets across multibyte text")

# Trailing punctuation belongs to the sentence, not the URL.
f2 = dp.facets_for("see https://example.com/x).")
assert f2[0]["features"][0]["uri"] == "https://example.com/x", f2
print("ok  trailing punctuation is not part of the link")

# The same digest must not go out twice in a week; a different one must.
import tempfile, time as _t, json as _j
with tempfile.TemporaryDirectory() as td:
    st = os.path.join(td, "s.json")
    assert dp.already_posted(st, "hello") is None
    with open(st, "w") as fh:
        _j.dump({"last": {"hash": dp.text_hash("hello"), "ts": _t.time()}}, fh)
    assert dp.already_posted(st, "hello") is not None
    assert dp.already_posted(st, "different") is None
    with open(st, "w") as fh:
        _j.dump({"last": {"hash": dp.text_hash("hello"),
                          "ts": _t.time() - 8 * 86400}}, fh)
    assert dp.already_posted(st, "hello") is None, "an old digest may repeat"
print("ok  a repeated digest is refused inside the week, allowed after")

print("ALL DIGEST CHECKS PASSED")
