"""Pool growth: co-like votes, probation, weighting, promotion and drop."""
import json
import os
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fg-growth-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "g.sqlite")

cfg = json.load(open("/home/alvaro/feedgen/config.json"))
cfg["feeds"] = [{"rkey": "t", "display_name": "T", "max_age_hours": 48,
                 "max_posts": 50, "min_likes": 0, "seen_ttl_hours": 0}]
cfg["growth"] = {"enabled": True, "sample_members": 25, "votes_needed": 3,
                 "probation_days": 7, "max_probation": 30,
                 "promote_min_likes": 3, "interval_secs": 0, "like_pages": 1,
                 "score_mult": 0.5}
CFG_PATH = os.path.join(TMP, "config.json")
json.dump(cfg, open(CFG_PATH, "w"))
os.environ["FEEDGEN_CONFIG"] = CFG_PATH

sys.path.insert(0, HERE)
import feedgen as fg  # noqa: E402

fg.migrate_schema()
con = fg.db()
NOW = time.time()

M1, M2, M3 = "did:plc:m1", "did:plc:m2", "did:plc:m3"
X = "did:plc:outsider"
Y = "did:plc:quiet"
# The list members exist because they have recent posts.
for d in (M1, M2, M3):
    con.execute(
        "INSERT OR REPLACE INTO posts(uri, cid, author_did, author_handle, "
        "indexed_at, like_count, repost_count, quote_count, reply_count, text, "
        "langs, record_json, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"at://{d}/m", "c", d, d, time.strftime("%Y-%m-%dT%H:%M:%SZ"),
         1, 0, 0, 0, "t", "", "{}", NOW, NOW))


def fake_rpc(host, method, data=None, token=None, timeout=30):
    if "getFollows" in method:
        actor = urllib.parse.parse_qs(method.split("?", 1)[1])["actor"][0]
        # Every member follows the outsider (and one already-known member).
        return {"follows": [{"did": X, "handle": "outsider.test"},
                            {"did": M1, "handle": "m1"}]}
    if "getAuthorFeed" in method:
        actor = urllib.parse.parse_qs(method.split("?", 1)[1])["actor"][0]
        return {"feed": [
            {"post": {"uri": f"at://{actor}/p1", "cid": "c",
                      "author": {"did": actor, "handle": "outsider.test"},
                      "record": {"text": "hello", "langs": ["en"]},
                      "indexedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "likeCount": 2, "repostCount": 0, "quoteCount": 0,
                      "replyCount": 0}},
            {"post": {"uri": f"at://{actor}/p2", "cid": "c",
                      "author": {"did": actor, "handle": "outsider.test"},
                      "record": {"text": "again", "langs": ["en"]},
                      "indexedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "likeCount": 9, "repostCount": 0, "quoteCount": 0,
                      "replyCount": 0}}]}
    raise AssertionError(f"unexpected rpc {method}")


fg.rpc = fake_rpc
fg.meta_set("growth_mine_at", 0)
out = fg.mine_graph("tok")
print("mine:", out)
assert out["mined"] == 3, out
assert out["promoted"] == 1, out

row = con.execute("SELECT status, votes FROM growth_members WHERE author_did=?",
                  (X,)).fetchone()
assert row and row[0] == "probation", row
assert row[1] >= 3, row
print("probation created from three distinct votes:", row)

posts, st = fg.fetch_growth("tok")
assert len(posts) == 2 and all(p["_growth"] == 1 for p in posts), posts
assert st["growth_authors"] == 1, st
print("growth fetch tags posts:", st)

# A probation post must rank below an identical list post.
now_dt = fg.datetime.now(fg.timezone.utc)
ts = now_dt.isoformat()
ctx = {"topic_affinity": {}, "taste": {}, "topic_seeds": set(),
       "topic_keywords": [], "author_priors": {}, "owner_liked": set()}
list_post = {"uri": "at://list/post", "indexedAt": ts, "likeCount": 10,
             "repostCount": 0, "quoteCount": 0, "replyCount": 0,
             "author": {"did": M1}, "record": {"text": "x"}}
prob_post = {"uri": "at://prob/post", "indexedAt": ts, "likeCount": 10,
             "repostCount": 0, "quoteCount": 0, "replyCount": 0,
             "author": {"did": X}, "record": {"text": "x"}, "_growth": 1}
fcfg = {"rkey": "t", "max_age_hours": 48, "max_posts": 50, "min_likes": 0,
        "min_reposts_share": 0, "max_per_author": 0, "seen_ttl_hours": 0}
scored, _ = fg.select([list_post, prob_post], fcfg, now_dt, {}, ctx, set())
by_uri = dict(scored)
assert by_uri["at://list/post"] > by_uri["at://prob/post"], by_uri
print("probation weighted below list:", {k: round(v, 2) for k, v in by_uri.items()})

# Settlement: X earned likes -> member; Y did not -> dropped.
con.execute(
    "INSERT OR REPLACE INTO posts(uri, cid, author_did, author_handle, indexed_at, "
    "like_count, repost_count, quote_count, reply_count, text, langs, record_json, "
    "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("at://x/p1", "c", X, "outsider.test", ts, 6, 0, 0, 0, "x", "", "{}", NOW, NOW))
con.execute("INSERT INTO growth_members(author_did, handle, status, added_at, "
            "expires_at, votes) VALUES(?,?,?,?,?,?)",
            (Y, "quiet.test", "probation", NOW - 8 * 86400, NOW - 86400, 4))
con.execute("UPDATE growth_members SET expires_at=? WHERE author_did=?",
            (NOW - 86400, X))
res = fg.settle_growth(NOW)
print("settle:", res)
assert res == {"promoted": 1, "dropped": 1}, res
assert con.execute("SELECT status FROM growth_members WHERE author_did=?",
                   (X,)).fetchone()[0] == "member"
assert con.execute("SELECT status FROM growth_members WHERE author_did=?",
                   (Y,)).fetchone()[0] == "dropped"
print("settlement promoted the productive author and dropped the quiet one")

# Disabled by default: configuration absent means no network traffic at all.
cfg2 = json.load(open(CFG_PATH))
cfg2["growth"] = {"enabled": False}
json.dump(cfg2, open(CFG_PATH, "w"))
fg.CFG = cfg2
fg.meta_set("growth_mine_at", 0)
assert fg.mine_graph("tok") == {"mined": 0, "votes": 0, "promoted": 0}
assert fg.fetch_growth("tok") == ([], {})
print("growth is off unless configured")

print("ALL GROWTH CHECKS PASSED")
