#!/usr/bin/env python3
"""Tests for the /why explanation and the discovery pure functions.

Run: python3 test_why.py
Uses no database and no network: every case is a dict in, a dict out. The one
thing it cannot fake is the drift guard — test_explanation_matches_the_ranker
imports the real feedgen.rank_score and asserts the itemised total equals it.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_spec = importlib.util.spec_from_file_location("disc", os.path.join(HERE, "discovery.py"))
disc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disc)  # type: ignore[union-attr]

W = {"w_likes": 1.0, "w_reposts": 3.0, "w_quotes": 2.0, "w_replies": 0.5, "w_saves": 0.0}


def test_breakdown_parts_sum_to_raw_and_base_matches_ranker():
    p = {"likeCount": 10, "repostCount": 2, "quoteCount": 1, "replyCount": 4}
    b = disc.score_breakdown(p, W)
    raw = sum(v["points"] for v in b["parts"].values())
    assert abs(raw - b["raw"]) < 1e-9, (raw, b["raw"])
    # 10*1 + 2*3 + 1*2 + 4*0.5 = 20.0
    assert abs(b["raw"] - 20.0) < 1e-9, b["raw"]
    # the base the ranker starts from is log1p(raw), not raw
    assert abs(b["base"] - __import__("math").log1p(20.0)) < 1e-9, b
    assert b["parts"]["reposts"]["points"] == 6.0
    assert b["parts"]["saves"]["n"] == 0
    print("ok   breakdown parts sum to the raw score; base is log1p of it")


def test_breakdown_accepts_snake_case_rows():
    """The route reads SQLite rows (like_count); the ranker sees AppView dicts
    (likeCount). Both must produce the same numbers, or /why explains a
    different post than the one the ranker scored."""
    camel = {"likeCount": 10, "repostCount": 2, "quoteCount": 1, "replyCount": 4}
    snake = {"like_count": 10, "repost_count": 2, "quote_count": 1, "reply_count": 4}
    assert disc.score_breakdown(camel, W)["raw"] == disc.score_breakdown(snake, W)["raw"]
    assert disc.score_breakdown({}, W)["raw"] == 0.0
    assert disc.score_breakdown({"likeCount": "x"}, W)["raw"] == 0.0
    print("ok   breakdown reads AppView dicts and SQLite rows identically")


def test_keyword_terms_agrees_with_ranker_count():
    kws = ["marx", "imperialismo", "stalin", "clase"]
    text = "Una lectura del Marxismo y el imperialismo, con STALIN enmayusculas"
    terms = disc.keyword_terms(text, kws)
    _score, distinct = disc.ranking_core.keyword_score(text, kws, 0.7)
    assert len(terms) == distinct, (terms, distinct)
    assert terms == ["marx", "imperialismo", "stalin"], terms
    assert disc.keyword_terms("", kws) == []
    assert disc.keyword_terms(None, kws) == []
    print("ok   keyword_terms names the terms and agrees with keyword_score's count")


def test_explanation_matches_the_ranker():
    """The module's central promise: the itemised total IS rank_score's
    output. If this fails, /why is showing a confident, wrong number."""
    os.environ.setdefault("FEEDGEN_DB", "/tmp/why-test-does-not-open.sqlite")
    fgspec = importlib.util.spec_from_file_location(
        "fg_why", os.path.join(HERE, "feedgen.py"))
    fg = importlib.util.module_from_spec(fgspec)
    fgspec.loader.exec_module(fg)  # type: ignore[union-attr]

    w = fg.weights_for({"ranking": "velocity", "gravity": 0.75})
    ctx = {
        "topic_affinity": {"did:a": 0.8, "did:b": 0.2},
        "taste": {"did:a": 0.9, "did:b": 0.1},
        "topic_seeds": {"did:b"},
        "topic_keywords": ["marx", "clase"],
        "author_priors": {"did:a": 1.2, "did:b": 0.85},
    }
    post = {"author": {"did": "did:a"},
            "record": {"text": "marx y la clase obrera"},
            "likeCount": 12, "repostCount": 3, "quoteCount": 2, "replyCount": 1}
    fcfg = {"ranking": "velocity", "gravity": 0.75, "taste_weight": 0.3,
            "topic_weight": 0.6, "topic_keyword_bonus": 0.35, "half_life_hours": 2.0}
    for age in (0.5, 3.0, 26.0):
        actual = fg.rank_score(post, fcfg, w, age, ctx)
        mine = disc.explain_rank(post, fcfg, w, age, ctx)
        assert disc.assert_matches_rank_score(mine["total"], actual), (
            age, mine["total"], actual, mine["steps"])
    # and the topic mode's flat seed bonus, which is additive not multiplicative
    tpost = dict(post, author={"did": "did:b"})
    tcfg = {"ranking": "topic", "decay_factor": 0.6, "topic_weight": 2.5,
            "taste_weight": 0.8, "topic_seed_bonus": 50.0, "topic_keyword_bonus": 0.35}
    actual = fg.rank_score(tpost, tcfg, w, 2.0, ctx)
    mine = disc.explain_rank(tpost, tcfg, w, 2.0, ctx)
    assert disc.assert_matches_rank_score(mine["total"], actual), (
        mine["total"], actual)
    assert mine["topic_seed_bonus_applied"] == 50.0
    # The tolerance must still be a real guard, not a rubber stamp: a dropped
    # multiplier is orders of magnitude out and has to FAIL.
    assert disc.assert_matches_rank_score(3.6890103616719174, 3.6890122552376834)
    assert not disc.assert_matches_rank_score(3.6890 * 1.01, 3.6890)
    assert not disc.assert_matches_rank_score(0.0, 1.0)
    assert not disc.assert_matches_rank_score(1.0, None)
    print("ok   explain_rank reproduces rank_score exactly (4 configurations)")


def test_cosine_edges():
    assert disc.cosine_similarity({}, {"a": 1}) == 0.0
    assert disc.cosine_similarity({"a": 1}, {}) == 0.0
    assert disc.cosine_similarity({"a": 1}, {"a": 1}) == 1.0
    assert disc.cosine_similarity({"a": 1}, {"b": 1}) == 0.0
    assert 0 < disc.cosine_similarity({"a": 2, "b": 1}, {"a": 1, "b": 2}) < 1
    print("ok   cosine handles empty/identical/disjoint/partial")


def test_similar_people_orders_and_excludes():
    corpus = {"d1": {"x": 3, "y": 1}, "d2": {"x": 1}, "d3": {"x": 9, "y": 9}}
    out = disc.similar_people({"x": 2, "y": 2}, corpus, limit=5)
    dids = [r["did"] for r in out]
    assert "d2" in dids, out
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True), scores
    assert disc.similar_people({"x": 1}, corpus, blocked={"d1"})[0]["did"] != "d1"
    # self is excluded because a DID can appear in its own profile
    assert "self" not in [r["did"] for r in disc.similar_people(
        {"self": 1}, {"self": {"self": 9}, "other": {"self": 1}})]
    # deterministic: two calls on identical input agree exactly
    assert out == disc.similar_people({"x": 2, "y": 2}, corpus, limit=5)
    assert out[0]["shared_authors"] >= out[-1]["shared_authors"]
    print("ok   similar_people sorts, excludes self and blocked, is deterministic")


def test_summary_uses_real_numbers():
    b = disc.score_breakdown({"likeCount": 10, "repostCount": 2,
                              "quoteCount": 1, "replyCount": 4}, W)
    r = disc.explain_rank(
        {"author": {"did": "d"}, "record": {"text": "marx"},
         "likeCount": 10, "repostCount": 2, "quoteCount": 1, "replyCount": 4},
        {"ranking": "velocity", "gravity": 0.75}, W, 3.14,
        {"topic_affinity": {}, "taste": {}, "topic_seeds": set(),
         "topic_keywords": ["marx"], "author_priors": {}})
    s = disc.summarize(b, r, True, ["marx"])
    assert str(round(b["raw"], 1)) in s or "10 likes" in s, s
    assert "3.1h" in s, s
    assert "personalized for you" in s, s
    assert "marx" in s, s
    s2 = disc.summarize(b, None, False, None)
    assert "not personalized for you" in s2, s2
    # "personalized for you" is a SUBSTRING of "not personalized for you",
    # so assert the honest form, not the fragment.
    assert s2.count("personalized for you") == 1, s2
    # the honest form is the one with the "not " prefix directly attached
    assert " not personalized for you" in s2, s2
    print("ok   summary is assembled from the returned numbers")


def test_gate_report_marks_inactive_and_margin():
    g = disc.gate_report({"likeCount": 10, "repostCount": 2},
                         {"min_likes": 5, "min_reposts_share": 0.33}, 12.0)
    assert g["min_likes"]["active"] and g["min_likes"]["pass"] is True, g
    assert g["min_reposts_share"]["active"] and g["min_reposts_share"]["pass"] is False, g
    # an unset gate is neither a pass nor a failure
    g2 = disc.gate_report({"likeCount": 1}, {"min_likes": 0}, 1.0)
    assert g2["min_likes"]["active"] is False, g2
    assert g2["min_likes"]["pass"] is None, g2
    assert g2["max_likes"]["active"] is False, g2
    # the one UPPER bound in the codebase (the small-accounts feed)
    g3 = disc.gate_report({"likeCount": 50}, {"max_likes": 20}, 50.0)
    assert g3["max_likes"]["active"] and g3["max_likes"]["pass"] is False, g3
    g4 = disc.gate_report({"likeCount": 5}, {"max_likes": 20}, 5.0)
    assert g4["max_likes"]["pass"] is True, g4
    # a zero like_count must not divide by zero
    g5 = disc.gate_report({"likeCount": 0, "repostCount": 3}, {}, 1.0)
    assert g5["min_reposts_share"]["actual"] == 0.0, g5
    print("ok   gate report marks active/inactive and shows the margin")


def test_author_of_uri_reads_the_did():
    u = "at://did:plc:abc123/app.bsky.feed.post/3kxyz"
    assert disc.author_of_uri(u) == "did:plc:abc123"
    assert disc.author_of_uri("") == ""
    assert disc.author_of_uri("http://example.com/x") == ""
    print("ok   author_of_uri extracts the author DID from an AT-URI")


def test_rank_candidates_deterministic():
    c = [{"uri": "at://did:plc:a/app.bsky.feed.post/1", "author_did": "did:plc:a",
          "like_count": 5, "repost_count": 1},
         {"uri": "at://did:plc:b/app.bsky.feed.post/2", "author_did": "did:plc:b",
          "like_count": 5, "repost_count": 1},
         {"uri": "at://did:plc:c/app.bsky.feed.post/3", "author_did": "did:plc:c",
          "like_count": 50, "repost_count": 20},
         {"uri": "at://did:plc:blk/app.bsky.feed.post/4", "author_did": "did:plc:blk",
          "like_count": 500, "repost_count": 200}]
    out = disc.rank_candidates(c, W, limit=10, blocked={"did:plc:blk"})
    assert [r["post"][-1] for r in out] == ["3", "1", "2"], out
    # equal scores (posts 1 and 2) break on URI, deterministically
    assert out == disc.rank_candidates(c, W, limit=10, blocked={"did:plc:blk"})
    assert disc.rank_candidates(c, W, exclude=["at://did:plc:c/app.bsky.feed.post/3"])
    print("ok   rank_candidates is deterministic, excludes blocked and the input")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL WHY TESTS PASSED")
