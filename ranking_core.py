#!/usr/bin/env python3
"""Shared ranking primitives used by the feed-generator service.

Deployed as an identical copy in both directories (sha256 must match; the
deploy step checks it). Pure functions only: no DB, no network, no
module-level config — everything is passed in. Stdlib only.
"""
import math
import re

HASHTAG_RE = re.compile(r"#(\w+)")


def log_engagement(p, w):
    """Compressed engagement: log1p of the weighted raw counts.

    Linear engagement made every feed a popularity board where the same big
    accounts always won. On the log scale a 500-like post beats a 5-like
    post, but by ~2x instead of ~100x, so smaller accounts can surface.
    """
    raw = (p.get("likeCount", 0) * w.get("w_likes", 1.0)
           + p.get("repostCount", 0) * w.get("w_reposts", 3.0)
           + p.get("quoteCount", 0) * w.get("w_quotes", 2.0)
           + p.get("replyCount", 0) * w.get("w_replies", 0.5))
    return math.log1p(max(raw, 0.0))


def velocity(age_h, gravity, half_life_h=2.0):
    """HN-style time penalty: 1 / (age + half_life) ** gravity.

    The half-life floor replaces the old max(age, floor)**decay hack: a
    brand-new post scores about 1/2**gravity instead of exploding, so no
    age_floor config is needed.
    """
    return 1.0 / (max(age_h, 0.0) + half_life_h) ** gravity


def recent_rate_score(delta_e, dt_h, age_h, half_life_h):
    """Rediscovery score: recent engagement RATE x gentle age decay.

    delta_e = E_now - E_then over dt_h hours (dt_h >= the rate window).
    Age never penalizes directly; stagnation does -- a flat post scores 0
    no matter how much engagement it piled up historically.
    """
    if dt_h <= 0 or delta_e <= 0:
        return 0.0
    return (delta_e / dt_h) * 0.5 ** (age_h / half_life_h)


def interleave_tracks(fresh, vintage, fraction):
    """Deterministic two-track merge for the trending feed.

    Every 1/fraction-th slot (1-indexed) takes the next vintage pick;
    other slots take the next fresh pick. A dry track is skipped and the
    other fills the remaining slots. Same-score ties keep input order,
    matching the deterministic-board rule in select().
    """
    if not vintage:
        return list(fresh)
    if not fresh:
        return list(vintage)
    step = max(1, round(1.0 / fraction)) if fraction else 0
    out, fi, vi, i = [], 0, 0, 0
    while fi < len(fresh) or vi < len(vintage):
        if step and (i + 1) % step == 0 and vi < len(vintage):
            out.append(vintage[vi]); vi += 1
        elif fi < len(fresh):
            out.append(fresh[fi]); fi += 1
        else:
            out.append(vintage[vi]); vi += 1
        i += 1
    return out


def keyword_score(text, keywords, normal_factor=0.7):
    """Weighted keyword hits.

    Hashtag occurrences (#kw) count 1.0 each; plain-text occurrences count
    `normal_factor` each (0.7 per the 2026-09-14 decision: normal keywords
    weigh 0.7 vs hashtags). Returns (weighted_score, distinct_keywords_hit).
    """
    if not text:
        return 0.0, 0
    low = text.lower()
    tags = set(HASHTAG_RE.findall(low))
    score, distinct = 0.0, 0
    for kw in keywords:
        if kw not in low:
            continue
        distinct += 1
        score += 1.0 if kw in tags else normal_factor
    return score, distinct


def post_keyword_affinity(weighted_score, saturate_at=4.0):
    """Saturating 0..1 keyword affinity for a single post.

    Unlike the old weighted_ml_affinity() — which divided by the sum of ALL
    known keyword weights and produced ~0.01 for everything — this
    saturates: 4+ weighted hits is as good as it gets.
    """
    if weighted_score <= 0:
        return 0.0
    return min(1.0, weighted_score / saturate_at)


def author_prior(median_eng, n_posts, list_median, min_posts=3,
                 exponent=0.15, cap=1.5, floor=0.7):
    """Per-author multiplier for consistent over/under-performers.

    Authors whose median post engagement is 4x the list median get
    4**0.15 ~= 1.23; 0.1x gets ~0.71. Neutral (1.0) until min_posts
    observed, so new members are neither punished nor boosted blind.
    """
    if median_eng is None or n_posts < min_posts or not list_median:
        return 1.0
    return max(floor, min(cap, (median_eng / list_median) ** exponent))
