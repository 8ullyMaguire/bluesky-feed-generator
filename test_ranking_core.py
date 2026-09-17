#!/usr/bin/env python3
"""Tests for ranking_core.py. Run: python3 test_ranking_core.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ranking_core import (log_engagement, velocity, keyword_score,      # noqa: E402
                          post_keyword_affinity, author_prior,
                          recent_rate_score, interleave_tracks)

W = {"w_likes": 1.0, "w_reposts": 3.0, "w_quotes": 2.0, "w_replies": 0.5}


def test_log_engagement_compresses():
    big = log_engagement({"likeCount": 500, "repostCount": 100}, W)
    small = log_engagement({"likeCount": 5, "repostCount": 1}, W)
    assert big > small
    assert big < 10 * small, big / small
    print("ok  log_engagement compresses the popularity gap")


def test_velocity_decays_and_floors():
    fresh = velocity(0.0, 0.8)
    old = velocity(24.0, 0.8)
    assert fresh > old
    assert fresh <= 1.0
    print("ok  velocity decays with age and floors brand-new posts")


def test_recent_rate_score():
    # rate = delta/dt, then x 0.5 ** (age / half_life)
    s = recent_rate_score(30.0, 10.0, 6.0, 72.0)   # 30 engagements over 10h, 6h old
    assert abs(s - 3.0 * 0.5 ** (6.0 / 72.0)) < 1e-9
    s72 = recent_rate_score(30.0, 10.0, 72.0, 72.0)
    assert abs(s72 - 1.5) < 1e-9                   # exactly half at the half-life
    # negative or zero delta clamps to 0: stagnation scores nothing
    assert recent_rate_score(-10.0, 30.0, 10.0, 72.0) == 0.0
    assert recent_rate_score(0.0, 30.0, 10.0, 72.0) == 0.0
    print("ok  recent_rate_score: rate x half-life decay, clamped at 0")


def test_interleave_tracks():
    fresh = [("f1", 4.0), ("f2", 3.0), ("f3", 2.0), ("f4", 1.0),
             ("f5", 0.5), ("f6", 0.4), ("f7", 0.3), ("f8", 0.2)]
    vintage = [("v1", 2.0), ("v2", 1.0)]
    uris = [u for u, _ in interleave_tracks(fresh, vintage, 0.2)]
    assert uris == ["f1", "f2", "f3", "f4", "v1", "f5", "f6", "f7", "f8", "v2"], uris
    # a dry track is skipped; the other fills everything
    assert [u for u, _ in interleave_tracks(fresh, [], 0.2)] ==         ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8"]
    assert [u for u, _ in interleave_tracks([], vintage, 0.2)] == ["v1", "v2"]
    print("ok  interleave_tracks: every 5th slot vintage, deterministic fill")


def test_keyword_score_hashtag_vs_normal():
    kws = ["gamedev", "pixelart"]
    tags, d1 = keyword_score("loving #gamedev #pixelart life", kws)
    plain, d2 = keyword_score("loving gamedev pixelart life", kws)
    assert d1 == 2 and d2 == 2
    assert abs(tags - 2.0) < 1e-9
    assert abs(plain - 1.4) < 1e-9
    assert tags > plain
    print("ok  hashtags weigh 1.0, plain text 0.7")


def test_post_keyword_affinity_saturates():
    assert post_keyword_affinity(0.0) == 0.0
    assert post_keyword_affinity(2.0) == 0.5
    assert post_keyword_affinity(8.0) == 1.0
    print("ok  post keyword affinity saturates at 4 weighted hits")


def test_author_prior_bounds():
    assert author_prior(10.0, 2, 10.0) == 1.0
    assert author_prior(10.0, 5, None) == 1.0
    p_hi = author_prior(40.0, 10, 10.0)
    assert 1.2 <= p_hi <= 1.5
    p_lo = author_prior(1.0, 10, 10.0)
    assert 0.7 <= p_lo <= 0.8
    print("ok  author prior: neutral on cold data, gentle tilt after")


if __name__ == "__main__":
    test_log_engagement_compresses()
    test_velocity_decays_and_floors()
    test_recent_rate_score()
    test_interleave_tracks()
    test_keyword_score_hashtag_vs_normal()
    test_post_keyword_affinity_saturates()
    test_author_prior_bounds()
    print("ALL PASS")
