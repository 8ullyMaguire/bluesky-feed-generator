#!/usr/bin/env python3
"""Tests for the ranking + personalization layer of a feedgen service.

Run from the service directory:
  python3 test_ranking.py feedgen.py

Uses a throwaway FEEDGEN_DB; performs no network calls.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import base64
import sqlite3
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="feedgen-rank-test-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")

HERE = os.path.dirname(os.path.abspath(__file__))
if not os.environ.get("FEEDGEN_CONFIG"):
    # Runnable on a fresh clone: prefer a local config.json, else the example.
    os.environ["FEEDGEN_CONFIG"] = os.path.join(
        HERE, "config.json" if os.path.exists(os.path.join(HERE, "config.json"))
        else "config.example.json")
TARGET = sys.argv[1] if len(sys.argv) > 1 else "feedgen.py"
_spec = importlib.util.spec_from_file_location("fg", os.path.join(HERE, TARGET))
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)  # type: ignore[union-attr]

W = {"w_likes": 1.0, "w_reposts": 3.0, "w_quotes": 2.0, "w_replies": 0.5}
NOW = datetime.now(timezone.utc)

FORYOU = next(f["rkey"] for f in fg.CFG["feeds"] if f.get("ranking") == "foryou")
MLFEED = next(f["rkey"] for f in fg.CFG["feeds"] if f.get("ranking") == "topic")
KW0 = (fg.CFG.get("topic_keywords") or ["kw"])[0]

CTX = {"topic_affinity": {}, "topic_seeds": set(), "taste": {}, "topic_keywords": [],
       "author_priors": {}}


def post(likes=10, did="did:plc:a", key="p", text="", hours=2.0, langs=None):
    rec = {"text": text}
    if langs is not None:
        rec["langs"] = langs
    return {"uri": f"at://{did}/app.bsky.feed.post/{key}", "cid": "c",
            "author": {"did": did}, "record": rec,
            "likeCount": likes, "repostCount": 0, "quoteCount": 0,
            "replyCount": 0,
            "indexedAt": (NOW - timedelta(hours=hours)).isoformat()}


def db_insert_post(uri, author_did):
    fg.db().execute(
        "INSERT OR REPLACE INTO posts(uri, cid, author_did, author_handle, "
        "indexed_at, record_json, first_seen, last_seen) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (uri, "c", author_did, "h.test", "2026-09-15T00:00:00Z", "{}",
         time.time(), time.time()))


def test_rank_score_velocity_decays_with_age():
    fcfg = {"ranking": "velocity", "gravity": 0.6}
    fresh = fg.rank_score(post(hours=0.5), fcfg, W, 0.5, CTX)
    old = fg.rank_score(post(hours=20.0), fcfg, W, 20.0, CTX)
    assert fresh > old * 3, (fresh, old)
    print("ok  velocity ranking prefers fresh posts")


def test_rank_score_taste_multiplier_on_any_mode():
    # Top feeds (ranking velocity) get a small taste_weight: owner-liked
    # authors rise everywhere, not just on foryou/topic.
    fcfg = {"ranking": "velocity", "gravity": 0.6, "taste_weight": 0.3}
    ctx = dict(CTX, taste={"did:plc:b": 1.0})
    s_taste = fg.rank_score(post(likes=10, did="did:plc:b"), fcfg, W, 1.0, ctx)
    s_plain = fg.rank_score(post(likes=10, did="did:plc:a"), fcfg, W, 1.0, ctx)
    assert abs(s_taste - s_plain * 1.3) < 1e-9, (s_taste, s_plain)
    print("ok  taste_weight lifts owner-liked authors on any feed")


def test_rank_score_hashtag_beats_plain():
    fcfg = {"ranking": "topic", "topic_weight": 2.5, "topic_keyword_bonus": 0.35,
            "decay_factor": 0.6}
    ctx = dict(CTX, topic_keywords=[KW0])
    s_tag = fg.rank_score(post(text=f"#{KW0}"), fcfg, W, 1.0, ctx)
    s_plain = fg.rank_score(post(text=KW0), fcfg, W, 1.0, ctx)
    assert s_tag > s_plain, (s_tag, s_plain)
    print("ok  hashtag keyword hits outweigh plain-text hits")


def test_author_prior_multiplies():
    fcfg = {"ranking": "velocity", "gravity": 0.6}
    ctx = dict(CTX, author_priors={"did:plc:c": 1.23})
    s_c = fg.rank_score(post(likes=10, did="did:plc:c"), fcfg, W, 1.0, ctx)
    s_a = fg.rank_score(post(likes=10, did="did:plc:a"), fcfg, W, 1.0, ctx)
    assert abs(s_c - s_a * 1.23) < 1e-9
    print("ok  author prior multiplies the baseline")


def test_select_returns_scored_tuples():
    posts = [post(likes=30, did="did:plc:a", key="1"),
             post(likes=5, did="did:plc:b", key="2")]
    fcfg = {"rkey": "t", "ranking": "velocity", "gravity": 0.6,
            "max_posts": 10, "max_per_author": 3,
            "min_age_hours": 0, "max_age_hours": 24}
    scored, stats = fg.select(posts, fcfg, NOW, {"did": "did:plc:own"},
                              CTX, set())
    assert all(isinstance(t, tuple) and len(t) == 2 for t in scored), scored
    assert scored[0][0].endswith("/1") and scored[0][1] > scored[1][1]
    print("ok  select() returns (uri, score) tuples in ranked order")


def test_select_owner_ratio_cap():
    owner_did = "did:plc:own"
    posts = [post(likes=50, did=owner_did, key=f"o{i}") for i in range(10)]
    posts += [post(likes=50, did=f"did:plc:u{i}", key=f"u{i}") for i in range(10)]
    fcfg = {"rkey": "t", "max_posts": 100, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24}
    owner = {"did": owner_did, "always_include": True, "boost": 1.3,
             "bonus": 3.0, "max_ratio": 0.33}
    scored, _ = fg.select(posts, fcfg, NOW, owner, CTX, set())
    uris = [u for u, _ in scored]
    n_own = sum(1 for u in uris if u.startswith(f"at://{owner_did}/"))
    assert n_own <= int(0.33 * len(uris)) + 1, (n_own, len(uris))
    print(f"ok  owner ratio cap holds ({n_own}/{len(uris)} owner posts)")


def test_min_topic_affinity_floor_drops_offtopic_authors():
    """A feed may refuse authors whose stored corpus barely mentions the
    subject — the firehose problem — while leaving alone authors it has not
    seen enough posts from to judge."""
    good, bad = "did:plc:ontopic", "did:plc:firehose"
    posts = [post(likes=50, did=good, key="g1"),
             post(likes=90, did=bad, key="b1"),
             post(likes=90, did=bad, key="b2")]
    fcfg = {"rkey": "t", "max_posts": 100, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24,
            "min_topic_affinity": 0.10, "topic_min_posts": 10}
    base = {"topic_affinity": {good: 0.8, bad: 0.0}, "topic_seeds": set(),
            "taste": {}, "topic_keywords": [], "author_priors": {},
            "topic_totals": {good: 100, bad: 3000}}
    scored, st = fg.select(posts, fcfg, NOW, {"did": "did:plc:floorowner"},
                           base, set())
    uris = [u for u, _s in scored]
    assert uris and all(bad not in u for u in uris), uris
    assert any(good in u for u in uris), uris
    assert st.get("topic_floor") == 2, st
    # Too few stored posts to judge the author: the POST has to carry the
    # evidence. The fixture's texts have no keyword, so the one-off account is
    # dropped here — this is the viral-post-from-a-stranger hole.
    few = dict(base, topic_totals={good: 100, bad: 3})
    scored2, _ = fg.select(posts, fcfg, NOW, {"did": "did:plc:floorowner"},
                           few, set())
    assert not any(bad in u for u, _s in scored2), [u for u, _s in scored2]
    # ...and it is kept when the post itself is on topic, whether in the text
    # or in a media post's alt text.
    kw = dict(few, topic_keywords=["bnwo"])
    posts_text = posts + [post(likes=90, did=bad, key="b3", text="bnwo tonight")]
    scored_t, _ = fg.select(posts_text, fcfg, NOW, {"did": "did:plc:floorowner"},
                            kw, set())
    assert any("b3" in u for u, _s in scored_t), [u for u, _s in scored_t]
    assert not any("b1" in u for u, _s in scored_t), [u for u, _s in scored_t]
    # No knob on the feed: unchanged ranking.
    fcfg_off = {k: v for k, v in fcfg.items()
                if k not in ("min_topic_affinity", "topic_min_posts")}
    scored3, _ = fg.select(posts, fcfg_off, NOW, {"did": "did:plc:floorowner"},
                           base, set())
    assert any(bad in u for u, _s in scored3)
    print("ok  min_topic_affinity drops off-topic firehose authors only when "
          "the sample is big enough")



def test_hide_seen_scope_can_be_per_feed():
    """Seen memory defaults to the service (the owner's "never see a post
    twice"), but a feed may scope it to itself: a reader who opens a different
    window then gets that window's own best posts instead of inheriting the
    first feed's burn. Same rows, same TTL — only the WHERE clause differs."""
    now = time.time()
    fg.db().execute("DELETE FROM served_posts WHERE requester_did=?",
                    ("did:plc:scope",))
    fg.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                    ("did:plc:scope",
                     "at://did:plc:a/app.bsky.feed.post/in-a", "feed-a",
                     now - 600))
    shared = fg.user_excluded_uris("did:plc:scope", True)
    assert "at://did:plc:a/app.bsky.feed.post/in-a" in shared, shared
    per_feed_a = fg.user_excluded_uris(
        "did:plc:scope", True, feed_rkey="feed-a", scope="feed")
    per_feed_b = fg.user_excluded_uris(
        "did:plc:scope", True, feed_rkey="feed-b", scope="feed")
    assert "at://did:plc:a/app.bsky.feed.post/in-a" in per_feed_a, per_feed_a
    assert per_feed_b == set(), per_feed_b
    print("ok  hide_seen_scope=feed hides only what that feed served")


def test_topic_post_floor_is_continuous():
    """Post-level topicality: a keyword hit is full evidence, anything else
    inherits half its author's affinity. The threshold then separates a
    topical author's off-vocabulary post from a firehose author's, instead of
    the binary "author is fine, every post passes" cliff."""
    good, bad = "did:plc:topicgood", "did:plc:topicbad"
    posts = [post(likes=10, did=good, key="g"),
             post(likes=10, did=bad, key="b")]
    fcfg = {"rkey": "t", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24,
            "topic_post_floor": 0.4}
    ctx = dict(CTX, topic_affinity={good: 0.9, bad: 0.05},
               topic_keywords=["kink"], topic_totals={good: 100, bad: 100})
    scored, st = fg.select(posts, fcfg, NOW, {"did": "did:plc:pfowner"}, ctx, set())
    uris = [u for u, _s in scored]
    assert any(good in u for u in uris), uris      # 0.9 * 0.5 = 0.45 >= 0.4
    assert not any(bad in u for u in uris), uris   # 0.05 * 0.5 = 0.025
    assert st.get("topic_post") == 1, st
    # A keyword in the post rescues even the low-affinity author.
    posts2 = posts + [post(likes=10, did=bad, key="b2", text="kink tonight")]
    scored2, _ = fg.select(posts2, fcfg, NOW, {"did": "did:plc:pfowner"}, ctx, set())
    assert any("b2" in u for u, _s in scored2), [u for u, _s in scored2]
    # No key on the feed: unchanged ranking.
    scored3, _ = fg.select(posts, {k: v for k, v in fcfg.items()
                                   if k != "topic_post_floor"},
                           NOW, {"did": "did:plc:pfowner"}, ctx, set())
    assert any(bad in u for u, _s in scored3)
    print("ok  topic_post_floor scores the post, not just its author")


def test_owner_bonus_mode_relative_is_scale_invariant():
    """A flat owner bonus is a takeover on a weak board and invisible on a strong
    one; the relative mode must put the operator a fixed *fraction* above the
    board's median regardless of what the median is.

    Measured on the live estate: +3.0 sat at or above the 90th percentile of
    every velocity board (p90 2.5-3.4) and below the median of the flat Spanish
    boards (4.2), so one constant cannot be right for both.
    """
    owner = "did:plc:own"
    fcfg = {"rkey": "t", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "ranking": "flat"}
    rel = {"did": owner, "always_include": True, "boost": 1.0,
           "bonus_mode": "relative", "bonus_frac": 0.3, "max_ratio": 0.5}
    absolute = {"did": owner, "always_include": True, "boost": 1.0,
                "bonus": 3.0, "max_ratio": 0.5}
    ratios = {}
    for likes in (1, 200, 4000):
        posts = [post(likes=likes, did=owner, key="own")] + [
            post(likes=likes, did="did:plc:u%d" % i, key="u%d" % i)
            for i in range(9)]
        got = {}
        for label, ob in (("rel", rel), ("abs", absolute)):
            scored, _ = fg.select(posts, fcfg, NOW, ob, CTX, set())
            sco = dict(scored)
            own_uri = "at://%s/app.bsky.feed.post/own" % owner
            others = sorted(v for k, v in sco.items() if k != own_uri)
            med = others[len(others) // 2]
            got[label] = sco[own_uri] / med
        ratios[likes] = got
        assert abs(got["rel"] - 1.3) < 0.05, (likes, got)
    spread = max(r["abs"] for r in ratios.values()) - min(r["abs"] for r in ratios.values())
    assert spread > 2.0, ratios          # absolute mode is not scale-free
    print("ok  relative owner bonus is scale-invariant (%.2f-%.2f) while absolute "
          "spans %.1fx" % (min(r["rel"] for r in ratios.values()),
                           max(r["rel"] for r in ratios.values()), spread))


def test_owner_bonus_mode_off_leaves_the_board_alone():
    """`bonus_mode: "off"` removes the multiplier and the bonus: the operator is
    an ordinary author on the score, whatever the interleave does with the slot."""
    owner = "did:plc:own"
    posts = [post(likes=5, did=owner, key="own")] + [
        post(likes=50, did="did:plc:u%d" % i, key="u%d" % i) for i in range(9)]
    fcfg = {"rkey": "t", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "ranking": "flat"}
    ob = {"did": owner, "always_include": True, "boost": 1.3, "bonus": 9.0,
          "bonus_mode": "off", "max_ratio": 0.5}
    scored, _ = fg.select(posts, fcfg, NOW, ob, CTX, set())
    sco = dict(scored)
    own_uri = "at://%s/app.bsky.feed.post/own" % owner
    others = sorted(v for k, v in sco.items() if k != own_uri)
    assert sco[own_uri] < others[0], (sco[own_uri], others[:2])
    assert abs(sco[own_uri] - fg.ranking_core.log_engagement(
        post(likes=5, did=owner, key="own"), W)) < 1e-9
    print("ok  bonus_mode=off drops boost and bonus, keeping the raw score")


def test_chrono_ignores_owner_promotion():
    """A timeline feed is a timeline: no boost, no bonus, whatever the config says."""
    owner = "did:plc:own"
    posts = [post(likes=1, did=owner, key="own", hours=5)] + [
        post(likes=50, did="did:plc:u%d" % i, key="u%d" % i, hours=1 + i)
        for i in range(5)]
    fcfg = {"rkey": "t", "max_posts": 10, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "ranking": "chrono"}
    ob = {"did": owner, "always_include": True, "boost": 9.0, "bonus": 99.0,
          "bonus_mode": "relative", "bonus_frac": 5.0, "max_ratio": 0.5}
    scored, _ = fg.select(posts, fcfg, NOW, ob, CTX, set())
    sco = dict(scored)
    own = sco["at://%s/app.bsky.feed.post/own" % owner]
    assert -6.0 < own < -4.0, own      # -age_h, untouched by boost/bonus
    print("ok  chrono keeps owner posts chronological (score %.2f)" % own)


def test_owner_gate_bypass_can_be_disabled():
    """owner.gate_bypass: false keeps the owner's boost and cap but makes their
    posts clear the same floors as everyone else's."""
    owner = "did:plc:gatedowner"
    posts = [post(likes=2, did=owner, key="own1")] + [
        post(likes=5, did="did:plc:other", key="o%d" % i) for i in range(4)]
    # adaptive_target: 0 pins the configured gates; otherwise a thin candidate
    # set relaxes min_likes to 0 and the test would prove nothing.
    fcfg = {"rkey": "t", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "min_likes": 3,
            "adaptive_target": 0}
    bypass = {"did": owner, "always_include": True, "boost": 1.3,
              "bonus": 0.0, "max_ratio": 0.5}
    scored, st = fg.select(posts, fcfg, NOW, bypass, CTX, set())
    assert any("own1" in u for u, _s in scored), [u for u, _s in scored]
    scored2, st2 = fg.select(posts, fcfg, NOW, dict(bypass, gate_bypass=False),
                             CTX, set())
    assert not any("own1" in u for u, _s in scored2), [u for u, _s in scored2]
    assert any("o1" in u for u, _s in scored2), [u for u, _s in scored2]
    assert len(scored2) == 4, [u for u, _s in scored2]
    print("ok  owner.gate_bypass=false puts owner posts through the gates")


def test_sources_allowlist_refuses_the_list():
    """A board that means "all Spanish posts" cannot be built from a follow list.
    `sources` is an allowlist on provenance: anything unlisted — the list crawl,
    an old DB row with no provenance — is not a candidate."""
    posts = [
        post(likes=100, did="did:plc:search", key="from-search", langs=["es"]),
        post(likes=100, did="did:plc:list", key="from-list", langs=["es"]),
        post(likes=100, did="did:plc:old", key="from-db", langs=["es"]),
    ]
    posts[0]["_src_key"] = "es-day"
    posts[1]["_src_key"] = "list"
    base = {"max_posts": 50, "max_per_author": 0, "min_age_hours": 0,
            "max_age_hours": 24, "min_likes": 0, "adaptive_target": 0,
            "require_lang": ["es"]}
    fcfg = dict(base, rkey="feed-es", sources=["es-day", "owner"])
    keys = {u.rsplit("/", 1)[-1] for u, _s in fg.select(posts, fcfg, NOW,
                                                        {"did": "did:plc:own"}, CTX, set())[0]}
    assert keys == {"from-search"}, keys

    fcfg_any = dict(base, rkey="feed-es")
    keys_any = {u.rsplit("/", 1)[-1] for u, _s in fg.select(posts, fcfg_any, NOW,
                                                            {"did": "did:plc:own"}, CTX, set())[0]}
    assert keys_any == {"from-search", "from-list", "from-db"}, keys_any
    print("ok  sources allowlist refuses the list (and untagged rows)")


def test_flat_ranking_has_no_time_decay():
    """`flat` is pure engagement: a popular 20-hour-old post must beat a weak
    fresh one on a board that means "popular today"."""
    w = fg.CFG["scoring"]
    fcfg = {"ranking": "flat", "max_age_hours": 24}
    strong_old = fg.rank_score(post(likes=400, hours=20), fcfg, w, 20.0, CTX)
    weak_new = fg.rank_score(post(likes=2, hours=0.2), fcfg, w, 0.2, CTX)
    assert strong_old > weak_new * 2, (strong_old, weak_new)
    vel = {"ranking": "velocity", "gravity": 0.4, "half_life_hours": 2.0,
           "max_age_hours": 24}
    strong_old_v = fg.rank_score(post(likes=400, hours=20), vel, w, 20.0, CTX)
    weak_new_v = fg.rank_score(post(likes=2, hours=0.2), vel, w, 0.2, CTX)
    assert strong_old_v < weak_new_v or strong_old_v < strong_old, \
        "velocity mode is supposed to trade engagement for freshness"
    print("ok  flat ranking ignores age; velocity mode does not")


def test_corpus_cap_comes_from_config():
    """The cap trims by recency, so it decides how many hours a long board can
    see. It must be settable from config, and must fall back when unset."""
    saved = fg.CFG.get("corpus")
    try:
        fg.CFG.pop("corpus", None)
        assert fg.corpus_cap() == fg.CORPUS_MAX
        fg.CFG["corpus"] = {"cap": 70000}
        assert fg.corpus_cap() == 70000
        fg.CFG["corpus"] = {"cap": "not-a-number"}
        assert fg.corpus_cap() == fg.CORPUS_MAX, "a bad value must not crash the cycle"
    finally:
        if saved is None:
            fg.CFG.pop("corpus", None)
        else:
            fg.CFG["corpus"] = saved
    print("ok  corpus cap is config-driven with a safe fallback")


def test_source_can_be_scoped_to_named_feeds():
    """A broad source (the Spanish top search) must reach only the feeds that
    asked for it, or the widest source in the estate becomes everybody's."""
    posts = [
        post(likes=50, did="did:plc:es", key="es-only", langs=["es"]),
        post(likes=50, did="did:plc:any", key="for-all", langs=["es"]),
    ]
    posts[0]["_src_feeds"] = ["feed-es"]
    fcfg = {"rkey": "feed-es", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "min_likes": 0,
            "adaptive_target": 0, "require_lang": ["es"]}
    other = dict(fcfg, rkey="feed-other")
    ob = {"did": "did:plc:own", "always_include": False}

    es_keys = {u.rsplit("/", 1)[-1] for u, _s in fg.select(posts, fcfg, NOW, ob, CTX, set())[0]}
    other_keys = {u.rsplit("/", 1)[-1] for u, _s in fg.select(posts, other, NOW, ob, CTX, set())[0]}
    assert "es-only" in es_keys and "for-all" in es_keys, sorted(es_keys)
    assert "es-only" not in other_keys, sorted(other_keys)
    assert "for-all" in other_keys, sorted(other_keys)
    print("ok  _src_feeds scopes a source to the feeds that asked for it")


def test_search_blocks_normalise_both_shapes():
    """`search_sources` (list) wins; the older single `search_source` object keeps
    working; a disabled block never runs."""
    saved = fg.CFG.get("search_source"), fg.CFG.get("search_sources")
    try:
        fg.CFG.pop("search_sources", None)
        fg.CFG["search_source"] = {"enabled": True, "terms": ["a"]}
        blocks = fg.search_source_blocks()
        assert len(blocks) == 1 and blocks[0]["_key"] == "search", blocks

        fg.CFG["search_sources"] = [
            {"name": "es-top", "enabled": True, "terms": ["b"], "feeds": ["x"]},
            {"name": "off", "enabled": False, "terms": ["c"]},
        ]
        blocks = fg.search_source_blocks()
        assert [b["_key"] for b in blocks] == ["es-top"], blocks
        assert blocks[0]["feeds"] == ["x"]

        fg.CFG["search_source"] = {"enabled": False}
        fg.CFG.pop("search_sources", None)
        assert fg.search_source_blocks() == [], "a disabled single source must not run"
    finally:
        fg.CFG.pop("search_sources", None)
        if saved[0] is None:
            fg.CFG.pop("search_source", None)
        else:
            fg.CFG["search_source"] = saved[0]
        if saved[1] is not None:
            fg.CFG["search_sources"] = saved[1]
    print("ok  search sources: list form, single form, disabled block")


def test_adaptive_gate_does_not_rescan_candidates():
    """The adaptive gate re-derived "how many non-owner posts so far" by summing
    over `cands` on every post, which made the candidate loop quadratic: 183 M
    iterations and 32 s on a 20 k-post feed. A running counter is the same number
    in O(1), and this guard fails if that re-scan ever comes back."""
    posts = [post(likes=(i % 40), did="did:plc:a%d" % (i % 60), key="p%d" % i,
                  langs=["es"]) for i in range(8000)]
    fcfg = {"rkey": "t", "max_posts": 300, "max_per_author": 20,
            "min_age_hours": 0, "max_age_hours": 24, "min_likes": 0,
            "adaptive_target": 120, "require_lang": ["es"]}
    ob = {"did": "did:plc:own", "always_include": True, "boost": 1.3,
          "bonus": 0.0, "max_ratio": 0.5}
    t0 = time.monotonic()
    scored, st = fg.select(posts, fcfg, NOW, ob, CTX, set())
    took = time.monotonic() - t0
    assert len(scored) > 0, "the board must still fill"
    assert took < 2.0, ("select() is quadratic again: %.1fs for %d posts"
                        % (took, len(posts)))
    print("ok  adaptivity is O(1) per post (%.2fs for %d posts)" % (took, len(posts)))


def test_owner_picks_respect_max_per_author():
    """The owner interleave was the one path onto a board that could exceed
    max_per_author: on a 323-post Spanish board the operator held 24 slots at a
    cap of 20 — the largest single author on his own "popular" feed."""
    owner = "did:plc:bigowner"
    posts = [post(likes=50, did=owner, key="own%d" % i, langs=["es"])
             for i in range(30)] + [
        post(likes=5, did="did:plc:o%d" % i, key="o%d" % i, langs=["es"])
        for i in range(40)]
    fcfg = {"rkey": "t", "max_posts": 100, "max_per_author": 5,
            "min_age_hours": 0, "max_age_hours": 24, "min_likes": 0,
            "adaptive_target": 0, "require_lang": ["es"]}
    ob = {"did": owner, "always_include": True, "boost": 1.3, "bonus": 0.0,
          "max_ratio": 0.5}
    scored, st = fg.select(posts, fcfg, NOW, ob, CTX, set())
    owner_slots = sum(1 for u, _s in scored if u.startswith("at://" + owner))
    assert owner_slots <= 5, (owner_slots, len(scored))
    assert len(scored) > 40, "the board must still fill from other authors"
    print("ok  owner insertions are capped by max_per_author (%d slots)" % owner_slots)


def test_owner_lang_gate_filters_only_the_language_axis():
    """`owner.lang_gate: true` holds the owner to the feed's language axis while
    every other bypass stays on — the narrow fix for a Spanish board carrying the
    operator's English replies. Without the key the bypass is untouched."""
    owner = "did:plc:esowner"
    posts = [post(likes=0, did=owner, key="own-es", langs=["es"]),
             post(likes=0, did=owner, key="own-en", langs=["en"]),
             # no declared language: unknown, not Spanish, so it fails a require
             # list exactly like the English one
             post(likes=0, did=owner, key="own-none")] + [
        post(likes=6, did="did:plc:other%d" % i, key="o%d" % i, langs=["es"])
        for i in range(10)]
    fcfg = {"rkey": "t", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "min_likes": 5,
            "adaptive_target": 0, "require_lang": ["es"]}
    # 0 < max_ratio < 1 is what switches the owner interleave on, and it caps
    # owner picks at max_ratio x non-owner posts: 10 others -> all 3 eligible.
    owner_block = {"did": owner, "always_include": True, "boost": 1.3,
                   "bonus": 0.0, "max_ratio": 0.5}

    # default: the owner bypasses the axis, so all three owner posts are eligible
    scored, _st = fg.select(posts, fcfg, NOW, owner_block, CTX, set())
    keys = {u.rsplit("/", 1)[-1] for u, _s in scored}
    assert {"own-es", "own-en", "own-none"} <= keys, sorted(keys)

    # narrow gate: language only — the ES owner post survives at 0 likes, which
    # proves the likes floor is still bypassed rather than the whole block applied
    scored2, st2 = fg.select(posts, fcfg, NOW,
                             dict(owner_block, lang_gate=True), CTX, set())
    keys2 = {u.rsplit("/", 1)[-1] for u, _s in scored2}
    assert "own-es" in keys2, sorted(keys2)
    assert "own-en" not in keys2 and "own-none" not in keys2, sorted(keys2)
    assert st2["owner_lang"] == 2, st2
    assert len([k for k in keys2 if k.startswith("o") and k != "own-es"]) == 10, sorted(keys2)
    print("ok  owner.lang_gate drops owner posts outside the feed's language")


def test_owner_lang_gate_sniff_keeps_untagged_spanish():
    """The owner's client sends no `langs` array, so a strict gate drops every
    owner post. `lang_gate: "sniff"` reads the text of untagged ones instead, and
    declared tags still win over the sniff."""
    owner = "did:plc:esowner"
    posts = [
        post(likes=0, did=owner, key="own-es",
             text="Buenos días, aquí va el hilo de la semana sobre lo que pasó"),
        post(likes=0, did=owner, key="own-en",
             text="Here is the thread about what happened this week, with the numbers"),
        # declared English: the sniffer never runs, the tag decides
        post(likes=0, did=owner, key="own-tag-en", text="de la que el los las",
             langs=["en"]),
    ] + [post(likes=6, did="did:plc:other%d" % i, key="o%d" % i, langs=["es"])
         for i in range(10)]
    fcfg = {"rkey": "t", "max_posts": 50, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24, "min_likes": 5,
            "adaptive_target": 0, "require_lang": ["es"]}
    ob = {"did": owner, "always_include": True, "boost": 1.3, "bonus": 0.0,
          "max_ratio": 0.5}

    strict, st_strict = fg.select(posts, fcfg, NOW, dict(ob, lang_gate=True), CTX, set())
    strict_keys = {u.rsplit("/", 1)[-1] for u, _s in strict}
    assert not (strict_keys & {"own-es", "own-en", "own-tag-en"}), sorted(strict_keys)
    assert st_strict["owner_lang"] == 3, st_strict

    sniffed, st_sniff = fg.select(posts, fcfg, NOW, dict(ob, lang_gate="sniff"), CTX, set())
    keys = {u.rsplit("/", 1)[-1] for u, _s in sniffed}
    assert "own-es" in keys, sorted(keys)
    assert "own-en" not in keys and "own-tag-en" not in keys, sorted(keys)
    assert st_sniff["owner_lang"] == 2, st_sniff
    print("ok  owner.lang_gate='sniff' keeps untagged Spanish, drops untagged English")


def test_config_reload_picks_up_a_changed_file():
    """A tuning change must not need a restart: a restart empties CORPUS and
    cold-starts every board. The mtime check reloads in place, and a half-written
    file keeps the config already in memory instead of taking the service down."""
    tmpdir = tempfile.mkdtemp(prefix="fgcfg-")
    path = os.path.join(tmpdir, "config.json")
    saved = (fg.CONFIG_PATH, fg.CFG, fg.CFG_STAMP, fg.RELOAD_REQUESTED)

    def write(feeds, bad=False):
        body = {"hostname": "h.test", "publisher_did": "did:plc:p",
                "feeds": feeds}
        text = "{not json" if bad else json.dumps(body)
        with open(path, "w") as fh:
            fh.write(text)
        # bump mtime explicitly: two writes inside one filesystem tick would
        # otherwise look like no change at all
        os.utime(path, (time.time() + len(feeds), time.time() + len(feeds)))

    try:
        fg.CONFIG_PATH = path
        write([{"rkey": "one"}])
        fg.CFG_STAMP = None
        assert fg.reload_config_if_changed() is True
        assert [f["rkey"] for f in fg.CFG["feeds"]] == ["one"]
        assert fg.reload_config_if_changed() is False, "unchanged file must not reload"

        write([{"rkey": "one"}, {"rkey": "two"}])
        assert fg.reload_config_if_changed() is True
        assert [f["rkey"] for f in fg.CFG["feeds"]] == ["one", "two"]
        assert fg.FEEDS_BY_RKEY["two"]["rkey"] == "two"

        write([{"rkey": "three"}], bad=True)
        assert fg.reload_config_if_changed() is False
        assert [f["rkey"] for f in fg.CFG["feeds"]] == ["one", "two"], \
            "a broken file must keep the running config"
    finally:
        (fg.CONFIG_PATH, fg.CFG, fg.CFG_STAMP, fg.RELOAD_REQUESTED) = saved
        os.remove(path)
        os.rmdir(tmpdir)
    print("ok  config reload: change picked up, broken file refused, no restart")


def test_cold_start_can_seed_from_follows():
    """Reader follows are the cheapest taste signal available: one public call
    names dozens of accounts. Only follows already in the candidate pool are
    seeded, and the whole path is off unless `tension.cold_start_follows`."""
    me = "did:plc:newreader"
    followed_known, followed_unknown = "did:plc:followed-known", "did:plc:nobody"
    db_insert_post("at://%s/app.bsky.feed.post/knownpost" % followed_known,
                   followed_known)
    fg.db().execute("DELETE FROM user_affinity WHERE requester_did=?", (me,))
    fg.db().execute("DELETE FROM meta WHERE k LIKE ?", ("coldstart:%",))
    tension = fg.CFG.setdefault("tension", {})
    had = tension.get("cold_start_follows")
    real_likes, real_follows = (getattr(fg, "fetch_requester_likes", None),
                                getattr(fg, "fetch_requester_follows", None))
    try:
        tension["cold_start_follows"] = False
        fg.fetch_requester_likes = lambda did, pages: ([], {}, True)
        fg.fetch_requester_follows = lambda did, pages: [followed_known,
                                                         followed_unknown]
        fg.seed_cold_start(me)
        assert not fg.db().execute(
            "SELECT 1 FROM user_affinity WHERE requester_did=?", (me,)
        ).fetchone(), "seeded without the flag"
        fg.db().execute("DELETE FROM meta WHERE k=?", ("coldstart:" + me,))
        tension["cold_start_follows"] = True
        fg.seed_cold_start(me)
        rows = {r[0] for r in fg.db().execute(
            "SELECT author_did FROM user_affinity WHERE requester_did=?", (me,))}
        assert followed_known in rows, rows
        assert followed_unknown not in rows, rows
    finally:
        if had is None:
            tension.pop("cold_start_follows", None)
        else:
            tension["cold_start_follows"] = had
        if real_likes:
            fg.fetch_requester_likes = real_likes
        if real_follows:
            fg.fetch_requester_follows = real_follows
        fg.db().execute("DELETE FROM user_affinity WHERE requester_did=?", (me,))
        fg.db().execute("DELETE FROM meta WHERE k=?", ("coldstart:" + me,))
    print("ok  cold start can seed affinity from the reader's follows")


def test_relevance_telemetry_is_persisted():
    """The telemetry the relevance work needs: keyword hit rate, text-less
    share, median author topicality and engagement concentration, per board,
    in metrics_log — so a config change can be read instead of inferred."""
    fg.migrate_schema()   # a fresh test DB starts from SCHEMA, not the live shape
    cols = {r[1] for r in fg.db().execute("PRAGMA table_info(metrics_log)")}
    for c in ("kw_hit_rate", "no_text_rate", "affinity_p50",
              "engagement_gini"):
        assert c in cols, (c, cols)
    posts = [post(likes=10, did="did:plc:tela", key="k1", text="kink"),
             post(likes=0, did="did:plc:telb", key="k2", text=""),
             post(likes=5, did="did:plc:tela", key="k3", text="unrelated")]
    for _p in posts:      # log_metrics reads the board back out of posts
        fg.db().execute(
            "INSERT OR REPLACE INTO posts(uri, cid, author_did, author_handle, "
            "indexed_at, record_json, first_seen, last_seen, text, like_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_p["uri"], "c", _p["author"]["did"], "h.test",
             _p["indexedAt"], "{}", time.time(), time.time(),
             _p["record"]["text"], _p["likeCount"]))
    fg.CFG.setdefault("topic_keywords", [])
    had_kw = list(fg.CFG["topic_keywords"])
    fg.CFG["topic_keywords"] = ["kink"]
    try:
        fg.log_metrics({"tl-a": [p["uri"] for p in posts]}, {}, time.time())
    finally:
        fg.CFG["topic_keywords"] = had_kw
    row = fg.db().execute(
        "SELECT kw_hit_rate, no_text_rate, engagement_gini FROM metrics_log "
        "WHERE feed_rkey='tl-a' ORDER BY ts DESC LIMIT 1").fetchone()
    assert row[0] is not None and abs(row[0] - 1 / 3.0) < 0.01, row
    assert row[1] is not None and abs(row[1] - 1 / 3.0) < 0.01, row
    assert row[2] is not None and 0.0 <= row[2] <= 1.0, row
    # Leave the table as this suite's other assertions expect to find it.
    fg.db().execute("DELETE FROM metrics_log")
    print("ok  relevance telemetry is computed and persisted per board")


def test_feed_owner_override_is_per_feed_and_off_by_default():
    """A feed may opt out of the owner boost/inclusion with its own `owner`
    block; a feed without one must rank exactly as before. The generic feed
    depends on this: "do not push my content" is a per-feed decision."""
    base = {"did": "did:plc:own", "always_include": True, "boost": 1.3,
            "bonus": 3.0, "max_ratio": 0.33}
    assert fg.feed_owner(base, {}) == base, "absent feed block must change nothing"
    got = fg.feed_owner(base, {"owner": {"always_include": False,
                                         "boost": 1.0, "bonus": 0.0,
                                         "max_ratio": 0}})
    assert got["always_include"] is False and got["max_ratio"] == 0, got
    assert got["did"] == base["did"], "the DID must survive the override"
    assert base["always_include"] is True, "the deployment block must not be mutated"

    owner_did = base["did"]
    posts = [post(likes=0, did=owner_did, key=f"q{i}") for i in range(4)]
    posts += [post(likes=40, did=f"did:plc:u{i}", key=f"v{i}") for i in range(6)]
    fcfg = {"rkey": "t", "max_posts": 100, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24}
    forced, _ = fg.select(posts, fcfg, NOW, base, CTX, set())
    plain, _ = fg.select(posts, fcfg, NOW,
                         fg.feed_owner(base, {"owner": {"always_include": False,
                                                        "boost": 1.0, "bonus": 0.0,
                                                        "max_ratio": 0}}),
                         CTX, set())
    furis = [u for u, _s in forced]
    puris = [u for u, _s in plain]
    # Baseline: boosted and interleaved — an owner post lands in the first 3.
    assert any(u.startswith(f"at://{owner_did}/") for u in furis[:3]), furis
    # Override: still eligible (nothing is banned), but ranked on its own
    # engagement and with no interleave, so it sinks to the tail. "Do not push
    # my content" = present, never promoted.
    n_plain = sum(1 for u in puris if u.startswith(f"at://{owner_did}/"))
    assert n_plain == 4, (n_plain, puris)
    first_own = min(i for i, u in enumerate(puris) if u.startswith(f"at://{owner_did}/"))
    assert first_own == 6, puris
    print(f"ok  per-feed owner block: owner post in top 3 when pushed, "
          f"{n_plain} eligible but starting at position {first_own} without it")


def test_snapshot_engagements_write_throttle_prune():
    con = fg.db()
    saved = {k: fg.CFG.get(k) for k in ("snapshot_interval_hours",
                                        "snapshot_prune_hours")}
    fg.CFG["snapshot_interval_hours"] = 1
    fg.CFG["snapshot_prune_hours"] = 192
    now = time.time()
    p = post(likes=10, key="s1", hours=2.0)
    try:
        assert fg.snapshot_engagements([p], now) == 1            # first write
        assert fg.snapshot_engagements([p], now + 60) == 0       # inside 1h interval
        p["likeCount"] = 25
        assert fg.snapshot_engagements([p], now + 3600) == 1     # interval elapsed
        with fg.DB_LOCK:
            n = con.execute("SELECT COUNT(*) FROM engagement_snapshots "
                            "WHERE uri LIKE '%/s1'").fetchone()[0]
        assert n == 2, n
        with fg.DB_LOCK:
            con.execute("UPDATE engagement_snapshots SET ts = ts - 400*3600 WHERE ts <= ?",
                        (now + 60,))
        assert fg.snapshot_engagements([p], now + 3601) == 0     # nothing due
        with fg.DB_LOCK:
            left = con.execute("SELECT COUNT(*) FROM engagement_snapshots "
                               "WHERE uri LIKE '%/s1'").fetchone()[0]
        assert left == 1, left                                   # stale rows pruned
    finally:
        for k, v in saved.items():
            if v is None:
                fg.CFG.pop(k, None)
            else:
                fg.CFG[k] = v
    print("ok  snapshot_engagements: hourly heartbeat, prune on every pass")


def test_select_trending_two_track_interleave():
    con = fg.db()
    now_ts = time.time()
    fresh = [post(likes=50, key=f"f{i}", hours=1.5 + i * 0.4) for i in range(10)]
    v1 = post(likes=60, key="v1", hours=30.0)        # surging: +20 in the window
    v2 = post(likes=60, key="v2", hours=40.0)        # rising slower: +12
    stale = post(likes=60, key="stale", hours=50.0)  # flat: delta 0 -> invisible
    with fg.DB_LOCK:
        for p, delta in ((v1, 20.0), (v2, 12.0), (stale, 0.0)):
            con.execute("INSERT OR REPLACE INTO engagement_snapshots"
                        "(uri, ts, likes, reposts, quotes, replies) VALUES(?,?,?,?,?,?)",
                        (p["uri"], now_ts - 7 * 3600, p["likeCount"] - delta, 0, 0, 0))
    posts = fresh + [v1, v2, stale]
    fcfg = {"rkey": "trend-test", "ranking": "trending", "decay_factor": 0.8,
            "window_hours": 6, "half_life_hours": 72,
            "vintage_slot_fraction": 0.2, "vintage_min_age_hours": 6,
            "vintage_max_age_hours": 168, "min_age_hours": 1,
            "max_age_hours": 720, "max_posts": 100, "max_per_author": 0,
            "min_likes": 0, "min_reposts_share": 0.0}
    scored, stats = fg.select(posts, fcfg, NOW, {"did": "did:plc:own"}, CTX, set())
    uris = [u for u, _ in scored]
    assert uris[4].endswith("/v1") and uris[9].endswith("/v2"), uris
    assert not any(u.endswith("/stale") for u in uris), uris
    fresh_uris = [u for i, u in enumerate(uris) if (i + 1) % 5]
    assert fresh_uris == [f"at://did:plc:a/app.bsky.feed.post/f{i}" for i in range(10)], fresh_uris
    assert stats["vintage_picked"] == 2
    print("ok  select: two-track trending interleaves rediscovery every 5th slot")


def test_select_trending_without_vintage_is_legacy():
    fcfg = {"rkey": "t", "ranking": "trending", "decay_factor": 0.8,
            "min_age_hours": 0, "max_age_hours": 720,
            "max_posts": 10, "max_per_author": 3,
            "min_likes": 0, "min_reposts_share": 0.0}
    scored, _ = fg.select([post(likes=30, key="a", hours=3.0),
                           post(likes=30, key="b", hours=30.0)],
                          fcfg, NOW, {"did": "did:plc:own"}, CTX, set())
    uris = [u for u, _ in scored]
    assert len(uris) == 2 and uris[0].endswith("/a"), uris
    print("ok  select: trending without vintage keys behaves exactly as today")


def test_engagement_snapshots_schema():
    con = fg.db()
    with fg.DB_LOCK:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(engagement_snapshots)")}
    assert "engagement_snapshots" in names
    assert "idx_engsnap_ts" in names
    assert {"uri", "ts", "likes", "reposts", "quotes", "replies"} <= cols
    print("ok  engagement_snapshots table + ts index exist")


def test_process_interactions_builds_affinity():
    con = fg.db()
    with fg.DB_LOCK:
        con.execute("DELETE FROM interaction_events")
        con.execute("DELETE FROM user_affinity")
        db_insert_post("at://did:plc:mla/p/1", "did:plc:mla")
        for req, kind in (("did:plc:u1", "like"), ("did:plc:u2", "less")):
            con.execute(
                "INSERT INTO interaction_events(post_uri, feed_rkey, kind, "
                "created_at, requester_did) VALUES(?,?,?,?,?)",
                ("at://did:plc:mla/p/1", FORYOU, kind, time.time(), req))
    n = fg.process_interactions()
    assert n == 2, n
    r1 = con.execute("SELECT score FROM user_affinity WHERE requester_did="
                     "'did:plc:u1' AND author_did='did:plc:mla'").fetchone()
    r2 = con.execute("SELECT score FROM user_affinity WHERE requester_did="
                     "'did:plc:u2' AND author_did='did:plc:mla'").fetchone()
    assert r1 and abs(r1[0] - 0.2) < 1e-9, r1   # like 1.0 * LR 0.2
    # one requestLess deliberately lands on the affinity floor (-5.0 * 0.2)
    assert r2 and abs(r2[0] - fg.AFFINITY_MIN) < 1e-9, r2
    print("ok  process_interactions folds events into clipped affinity")


def test_owner_gravity_drifts_toward_taste():
    con = fg.db()
    fg.CFG.setdefault("tension", {})["owner_gravity_daily"] = 0.004
    with fg.DB_LOCK:
        con.execute("DELETE FROM user_affinity")
        con.execute("DELETE FROM taste")
        con.execute("INSERT INTO taste(author_did, likes_count, updated_at) "
                    "VALUES('did:plc:t1', 10, ?)", (time.time(),))
        con.execute("INSERT INTO taste(author_did, likes_count, updated_at) "
                    "VALUES('did:plc:t2', 5, ?)", (time.time(),))
        for a in ("did:plc:t1", "did:plc:t2"):
            con.execute("INSERT INTO user_affinity(requester_did, author_did, "
                        "score, updated_at) VALUES('did:plc:u1', ?, 1.0, ?)",
                        (a, time.time()))
    fg.meta_set("gravity_day", "")
    n = fg.apply_owner_gravity()
    assert n == 2, n
    rows = dict(con.execute("SELECT author_did, score FROM user_affinity "
                            "WHERE requester_did='did:plc:u1'").fetchall())
    g = 0.004
    assert abs(rows["did:plc:t1"] - 1.0) < 1e-9          # owner top: target 1.0
    assert rows["did:plc:t2"] < 1.0                      # target 0.5: pulled down
    print("ok  owner gravity drifts affinity toward owner taste")


def test_personalize_reranks_by_user_affinity():
    con = fg.db()
    fg.CFG["tension"] = {"personal_feeds": [FORYOU], "explore_share": 0.0}
    with fg.DB_LOCK:
        con.execute("DELETE FROM user_affinity")
        con.execute("DELETE FROM taste")
        db_insert_post("at://did:plc:love/p/1", "did:plc:love")
        db_insert_post("at://did:plc:meh/p/2", "did:plc:meh")
        con.execute("INSERT INTO user_affinity(requester_did, author_did, "
                    "score, updated_at) VALUES('did:plc:u9','did:plc:love',"
                    "1.5,?)", (time.time(),))
    board = ["at://did:plc:meh/p/2", "at://did:plc:love/p/1"]
    scores = {"at://did:plc:meh/p/2": 2.0, "at://did:plc:love/p/1": 1.0}
    out = fg.personalize_page("did:plc:u9", FORYOU, board, scores)
    assert out[0] == "at://did:plc:love/p/1", out   # 1.0*(1+1.5)=2.5 > 2.0
    # Non-personal feeds (anything outside tension.personal_feeds) are untouched
    other = next(f["rkey"] for f in fg.CFG["feeds"] if f["rkey"] != FORYOU)
    assert fg.personalize_page("did:plc:u9", other, board, scores) == board
    print("ok  personalize_page re-ranks personal feeds by user affinity")


def test_personalize_injects_explore_slots():
    con = fg.db()
    fg.CFG["tension"] = {"personal_feeds": [FORYOU], "explore_share": 0.12}
    with fg.DB_LOCK:
        con.execute("DELETE FROM user_affinity")
        con.execute("DELETE FROM taste")
        con.execute("INSERT INTO taste(author_did, likes_count, updated_at) "
                    "VALUES('did:plc:exp', 10, ?)", (time.time(),))
    board = [f"at://did:plc:x{i}/p/{i}" for i in range(20)]
    board += ["at://did:plc:exp/p/a", "at://did:plc:exp/p/b"]
    for u in board:
        db_insert_post(u, u.split("/")[2])
    scores = {u: 1.0 for u in board}
    out = fg.personalize_page("did:plc:fresh", FORYOU, board, scores)
    assert len(out) == len(board) and set(out) == set(board)
    exps = [i for i, u in enumerate(out) if u.startswith("at://did:plc:exp/")]
    assert len(exps) == 2 and exps[0] == 7, exps   # spacing 8 -> slot at 7
    print(f"ok  explore slots woven at owner-taste authors (positions {exps})")


def test_event_kinds_map():
    assert fg.EVENT_KINDS["interactionSeen"] == "seen"
    assert fg.EVENT_KINDS["requestMore"] == "more"
    assert fg.EVENT_KINDS["requestLess"] == "less"
    print("ok  sendInteractions event kinds map to internal kinds")


def test_gravity_two_rates():
    """Local taste still pulls in months; the other account's
    taste pulls ~10x slighter (years)."""
    con = fg.db()
    gL, gE = 0.004, 0.0004
    fg.CFG.setdefault("tension", {})["owner_gravity_daily"] = gL
    fg.CFG["tension"]["extended_gravity_daily"] = gE
    with fg.DB_LOCK:
        con.execute("DELETE FROM user_affinity")
        con.execute("DELETE FROM taste")
        con.execute("DELETE FROM taste_ext")
        con.execute("INSERT INTO taste(author_did, likes_count, updated_at) VALUES('did:plc:t1', 10, ?)", (time.time(),))
        con.execute("INSERT INTO taste(author_did, likes_count, updated_at) VALUES('did:plc:t2', 5, ?)", (time.time(),))
        con.execute("INSERT INTO taste_ext(author_did, likes_count, updated_at) VALUES('did:plc:e1', 7, ?)", (time.time(),))
        for a in ("did:plc:t1", "did:plc:t2", "did:plc:e1"):
            con.execute("INSERT INTO user_affinity(requester_did, author_did, score, updated_at) VALUES('did:plc:u1', ?, 1.0, ?)", (a, time.time()))
    fg.meta_set("gravity_day", "")
    n = fg.apply_owner_gravity()
    assert n == 3, n
    rows = dict(con.execute("SELECT author_did, score FROM user_affinity WHERE requester_did='did:plc:u1'").fetchall())
    assert abs(rows["did:plc:t1"]  - (1 - gE))       < 1e-9, rows["did:plc:t1"]
    assert abs(rows["did:plc:t2"]  - (1 - gL*0.5 - gE)) < 1e-9, rows["did:plc:t2"]
    assert abs(rows["did:plc:e1"]  - (1 - gL))       < 1e-9, rows["did:plc:e1"]
    print("ok   two-rate gravity: months to local taste, years to extended taste")


def test_build_taste_ext_stores_per_source_counts():
    """Public like records of every taste_sources account land
    in taste_ext, keyed by author, summed across sources."""
    con = fg.db()
    fg.CFG.setdefault("tension", {})["taste_sources"] = ["did:plc:src"]
    with fg.DB_LOCK:
        con.execute("DELETE FROM taste_ext")
    fg.meta_set("taste_ext_built_at", "")
    orig = fg.fetch_liked_authors
    fg.fetch_liked_authors = lambda did: {"did:plc:x": 5, "did:plc:y": 2} if did == "did:plc:src" else {}
    try:
        counts = fg.build_taste_ext()
    finally:
        fg.fetch_liked_authors = orig
    assert counts == {"did:plc:x": 5, "did:plc:y": 2}, counts
    rows = dict(con.execute("SELECT author_did, likes_count FROM taste_ext").fetchall())
    assert rows == {"did:plc:x": 5, "did:plc:y": 2}, rows
    counts2 = fg.build_taste_ext()          # TTL cache respected
    assert counts2 == counts
    print("ok   taste_ext built from taste_sources via public like records")


def test_select_excludes_foreign_owner():
    """A blocked_dids author is never scored and never enters
    the board, even though the feed owner bypasses gates."""
    posts = [post(likes=50, did="did:plc:bad", key="b"),
             post(likes=10, did="did:plc:good", key="g")]
    fcfg = {"rkey": "t", "max_posts": 10, "max_per_author": 0,
            "min_age_hours": 0, "max_age_hours": 24}
    fg.CFG.setdefault("blocked_dids", []).append("did:plc:bad")
    try:
        scored, _ = fg.select(posts, fcfg, NOW, {"did": "did:plc:good"}, CTX, set())
        assert [u for u, _ in scored] == ["at://did:plc:good/app.bsky.feed.post/g"]
    finally:
        fg.CFG["blocked_dids"].remove("did:plc:bad")
    print("ok   select() hard-skips blocked_dids authors")


def test_blocked_dids_never_blocks_local_owner():
    """A deployment's `blocked_dids` must never contain its own curator DID:
    blocking the owner would silently drop exactly the account the owner
    ratio exists to protect. (Separate deployments of this service commonly
    block each other's curator to keep their candidate pools disjoint — that
    is a deployment's choice, not an invariant of the code.)"""
    blocked = set(fg.CFG.get("blocked_dids") or [])
    own = fg.CFG["owner"]["did"]
    assert own not in blocked, (own, sorted(blocked))
    print("ok   blocked_dids never contains the local owner DID")


def test_verify_jwt_verifies_es256k_signature():
    """verify_jwt verifies ES256K against the KID's DID document (exact vm
    id match, multibase or JWK) and is fail-closed: tampered, expired,
    garbage, unmatched, or unresolvable tokens all return None. No network:
    docs are injected into _DID_DOC_CACHE / _fetch_did_doc is patched."""
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives import hashes as _h
    priv = _ec.generate_private_key(_ec.SECP256K1())
    pn = priv.public_key().public_numbers()
    comp = bytes([0x03 if (pn.y & 1) else 0x02]) + pn.x.to_bytes(32, "big")  # compressed SECP256K1
    ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    def _b58c(b):
        n = int.from_bytes(b, "big"); s = ""
        while n > 0:
            n, r = divmod(n, 58); s = ALPH[r] + s
        return "z" + s
    mb = _b58c(comp)
    kid_did = "did:plc:testverify000000000000000000000001"
    iss = "did:plc:testissuer000000000000000000000001"
    full_kid = f"{kid_did}#atproto"
    fg._DID_DOC_CACHE[kid_did] = ({"verificationMethod":
        [{"id": full_kid, "publicKeyMultibase": mb}]}, time.time())

    def _b64(o):
        return base64.urlsafe_b64encode(
            json.dumps(o, separators=(",", ":")).encode()).rstrip(b"=").decode()

    def mint(over=None):
        header = {"alg": "ES256K", "typ": "JWT", "kid": full_kid}
        payload = {"iss": iss, "aud": fg.SERVICE_DID,
                   "lxm": "app.bsky.feed.getFeedSkeleton",
                   "exp": time.time() + 600}
        if over:
            payload.update(over)
        hdr_b, pay_b = _b64(header), _b64(payload)
        si = f"{hdr_b}.{pay_b}".encode()
        sig = priv.sign(si, _ec.ECDSA(_h.SHA256()))
        return f"{hdr_b}.{pay_b}." + base64.urlsafe_b64encode(
            sig).rstrip(b"=").decode()

    assert fg.verify_jwt("Bearer " + mint()) == iss          # multibase path
    jwk_doc = {"verificationMethod": [{"id": full_kid, "publicKeyJwk":
        {"kty": "EC", "crv": "secp256k1",
         "x": base64.urlsafe_b64encode(pn.x.to_bytes(32, "big")).rstrip(b"=").decode(),
         "y": base64.urlsafe_b64encode(pn.y.to_bytes(32, "big")).rstrip(b"=").decode()}}]}
    old = fg._DID_DOC_CACHE.get(kid_did)
    fg._DID_DOC_CACHE[kid_did] = (jwk_doc, time.time())
    assert fg.verify_jwt("Bearer " + mint()) == iss          # jwk path
    fg._DID_DOC_CACHE[kid_did] = old
    good = mint()
    assert fg.verify_jwt("Bearer " + good[:-4] + "XXXX") is None       # tampered
    assert fg.verify_jwt("Bearer " + mint(over={"exp": 0})) is None    # expired
    assert fg.verify_jwt("Bearer aaa.bbb.ccc") is None                 # garbage
    fg._DID_DOC_CACHE[kid_did] = (
        {"verificationMethod": [{"id": "did:plc:someoneelse#atproto",
                                 "publicKeyMultibase": mb}]}, time.time())
    assert fg.verify_jwt("Bearer " + mint()) is None                   # vm id mismatch
    fg._DID_DOC_CACHE[kid_did] = old
    def _resolver_down(did):
        raise OSError("resolver down")
    orig_fetch = fg._fetch_did_doc
    fg._fetch_did_doc = _resolver_down
    try:
        assert fg.verify_jwt("Bearer " + good) is None                 # fail-closed
    finally:
        fg._fetch_did_doc = orig_fetch
    print("ok   verify_jwt ES256K via kid doc: valid accepted (multibase+jwk), fail-closed otherwise")


def test_cold_start_seeds_affinity():
    """One-shot seeding of user_affinity from a requester's public likes."""
    if not hasattr(fg, "seed_cold_start"):
        print("ok   cold-start absent in this module (skipped)")
        return
    import tempfile, os as _os
    old = fg.DB_PATH
    tmp = tempfile.mktemp(suffix=".sqlite")
    fg.DB_PATH = tmp
    if hasattr(fg, "_DB"):
        fg._DB = None
    fake_rpc_calls = []
    try:
        fg.migrate_schema()
        likes = {"records": [
            {"value": {"subject": {"uri": f"at://did:plc:a{i}/app.bsky.feed.post/p{i}"}}}
            for i in range(3)] + [
            {"value": {"subject": {"uri": "at://did:plc:a0/app.bsky.feed.post/dup"}}}]}
        fg.rpc = lambda *a, **k: (fake_rpc_calls.append(a), likes)[1]
        n = fg.seed_cold_start("did:plc:newbie")
        assert n == 3, n  # 4 records -> 3 distinct authors
        rows = fg.db().execute(
            "SELECT COUNT(*) FROM user_affinity WHERE requester_did='did:plc:newbie'"
        ).fetchone()[0]
        assert rows == 3, rows
        assert fg.seed_cold_start("did:plc:newbie") == 0  # one-shot via meta flag
        assert fg.seed_cold_start(None) == 0  # no DID -> no-op
        assert len(fake_rpc_calls) == 1  # no re-fetch after flag set
    finally:
        fg.DB_PATH = old
        if hasattr(fg, "_DB"):
            fg._DB = None
        if _os.path.exists(tmp):
            _os.remove(tmp)
    print("ok   cold-start seeds affinity once from public likes")


def test_metrics_log_written():
    """refresh() writes a metrics_log row per feed; never raises."""
    import tempfile, os as _os
    if not hasattr(fg, "log_metrics"):
        print("skip test_metrics_log (module lacks log_metrics)")
        return
    old = fg.DB_PATH
    tmp = tempfile.mktemp(suffix=".sqlite")
    fg.DB_PATH = tmp
    if hasattr(fg, "_DB"):
        fg._DB = None
    conn = None
    try:
        fg.migrate_schema()
        picks = {"/r/test": ["uri1", "uri2", "uri3"]}
        scores = {"/r/test": {"uri1": 0.9, "uri2": 0.8, "uri3": 0.5}}
        fg.log_metrics(picks, scores, time.time())
        n = fg.db().execute("SELECT COUNT(*) FROM metrics_log").fetchone()[0]
        assert n == 1, n
        fg.db().execute("DELETE FROM metrics_log")
        fg.db().commit()
        fg.log_metrics(picks, scores, time.time())
        fg.log_metrics({}, {}, time.time() - 91 * 86400 - 1)  # old row pruned
        n2 = fg.db().execute("SELECT COUNT(*) FROM metrics_log").fetchone()[0]
        assert n2 == 1, n2
    finally:
        fg.DB_PATH = old
        if hasattr(fg, "_DB"):
            fg._DB = None
        if conn:
            conn.close()
        if _os.path.exists(tmp):
            _os.remove(tmp)
    print("ok   metrics_log written and pruned correctly")

def test_user_excluded_uris_unions_likes_and_hidden():
    """Liked and not-interested posts are excluded in EVERY feed; served
    posts only when the feed opted in with hide_seen."""
    if not hasattr(fg, "user_excluded_uris"):
        print("ok   user_excluded_uris absent in this module (skipped)")
        return
    now = time.time()
    fg.db().execute("DELETE FROM user_likes WHERE requester_did='did:plc:ex'")
    fg.db().execute("DELETE FROM user_hidden WHERE requester_did='did:plc:ex'")
    fg.db().execute("DELETE FROM served_posts WHERE requester_did='did:plc:ex'")
    fg.db().execute("INSERT INTO user_likes VALUES(?,?,?)",
                    ("did:plc:ex", "at://did:plc:a/app.bsky.feed.post/liked", now))
    fg.db().execute("INSERT INTO user_hidden VALUES(?,?,?,?)",
                    ("did:plc:ex", "at://did:plc:a/app.bsky.feed.post/less",
                     "less", now))
    fg.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                    ("did:plc:ex", "at://did:plc:a/app.bsky.feed.post/served",
                     "for-you", now))
    got = fg.user_excluded_uris("did:plc:ex")
    assert got == {"at://did:plc:a/app.bsky.feed.post/liked",
                   "at://did:plc:a/app.bsky.feed.post/less"}, got
    with_seen = fg.user_excluded_uris("did:plc:ex", True)
    assert with_seen == {"at://did:plc:a/app.bsky.feed.post/liked",
                         "at://did:plc:a/app.bsky.feed.post/less",
                         "at://did:plc:a/app.bsky.feed.post/served"}, with_seen
    assert fg.user_excluded_uris(None) == set()
    print("ok   user_excluded_uris unions likes+hidden, adds served only when asked")


def test_seen_memory_is_shared_across_service_feeds():
    """Seen memory belongs to the SERVICE (one deployment = one database), not
    to a single feed: a post served in any feed is hidden from every other feed
    of the same service, while two separate deployments never share it.
    `hide_seen_ttl_h` bounds how long a post stays hidden, so a heavy scroller
    cannot drain every board at once — past the TTL the post is eligible
    again."""
    if not hasattr(fg, "user_excluded_uris"):
        print("ok   user_excluded_uris absent in this module (skipped)")
        return
    ttl = fg.CFG.get("hide_seen_ttl_h", 24)
    assert ttl and ttl > 0, "hide_seen_ttl_h must be configured"
    now = time.time()
    other = next(f["rkey"] for f in fg.CFG["feeds"] if f["rkey"] != FORYOU)
    fg.db().execute("DELETE FROM served_posts WHERE requester_did=?",
                    ("did:plc:shared",))
    fg.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                    ("did:plc:shared",
                     "at://did:plc:a/app.bsky.feed.post/seen-in-other-feed",
                     other, now - 3600))
    fg.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                    ("did:plc:shared", "at://did:plc:a/app.bsky.feed.post/stale",
                     other, now - (ttl + 1) * 3600))
    got = fg.user_excluded_uris("did:plc:shared", True)
    assert "at://did:plc:a/app.bsky.feed.post/seen-in-other-feed" in got, got
    assert "at://did:plc:a/app.bsky.feed.post/stale" not in got, got
    assert fg.user_excluded_uris("did:plc:unrelated", True) == set()
    print(f"ok   seen memory shared across feeds of this service (ttl={ttl}h)")


def test_user_excluded_uris_protects_owner_posts():
    """Owner posts are protected from OTHER readers' exclusions — the feeds
    must keep serving them at the configured owner ratio — while the owner
    reading their own feeds is a reader like any other: a post they already
    saw or liked is a repeat for THEM, and their own posts never come back
    once served, however old that serve is.
    """
    if not hasattr(fg, "user_excluded_uris"):
        print("ok   user_excluded_uris absent in this module (skipped)")
        return
    now = time.time()
    owner = fg.CFG["owner"]["did"]
    reader = "did:plc:ownerprotectreader"
    mine = "at://did:plc:ownerpost/app.bsky.feed.post/mine"
    old_seen = "at://did:plc:ownerpost/app.bsky.feed.post/old-seen"
    ttl = fg.CFG.get("hide_seen_ttl_h", 24) or 24
    db_insert_post(mine, owner)
    db_insert_post(old_seen, owner)
    for did in (owner, reader):
        for table in ("user_likes", "user_hidden", "served_posts"):
            fg.db().execute("DELETE FROM %s WHERE requester_did=?" % table,
                            (did,))
    fg.db().execute("INSERT INTO user_likes VALUES(?,?,?)", (owner, mine, now))
    fg.db().execute("INSERT INTO user_hidden VALUES(?,?,?,?)",
                    (owner, mine, "less", now))
    # Served to the owner longer ago than hide_seen_ttl_h: still a repeat.
    fg.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                    (owner, old_seen, "probe", now - (ttl + 1) * 3600))
    got = fg.user_excluded_uris(owner, True, owner)
    assert mine in got, got
    assert old_seen in got, got
    # A different reader who liked / was served the same owner posts still
    # gets them: the owner ratio is what feeds other people.
    fg.db().execute("INSERT INTO user_likes VALUES(?,?,?)", (reader, mine, now))
    fg.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                    (reader, old_seen, "probe", now - 3600))
    assert fg.user_excluded_uris(reader, True, owner) == set()
    print("ok   owner posts protected for other readers, hidden as the "
          "owner's own repeats")


def test_owner_repeats_survive_the_starvation_floor():
    """The owner's own already-seen posts are dropped from the board even
    when the floor would otherwise re-append the excluded posts: the floor
    exists so a heavy reader is not starved of other people's content."""
    if not hasattr(fg, "apply_user_exclusions") or \
            not hasattr(fg, "author_of_uris"):
        print("ok   exclusion helpers absent in this module (skipped)")
        return
    owner = fg.CFG["owner"]["did"]
    mine = "at://did:plc:ownerpost/app.bsky.feed.post/floor-own"
    theirs = "at://did:plc:someone/app.bsky.feed.post/floor-other"
    db_insert_post(mine, owner)
    db_insert_post(theirs, "did:plc:someone")
    authors = fg.author_of_uris([mine, theirs])
    assert authors.get(mine) == owner, authors
    board = [mine, theirs]
    got = fg.apply_user_exclusions(board, {mine, theirs}, hard_hide=True,
                                   min_board=20, never={mine})
    assert mine not in got, got
    assert got == [theirs], got
    # `never` alone (nothing else excluded) still drops the post
    assert fg.apply_user_exclusions(board, set(), never={mine}) == [theirs]
    print("ok   owner's own repeats are dropped past the starvation floor")


def test_apply_user_exclusions_tail_vs_hard_hide():
    """Tail ordering for the window feeds, hard hide + starvation floor for
    the For You feeds."""
    if not hasattr(fg, "apply_user_exclusions"):
        print("ok   apply_user_exclusions absent in this module (skipped)")
        return
    board = ["at://d/a", "at://d/b", "at://d/c"]
    assert fg.apply_user_exclusions(board, {"at://d/a"}) == \
        ["at://d/b", "at://d/c", "at://d/a"]
    assert fg.apply_user_exclusions(board, {"at://d/a"}, True, 2) == \
        ["at://d/b", "at://d/c"]
    assert fg.apply_user_exclusions(board, {"at://d/a", "at://d/b"}, True, 3) == \
        ["at://d/c", "at://d/a", "at://d/b"]
    assert fg.apply_user_exclusions(board, set()) == board
    print("ok   apply_user_exclusions tails, hard-hides, and floors correctly")


def test_extract_terms_and_negative_penalty():
    """Terms come out of a post body minus stopwords; the penalty is a
    saturating down-rank, never a hide."""
    if not hasattr(fg, "extract_terms"):
        print("ok   extract_terms absent in this module (skipped)")
        return
    terms = fg.extract_terms("Photography and the darkroom")
    assert "photography" in terms and "darkroom" in terms, terms
    assert "the" not in terms and "and" not in terms, terms
    assert fg.negative_term_penalty("a clickbait post", {"clickbait": 1}, 0.25) == 0.75
    assert fg.negative_term_penalty("clean post", {"clickbait": 1}, 0.25) == 1.0
    assert fg.negative_term_penalty("clickbait " * 5, {"clickbait": 9}, 0.25) == 0.5
    print("ok   extract_terms + saturating negative_term_penalty")


def test_less_event_hides_post_and_sinks_author():
    """A requestLess event: hides the post for that requester, drives the
    author to the affinity floor, and teaches the post's vocabulary."""
    if not hasattr(fg, "process_interactions") or not hasattr(fg, "learn_negative_terms"):
        print("ok   less-learning absent in this module (skipped)")
        return
    post_uri = "at://did:plc:authorx/app.bsky.feed.post/lessme"
    fg.db().execute("DELETE FROM interaction_events WHERE requester_did='did:plc:user9'")
    fg.db().execute("DELETE FROM user_hidden WHERE requester_did='did:plc:user9'")
    fg.db().execute("DELETE FROM user_negative_terms WHERE requester_did='did:plc:user9'")
    fg.db().execute("DELETE FROM user_affinity WHERE requester_did='did:plc:user9'")
    fg.db().execute(
        "INSERT OR REPLACE INTO posts(uri, cid, author_did, author_handle, "
        "indexed_at, record_json, first_seen, last_seen, text) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (post_uri, "c", "did:plc:authorx", "h.test", "2026-09-17T00:00:00Z",
         "{}", time.time(), time.time(), "endless clickbait spam"))
    fg.db().execute(
        "INSERT INTO interaction_events(post_uri, feed_rkey, kind, created_at, "
        "requester_did) VALUES(?,?,?,?,?)",
        (post_uri, "x-foryou", "less", time.time(), "did:plc:user9"))
    fg.process_interactions()
    hidden = fg.db().execute(
        "SELECT COUNT(*) FROM user_hidden WHERE requester_did='did:plc:user9' "
        "AND post_uri=?", (post_uri,)).fetchone()[0]
    aff = fg.db().execute(
        "SELECT score FROM user_affinity WHERE requester_did='did:plc:user9' "
        "AND author_did='did:plc:authorx'").fetchone()
    terms = fg.get_negative_terms("did:plc:user9")
    assert hidden == 1, hidden
    assert aff and aff[0] <= fg.AFFINITY_MIN + 1e-9, aff
    assert "clickbait" in terms and "spam" in terms, terms
    # idempotent: a processed event is never folded twice
    fg.process_interactions()
    aff2 = fg.db().execute(
        "SELECT score FROM user_affinity WHERE requester_did='did:plc:user9' "
        "AND author_did='did:plc:authorx'").fetchone()[0]
    assert aff2 == aff[0], (aff2, aff[0])
    print("ok   requestLess hides the post, sinks the author to the floor, learns terms")


def test_every_feed_hides_seen_posts():
    """Per-user seen-hiding on EVERY feed, not just the personal ones: a feed
    added later without hide_seen would silently go back to repeating posts, so
    this is the guard, and hide_min_board is the floor that keeps a drained
    board from serving nothing."""
    missing = [f["rkey"] for f in fg.CFG["feeds"] if not f.get("hide_seen")]
    assert not missing, f"feeds without hide_seen: {missing}"
    assert fg.CFG.get("hide_min_board"), "hide_min_board floor is required"
    print(f"ok   all {len(fg.CFG['feeds'])} feeds declare hide_seen "
          f"(floor={fg.CFG['hide_min_board']})")


def test_keyword_weights_learner_is_config_gated():
    """Adaptive keyword weights: off unless the config opts in, and then the
    engine seeds `keyword_weights` from `topic_keywords`, learns from engaged
    posts with an EMA, and decays toward per-source floors. A deployment that
    leaves the block out must never write the table."""
    con = fg.db()

    def rows():
        return {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT keyword, weight, hits, source FROM keyword_weights")}

    old_kws = fg.CFG.get("topic_keywords")
    old_kw = fg.CFG.get("keyword_weights")
    try:
        fg.CFG["topic_keywords"] = ["kwtest-alpha", "kwtest-beta"]
        with fg.DB_LOCK:
            con.execute("DELETE FROM keyword_weights")
        # disabled (the default): the table is never touched
        fg.CFG["keyword_weights"] = {"enabled": False}
        fg.kw_init()
        assert fg.kw_update(con, "a post about kwtest-alpha", time.time()) == 0
        assert rows() == {}, rows()
        # enabled: config terms seed at seed_weight with source='config'
        fg.CFG["keyword_weights"] = {"enabled": True}
        assert fg.kw_init() == 2, "both config terms seed"
        assert fg.kw_init() == 0, "seeding is idempotent"
        weight, hits, source = rows()["kwtest-alpha"]
        assert (source, hits) == ("config", 0), rows()
        assert abs(weight - 1.0) < 1e-9, rows()
        # an engaged post counts a hit and pulls the weight toward the EMA's
        # fixed point (1.0): a decayed term recovers, a fresh 1.0 stays put
        now = time.time()
        assert fg.kw_update(con, "loving #kwtest-alpha today", now) == 1
        weight1, hits1, _ = rows()["kwtest-alpha"]
        assert hits1 == 1 and abs(weight1 - 1.0) < 1e-9, rows()
        with fg.DB_LOCK:
            con.execute("UPDATE keyword_weights SET weight=0.5 WHERE keyword=?",
                        ("kwtest-alpha",))
        fg.kw_update(con, "kwtest-alpha again", now)
        weight2, hits2, _ = rows()["kwtest-alpha"]
        assert hits2 == 2 and 0.5 < weight2 <= 1.0, rows()
        assert fg.kw_update(con, "nothing topical here", now) == 0
        # decay: 'config' terms floor higher than learned 'auto' ones
        with fg.DB_LOCK:
            con.execute("INSERT OR REPLACE INTO keyword_weights"
                        "(keyword, weight, hits, last_seen, source) VALUES"
                        "('kwtest-gamma', 0.9, 1, ?, 'auto')", (now - 365 * 86400,))
        fg.kw_decay(con, now)
        assert 0.5 <= rows()["kwtest-alpha"][0] <= weight2, rows()
        assert 0.1 <= rows()["kwtest-gamma"][0] <= 0.9, rows()
        # known terms = config terms + learned ones above the weight floor
        known = fg.kw_all(con)
        assert {"kwtest-alpha", "kwtest-beta"} <= set(known), known
        assert "kwtest-gamma" not in known, known        # decayed below the floor
        assert fg.kw_extract("Loving #KWTest-Alpha", ["kwtest-alpha"]) == ["kwtest-alpha"]
    finally:
        fg.CFG["topic_keywords"], fg.CFG["keyword_weights"] = old_kws, old_kw
        with fg.DB_LOCK:
            con.execute("DELETE FROM keyword_weights")
    print("ok   keyword weights: config-gated seed, EMA learning, floors, terms")


def test_corpus_keeps_window_sourced_posts_past_the_cap():
    """A `since_hours` search exists to reach posts the rolling corpus cannot
    hold. The recency trim used to evict exactly those: the week source returned
    2.5k in-window posts and the corpus stayed at ~31 hours of supply."""
    old_cap = fg.CFG.get("corpus")
    try:
        fg.CFG["corpus"] = {"cap": 5}
        recent = [post(likes=1, did="did:plc:r", key="r%d" % i, hours=1)
                  for i in range(10)]
        window = [post(likes=1, did="did:plc:w", key="w%d" % i, hours=100)
                  for i in range(2)]
        for p in window:
            p["_src_key"] = "wk"
            p["_src_protect"] = True
        stale = post(likes=1, did="did:plc:s", key="s0", hours=100)  # not protected
        fg.merge_corpus(recent + window + [stale], 168)
        uris = set(fg.CORPUS["by_uri"])
        kept_w = [u for u in uris if "/w" in u]
        assert len(kept_w) == 2, (len(kept_w), len(uris))
        assert not any("/s0" in u for u in uris), "unprotected old post must be trimmed"
        assert len(uris) <= 5, len(uris)
        print("ok  window-sourced posts survive the corpus cap (%d kept, cap 5)"
              % len(uris))
    finally:
        fg.CORPUS["by_uri"].clear()
        fg.CFG["corpus"] = old_cap


def test_corpus_seed_is_config_gated():
    """Corpus seeding: off unless the config opts in, and then it loads only the
    rows inside the configured window. The block exists because CORPUS is module
    state — every restart used to rank a thin incremental crawl while the posts
    table held the same window on disk, and supply-bound boards served a fraction
    of their depth for hours."""
    if not hasattr(fg, "seed_corpus_from_db"):
        print("ok   corpus seed absent in this module (skipped)")
        return
    con = fg.db()
    fresh = "at://did:plc:seedtest/app.bsky.feed.post/fresh"
    stale = "at://did:plc:seedtest/app.bsky.feed.post/stale"
    now = datetime.now(timezone.utc)
    rows = [
        (fresh, "c", "did:plc:seedtest", "seed.test",
         now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         json.dumps({"uri": fresh, "cid": "c", "author": {"did": "did:plc:seedtest"},
                     "record": {"text": "fresh"}, "likeCount": 1}),
         time.time(), time.time(), "fresh"),
        (stale, "c", "did:plc:seedtest", "seed.test",
         (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         json.dumps({"uri": stale, "cid": "c", "author": {"did": "did:plc:seedtest"},
                     "record": {"text": "stale"}, "likeCount": 1}),
         time.time(), time.time(), "stale"),
    ]
    old_cfg = fg.CFG.get("corpus_seed")
    saved = dict(fg.CORPUS["by_uri"])
    try:
        with fg.DB_LOCK:
            con.executemany(
                "INSERT OR REPLACE INTO posts(uri, cid, author_did, author_handle, "
                "indexed_at, record_json, first_seen, last_seen, text) "
                "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        fg.CORPUS["by_uri"].clear()
        # absent block (the default): nothing is loaded
        fg.CFG.pop("corpus_seed", None)
        assert fg.seed_corpus_from_db() == 0, "absent corpus_seed must not fill"
        assert fresh not in fg.CORPUS["by_uri"]
        # disabled explicitly: still nothing
        fg.CFG["corpus_seed"] = {"enabled": False, "hours": 6}
        assert fg.seed_corpus_from_db() == 0
        # enabled: the window loads, the older post does not
        fg.CFG["corpus_seed"] = {"enabled": True, "hours": 6, "max_posts": 500}
        n = fg.seed_corpus_from_db()
        assert fresh in fg.CORPUS["by_uri"], "enabled seed must load the window"
        assert stale not in fg.CORPUS["by_uri"], "seed must respect its window"
        assert n >= 1, n
        # the seeded entry is a real post view, not a bare uri
        assert fg.CORPUS["by_uri"][fresh]["record"]["text"] == "fresh"
    finally:
        fg.CFG["corpus_seed"] = old_cfg if old_cfg is not None else \
            fg.CFG.pop("corpus_seed", None)
        fg.CORPUS["by_uri"] = saved
        with fg.DB_LOCK:
            con.execute("DELETE FROM posts WHERE uri IN (?,?)", (fresh, stale))
    print("ok   corpus seed: config-gated, window-bounded, loads real post views")


def test_language_axis_and_author_cap_invariants():
    """A feed that filters by language is the one place a silently wrong config
    value cannot fail loudly: `require_lang` compares primary subtags, so a full
    tag ("es-MX") would match nothing and an untagged post is dropped, while the
    owner's untagged posts still come through the bypass. Pin the shape here, and
    keep every feed's per-author cap explicit so the knob never drifts back to an
    implicit default."""
    bad_tags = {}
    for f in fg.CFG["feeds"]:
        for key in ("require_lang", "exclude_langs"):
            vals = f.get(key) or []
            wrong = [v for v in vals
                     if not isinstance(v, str) or "-" in v or v != v.lower()]
            if wrong:
                bad_tags[f["rkey"]] = {key: wrong}
    assert not bad_tags, f"language lists must be lowercase primary subtags: {bad_tags}"
    lang_feeds = [f["rkey"] for f in fg.CFG["feeds"]
                  if f.get("require_lang") or f.get("exclude_langs")]
    no_hide = [r for r in lang_feeds
               if not next(f for f in fg.CFG["feeds"] if f["rkey"] == r).get("hide_seen")]
    assert not no_hide, f"language-filtered feeds must hide seen posts: {no_hide}"
    missing_cap = [f["rkey"] for f in fg.CFG["feeds"] if not f.get("max_per_author")]
    assert not missing_cap, f"feeds without an explicit max_per_author: {missing_cap}"
    print(f"ok   language axis + author cap: {len(lang_feeds)} language feeds, "
          f"all {len(fg.CFG['feeds'])} feeds declare max_per_author")


if __name__ == "__main__":
    test_rank_score_velocity_decays_with_age()
    test_rank_score_taste_multiplier_on_any_mode()
    test_rank_score_hashtag_beats_plain()
    test_author_prior_multiplies()
    test_select_returns_scored_tuples()
    test_select_owner_ratio_cap()
    test_hide_seen_scope_can_be_per_feed()
    test_topic_post_floor_is_continuous()
    test_owner_gate_bypass_can_be_disabled()
    test_sources_allowlist_refuses_the_list()
    test_flat_ranking_has_no_time_decay()
    test_corpus_cap_comes_from_config()
    test_source_can_be_scoped_to_named_feeds()
    test_search_blocks_normalise_both_shapes()
    test_adaptive_gate_does_not_rescan_candidates()
    test_owner_picks_respect_max_per_author()
    test_owner_lang_gate_filters_only_the_language_axis()
    test_owner_lang_gate_sniff_keeps_untagged_spanish()
    test_config_reload_picks_up_a_changed_file()
    test_cold_start_can_seed_from_follows()
    test_relevance_telemetry_is_persisted()
    test_feed_owner_override_is_per_feed_and_off_by_default()
    test_min_topic_affinity_floor_drops_offtopic_authors()
    test_engagement_snapshots_schema()
    test_snapshot_engagements_write_throttle_prune()
    test_select_trending_two_track_interleave()
    test_select_trending_without_vintage_is_legacy()
    test_process_interactions_builds_affinity()
    test_owner_gravity_drifts_toward_taste()
    test_personalize_reranks_by_user_affinity()
    test_personalize_injects_explore_slots()
    test_event_kinds_map()
    test_gravity_two_rates()
    test_build_taste_ext_stores_per_source_counts()
    test_select_excludes_foreign_owner()
    test_blocked_dids_never_blocks_local_owner()
    test_verify_jwt_verifies_es256k_signature()
    test_cold_start_seeds_affinity()
    test_metrics_log_written()
    test_user_excluded_uris_unions_likes_and_hidden()
    test_seen_memory_is_shared_across_service_feeds()
    test_user_excluded_uris_protects_owner_posts()
    test_owner_repeats_survive_the_starvation_floor()
    test_apply_user_exclusions_tail_vs_hard_hide()
    test_extract_terms_and_negative_penalty()
    test_less_event_hides_post_and_sinks_author()
    test_every_feed_hides_seen_posts()
    test_owner_bonus_mode_relative_is_scale_invariant()
    test_owner_bonus_mode_off_leaves_the_board_alone()
    test_chrono_ignores_owner_promotion()
    test_corpus_keeps_window_sourced_posts_past_the_cap()
    test_corpus_seed_is_config_gated()
    test_language_axis_and_author_cap_invariants()
    test_keyword_weights_learner_is_config_gated()
    print("ALL PASS")
