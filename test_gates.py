#!/usr/bin/env python3
"""Tests for the max_likes upper-bound gate in select().

max_likes is the only gate in the codebase that is a CEILING rather than a
floor: every other gate keeps a post when its number is high enough, and this
one drops a post when its number is too high. That inversion is the entire
small-accounts feed, so it needs its own regression guard — a future edit that
turns it into a floor, or drops it, is invisible in every other test.

Runs against a throwaway SQLite file; the real feedgen.sqlite is untouched.
Run: python3 test_gates.py
"""
import importlib.util
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="feedgen-gate-test-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")
os.environ["FEEDGEN_STATE_PATH"] = os.path.join(TMP, "no-legacy.json")
if not os.environ.get("FEEDGEN_CONFIG"):
    os.environ["FEEDGEN_CONFIG"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.example.json")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("fg_gates", os.path.join(HERE, "feedgen.py"))
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)  # type: ignore[union-attr]

OWNER = {"did": "did:plc:owner", "handle": "owner.test", "always_include": False}
CTX = {"topic_affinity": {}, "taste": {}, "topic_seeds": set(),
       "topic_keywords": [], "author_priors": {}}


def mkpost(uri, did, likes, reposts=0, hours=1.0):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"uri": f"at://{did}/app.bsky.feed.post/{uri}", "author": {"did": did},
            "record": {"text": "x", "createdAt": ts}, "indexedAt": ts,
            "likeCount": likes, "repostCount": reposts,
            "quoteCount": 0, "replyCount": 0}


def test_max_likes_drops_amplified_posts():
    """max_likes: 20 keeps a 20-like post and drops a 500-like one."""
    fcfg = {"rkey": "t", "max_likes": 20, "min_likes": 0, "min_reposts_share": 0,
            "min_score": 0, "ranking": "flat", "max_posts": 100, "max_age_hours": 720}
    posts = [mkpost("small", "did:plc:quiet", 3),
             mkpost("edge", "did:plc:edge", 20),
             mkpost("big", "did:plc:loud", 500)]
    out, st = fg.select(posts, fcfg, datetime.now(timezone.utc), OWNER, CTX, set())
    uris = {u.rsplit("/", 1)[-1] for u, _s in out}
    assert "small" in uris and "edge" in uris, uris
    assert "big" not in uris, f"max_likes: 20 let a 500-like post through: {uris}"
    assert st["over_max_likes"] == 1, st
    print("ok   max_likes is an upper bound: 500 likes dropped, 20 kept")


def test_no_max_likes_means_no_ceiling():
    """A feed without the key is unaffected — this must not become a default."""
    fcfg = {"rkey": "t", "min_likes": 0, "min_reposts_share": 0,
            "min_score": 0, "ranking": "flat", "max_posts": 100, "max_age_hours": 720}
    posts = [mkpost("big", "did:plc:loud", 500)]
    out, st = fg.select(posts, fcfg, datetime.now(timezone.utc), OWNER, CTX, set())
    assert len(out) == 1, out
    assert st["over_max_likes"] == 0, st
    print("ok   a feed without max_likes keeps every post (no implicit default)")


def test_max_likes_is_exclusive_at_the_boundary():
    """21 is over a 20 ceiling, 20 is not. Off-by-one here is the whole test."""
    for likes, want in ((19, True), (20, True), (21, False)):
        fcfg = {"rkey": "t", "max_likes": 20, "min_likes": 0, "min_reposts_share": 0,
                "min_score": 0, "ranking": "flat", "max_posts": 100, "max_age_hours": 720}
        out, _st = fg.select([mkpost("p", "did:plc:a", likes)], fcfg,
                            datetime.now(timezone.utc), OWNER, CTX, set())
        assert bool(out) is want, (likes, out)
    print("ok   the boundary is inclusive: <= max_likes keeps, > drops")


def test_gate_report_and_select_agree_on_max_likes():
    """The explanation and the ranker must not disagree about a ceiling."""
    if getattr(fg, "discovery", None) is None:
        print("skip  discovery module not importable; gate report untested")
        return
    p = mkpost("p", "did:plc:a", 500)
    view = {"likeCount": p["likeCount"], "repostCount": p["repostCount"]}
    g = fg.discovery.gate_report(view, {"max_likes": 20}, 99.0)
    assert g["max_likes"]["active"] and g["max_likes"]["pass"] is False, g
    fcfg = {"rkey": "t", "max_likes": 20, "min_likes": 0, "min_reposts_share": 0,
            "min_score": 0, "ranking": "flat", "max_posts": 100, "max_age_hours": 720}
    out, _st = fg.select([p], fcfg, datetime.now(timezone.utc), OWNER, CTX, set())
    assert not out and g["max_likes"]["pass"] is False
    print("ok   gate_report and select agree about the ceiling")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL GATE TESTS PASSED")
