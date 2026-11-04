"""fetch_all must stop paging once the list feed turns into history it already
has, and must not stop while the pages are still new."""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fg-incr-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "i.sqlite")

cfg = json.load(open("/home/alvaro/feedgen/config.json"))
cfg["feeds"] = [{"rkey": "t", "display_name": "T", "max_age_hours": 48,
                 "max_posts": 20}]
cfg["source"] = dict(cfg["source"], stale_pages_stop=2, full_crawl_every=0)
PATH = os.path.join(TMP, "config.json")
json.dump(cfg, open(PATH, "w"))
os.environ["FEEDGEN_CONFIG"] = PATH

sys.path.insert(0, HERE)
import feedgen as fg  # noqa: E402

fg.migrate_schema()

PAGES = 6
calls = {"n": 0}


def make_item(page, i):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - page * 60))
    return {"post": {"uri": f"at://did:plc:a/app.bsky.feed.post/{page}-{i}",
                     "cid": "c", "author": {"did": "did:plc:a", "handle": "a"},
                     "record": {"text": "x"}, "indexedAt": ts,
                     "likeCount": 1, "repostCount": 0, "quoteCount": 0,
                     "replyCount": 0}}


def fake_rpc(host, method, data=None, token=None, timeout=30):
    if "getListFeed" in method:
        page = calls["n"]
        calls["n"] += 1
        if page >= PAGES:
            return {"feed": []}
        return {"feed": [make_item(page, i) for i in range(5)],
                "cursor": f"c{page}" if page < PAGES - 1 else None}
    if "getAuthorFeed" in method:
        return {"feed": []}
    raise AssertionError(method)


fg.rpc = fake_rpc

# First crawl, empty database: every page is new, all 6 must be read.
posts, st = fg.fetch_all("tok")
assert st["pages"] == 6, st
assert len(posts) == 30, len(posts)
print("first crawl reads every page:", st["pages"], "pages,", len(posts), "posts")

# Store them and merge into the rolling corpus (refresh does both), then crawl
# again: the first pages are known, so the walk stops early.
fg.record_posts(posts)
corpus = fg.merge_corpus(posts, 48)
assert len(corpus) == 30, len(corpus)
calls["n"] = 0
posts2, st2 = fg.fetch_all("tok")
assert st2["pages"] <= 3, st2
print("second crawl stops early:", st2["pages"], "pages")

# A forced full crawl reads everything again.
fg.CFG["source"]["stale_pages_stop"] = 0
calls["n"] = 0
_ps, st3 = fg.fetch_all("tok")
assert st3["pages"] == 6, st3
print("full crawl with the knob off:", st3["pages"], "pages")

# The corpus is a set, not a list: a post seen twice counts once.
corpus = fg.merge_corpus(posts, 48)
assert len(corpus) == 30, len(corpus)
# And it prunes what fell out of the window.
old = make_item(99, 0)
old["post"]["indexedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(time.time() - 400 * 86400))
corpus = fg.merge_corpus([old["post"]], 48)
assert not any(p["uri"] == old["post"]["uri"] for p in corpus)
print("corpus merges, dedupes and prunes:", len(corpus))

print("ALL INCREMENTAL CRAWL CHECKS PASSED")
