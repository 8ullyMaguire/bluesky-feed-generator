#!/usr/bin/env python3
"""Tests for the additive content sources and the seen-marking knob.

Covers the three things a future edit can silently break:

  * `fetch_ext_feeds()` / `fetch_search_posts()` must tag every post they
    contribute, keep their own cadence, and never raise into refresh().
  * `select()` must honour `_src_mult` / `_src_boost`, so a contributor can be
    ranked below list members without being banned.
  * `mark_seen()` is the one writer of the per-feed suppression window; the
    `seen_mark` knob decides whether refresh writes it at all.

Everything runs against a throwaway SQLite file with a stubbed `rpc`, so no
network call is made and the live deployments are untouched.
Run: python3 test_sources.py
"""
import importlib.util
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="feedgen-sources-test-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")
os.environ["FEEDGEN_STATE_PATH"] = os.path.join(TMP, "no-legacy.json")
if not os.environ.get("FEEDGEN_CONFIG"):
    os.environ["FEEDGEN_CONFIG"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.example.json")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("fg_sources", os.path.join(HERE, "feedgen.py"))
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)  # type: ignore[union-attr]


def mkpost(uri, did, likes=1, reposts=0, hours=1.0):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"uri": uri, "cid": "c", "indexedAt": ts,
            "author": {"did": did, "handle": did.split(":")[-1]},
            "record": {"text": "hello", "createdAt": ts},
            "likeCount": likes, "repostCount": reposts,
            "quoteCount": 0, "replyCount": 0}


def set_cfg(**kw):
    old = {k: fg.CFG.get(k) for k in kw}
    fg.CFG.update(kw)
    return old


def restore(old):
    for k, v in old.items():
        if v is None:
            fg.CFG.pop(k, None)
        else:
            fg.CFG[k] = v


checks = 0


def ok(cond, label):
    global checks
    assert cond, label
    checks += 1


# ---------------------------------------------------------------- ext feeds

calls = []


def fake_rpc_ok(host, method, data=None, token=None, timeout=30):
    calls.append((host, method))
    if "getFeedSkeleton" in method:
        return {"feed": [{"post": "at://did:plc:a/app.bsky.feed.post/1"},
                         {"post": "at://did:plc:b/app.bsky.feed.post/2"}]}
    if "getPosts" in method:
        return {"posts": [mkpost("at://did:plc:a/app.bsky.feed.post/1", "did:plc:a"),
                          mkpost("at://did:plc:b/app.bsky.feed.post/2", "did:plc:b")]}
    raise AssertionError("unexpected method " + method)


old = set_cfg(ext_feeds=[{"name": "demo", "uri": "at://x/app.bsky.feed.generator/demo",
                          "endpoint": "https://feeds.example", "limit": 10,
                          "score_mult": 0.6, "boost": 0.25, "interval_secs": 0}])
real_rpc = fg.rpc
fg.rpc = fake_rpc_ok
try:
    posts, stats = fg.fetch_ext_feeds()
    ok(stats["demo"] == 2, "ext feed counts fetched posts")
    ok(len(posts) == 2, "ext feed returns the posts")
    ok(all(p.get("_extfeed_src") == "demo" for p in posts), "ext posts are tagged")
    ok(all(abs(p.get("_src_mult") - 0.6) < 1e-9 for p in posts), "ext mult applied")
    ok(all(abs(p.get("_src_boost") - 0.25) < 1e-9 for p in posts), "ext boost applied")

    # cadence: an interval in the future must skip the source entirely
    fg.CFG["ext_feeds"][0]["interval_secs"] = 3600
    fg._src_mark("ext:demo")
    posts2, stats2 = fg.fetch_ext_feeds()
    ok(posts2 == [] and stats2["demo"] == "skipped", "ext cadence skips when not due")

    # a dead community feed is logged, not raised
    def fake_rpc_dead(host, method, data=None, token=None, timeout=30):
        raise RuntimeError("boom")

    fg.CFG["ext_feeds"][0]["interval_secs"] = 0
    fg.rpc = fake_rpc_dead
    posts3, stats3 = fg.fetch_ext_feeds()
    ok(posts3 == [] and stats3["demo"] == -1, "a failing ext feed never breaks refresh")

    # no config block -> no calls at all
    fg.CFG.pop("ext_feeds")
    fg.rpc = fake_rpc_ok
    before = len(calls)
    posts4, stats4 = fg.fetch_ext_feeds()
    ok(posts4 == [] and stats4 == {} and len(calls) == before,
       "ext feeds are off unless configured")
finally:
    fg.rpc = real_rpc
    restore(old)

# ---------------------------------------------------------------- search

old = set_cfg(search_source={"enabled": False, "terms": ["x"]})
try:
    ok(fg.fetch_search_posts("tok")[0] == [], "search is off unless enabled")
finally:
    restore(old)

search_config = {"enabled": True, "terms": ["demo"], "limit": 10, "pages": 1,
                 "max_age_hours": 24, "score_mult": 0.5, "interval_secs": 0,
                 "host": "https://pds.example"}
old = set_cfg(search_source=search_config)


def fake_rpc_search(host, method, data=None, token=None, timeout=30):
    assert token == "tok", "search must use the session token"
    assert "searchPosts" in method
    return {"posts": [mkpost("at://did:plc:s/app.bsky.feed.post/1", "did:plc:s", hours=1),
                      mkpost("at://did:plc:t/app.bsky.feed.post/2", "did:plc:t", hours=48)],
            "cursor": None}


real_rpc = fg.rpc
fg.rpc = fake_rpc_search
try:
    posts, hits = fg.fetch_search_posts("tok")
    ok(len(posts) == 1, "search drops posts older than max_age_hours")
    ok(posts[0].get("_search_src") == "demo", "search posts are tagged with the term")
    ok(abs(posts[0].get("_src_mult") - 0.5) < 1e-9, "search mult applied")
    ok(hits["demo"] == 1, "search reports per-term hits")
finally:
    fg.rpc = real_rpc
    restore(old)

# ---------------------------------------------------------------- select

FCFG = {"rkey": "t", "max_age_hours": 48, "max_posts": 10, "min_likes": 0,
        "min_reposts_share": 0, "max_per_author": 0, "seen_ttl_hours": 0,
        "ranking": "flat"}
CTX = {"topic_affinity": {}, "taste": {}, "topic_seeds": set(),
       "topic_keywords": [], "author_priors": {}}
from datetime import datetime, timezone

p_list = mkpost("at://did:plc:list/app.bsky.feed.post/1", "did:plc:list", likes=5)
p_src = mkpost("at://did:plc:src/app.bsky.feed.post/2", "did:plc:src", likes=5)
p_src["_src_mult"] = 0.5
picked, _st = fg.select([p_src, p_list], FCFG, datetime.now(timezone.utc),
                        {}, CTX, set())
ok([u for u, _s in picked] == ["at://did:plc:list/app.bsky.feed.post/1",
                               "at://did:plc:src/app.bsky.feed.post/2"],
   "select ranks a multiplied source below a list member with equal engagement")

p_boost = mkpost("at://did:plc:src/app.bsky.feed.post/3", "did:plc:src", likes=5)
p_boost["_src_boost"] = 50.0
picked, _st = fg.select([p_boost, p_list], FCFG, datetime.now(timezone.utc),
                        {}, CTX, set())
ok([u for u, _s in picked][0] == "at://did:plc:src/app.bsky.feed.post/3",
   "select honours an additive source boost")

# ---------------------------------------------------------------- seen marking

before = fg.db().execute("SELECT COUNT(*) FROM seen").fetchone()[0]
fg.mark_seen("r1", ["u1", "u2"], now_ts=time.time())
fg.mark_seen("r1", ["u1"], now_ts=time.time())
after = fg.db().execute("SELECT COUNT(*) FROM seen").fetchone()[0]
ok(after - before == 2, "mark_seen writes one row per uri, not per call")
cnt = fg.db().execute("SELECT shown_count FROM seen WHERE rkey='r1' AND uri='u1'").fetchone()[0]
ok(cnt == 2, "mark_seen bumps shown_count on repeat")

ok(isinstance(fg.CFG.get("seen_mark", "refresh"), str), "seen_mark defaults are sane")

print(f"ALL SOURCES CHECKS PASSED ({checks} assertions)")
