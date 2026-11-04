#!/usr/bin/env python3
"""Tests for the selective feed axes and the chrono ranking mode.

A feed earns its slot by answering a different question, not by re-running the
same ranker on another time window. These are the four knobs that make that
true — and each one is invisible in every other suite if it silently stops
filtering:

  * `ranking: "chrono"` — newest first, engagement and taste ignored.
  * `require_media`     — only posts whose embed type is in the list.
  * `min_replies`       — only posts with at least N replies.
  * `require_owner_liked` — only posts the owner has publicly liked.

Run: python3 test_axes.py
"""
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="feedgen-axes-test-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")
os.environ["FEEDGEN_STATE_PATH"] = os.path.join(TMP, "no-legacy.json")
if not os.environ.get("FEEDGEN_CONFIG"):
    os.environ["FEEDGEN_CONFIG"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.example.json")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("fg_axes", os.path.join(HERE, "feedgen.py"))
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)  # type: ignore[union-attr]

NOW = datetime.now(timezone.utc)
CTX = {"topic_affinity": {}, "taste": {}, "topic_seeds": set(),
       "topic_keywords": [], "author_priors": {}, "owner_liked": set()}
checks = 0


def ok(cond, label):
    global checks
    assert cond, label
    checks += 1


def mkpost(uri, hours, likes=0, replies=0, embed=None, langs=None):
    ts = (NOW - timedelta(hours=hours)).isoformat()
    rec = {"text": "t", "createdAt": ts}
    if embed:
        rec["embed"] = {"$type": embed}
    if langs:
        rec["langs"] = langs
    return {"uri": uri, "cid": "c", "indexedAt": ts,
            "author": {"did": "did:plc:" + uri.split("/")[-1], "handle": "h"},
            "record": rec, "likeCount": likes, "repostCount": 0,
            "quoteCount": 0, "replyCount": replies}


BASE = {"rkey": "t", "max_age_hours": 48, "max_posts": 50, "min_likes": 0,
        "min_reposts_share": 0, "max_per_author": 0, "seen_ttl_hours": 0}
OWNER = {}


def uris(fcfg, posts, ctx=CTX):
    picked, _st = fg.select(posts, fcfg, NOW, OWNER, ctx, set())
    return [u for u, _s in picked]


# ---- chrono: newest first, engagement ignored
old_but_loved = mkpost("at://did:plc:a/app.bsky.feed.post/1", hours=10, likes=500)
new_quiet = mkpost("at://did:plc:b/app.bsky.feed.post/2", hours=0.5, likes=0)
order = uris(dict(BASE, ranking="chrono"), [old_but_loved, new_quiet])
ok(order == ["at://did:plc:b/app.bsky.feed.post/2",
             "at://did:plc:a/app.bsky.feed.post/1"],
   "chrono ranks by recency, not engagement")

# chrono must ignore the source multiplier (a negative score times 0.6 would
# otherwise jump an external post to the top)
tagged = mkpost("at://did:plc:c/app.bsky.feed.post/3", hours=3, likes=1)
tagged["_src_mult"] = 0.1
order = uris(dict(BASE, ranking="chrono"), [tagged, new_quiet])
ok(order[0] == "at://did:plc:b/app.bsky.feed.post/2",
   "chrono ignores the source multiplier")

# ---- require_media
text_only = mkpost("at://did:plc:d/app.bsky.feed.post/4", hours=1, likes=9)
pictured = mkpost("at://did:plc:e/app.bsky.feed.post/5", hours=2, likes=1, embed="app.bsky.embed.images")
video = mkpost("at://did:plc:f/app.bsky.feed.post/6", hours=3, likes=1, embed="app.bsky.embed.video")
media_cfg = dict(BASE, require_media=["app.bsky.embed.images", "app.bsky.embed.video"])
order = uris(media_cfg, [text_only, pictured, video])
ok(set(order) == {"at://did:plc:e/app.bsky.feed.post/5",
                  "at://did:plc:f/app.bsky.feed.post/6"},
   "require_media keeps only posts with an allowed embed type")
ok(uris(dict(BASE, require_media=["app.bsky.embed.images"]), [text_only]) == [],
   "require_media does not fall back to text posts when the pool is empty")

# ---- min_replies
quiet = mkpost("at://did:plc:g/app.bsky.feed.post/7", hours=1, likes=9, replies=0)
talked = mkpost("at://did:plc:h/app.bsky.feed.post/8", hours=1, likes=0, replies=4)
order = uris(dict(BASE, min_replies=3), [quiet, talked])
ok(order == ["at://did:plc:h/app.bsky.feed.post/8"], "min_replies keeps only discussions")

# ---- require_owner_liked
liked = mkpost("at://did:plc:i/app.bsky.feed.post/9", hours=1, likes=0)
not_liked = mkpost("at://did:plc:j/app.bsky.feed.post/10", hours=1, likes=9)
ctx = dict(CTX, owner_liked={"at://did:plc:i/app.bsky.feed.post/9"})
order = uris(dict(BASE, require_owner_liked=True), [liked, not_liked], ctx)
ok(order == ["at://did:plc:i/app.bsky.feed.post/9"],
   "require_owner_liked keeps only the owner's liked posts")
ok(uris(dict(BASE, require_owner_liked=True), [liked, not_liked], CTX) == [],
   "an empty owner_liked set hides the feed's whole pool rather than ignoring the flag")

# ---- language axis
es_post = mkpost("at://did:plc:k/app.bsky.feed.post/11", hours=1, likes=1, langs=["es-MX"])
ca_post = mkpost("at://did:plc:l/app.bsky.feed.post/12", hours=1, likes=1, langs=["ca"])
en_post = mkpost("at://did:plc:m/app.bsky.feed.post/13", hours=1, likes=1, langs=["en"])
none_post = mkpost("at://did:plc:n/app.bsky.feed.post/14", hours=1, likes=1)
order = uris(dict(BASE, require_lang=["es", "ca", "gl"]),
             [es_post, ca_post, en_post, none_post])
ok(set(order) == {"at://did:plc:k/app.bsky.feed.post/11",
                  "at://did:plc:l/app.bsky.feed.post/12"},
   "require_lang matches primary subtags and drops untagged posts")
order = uris(dict(BASE, exclude_langs=["en"]),
             [es_post, ca_post, en_post, none_post])
ok(len(order) == 3 and "at://did:plc:m/app.bsky.feed.post/13" not in order,
   "exclude_langs drops only declared matches, keeps untagged posts")

# ---- the axes are opt-in: an unconfigured feed keeps every post
order = uris(BASE, [quiet, talked, pictured, text_only, es_post, none_post])
ok(len(order) == 6, "no axis configured means no filtering")

print(f"ALL AXES CHECKS PASSED ({checks} assertions)")
