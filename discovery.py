#!/usr/bin/env python3
"""Discovery helpers for the public pages — pure functions, no I/O, no server.

Every function here takes already-fetched rows and returns plain data, so the
whole module is unit-testable without a database, a network, or a server.

THE RULE THIS MODULE EXISTS TO ENFORCE
-------------------------------------
An explanation is a READ of the ranker, never a reimplementation of it.

`explain_rank` reconstructs the multipliers that `feedgen.rank_score` applies,
then `assert_matches_rank_score` proves the reconstruction equals the real
function's return value. If the ranker ever drifts, the runtime self-check
fails closed: the endpoint reports the base components it can prove and flags
`reconstruction_drift` rather than showing a plausible, wrong explanation.
A parallel implementation that is never checked against the original is how a
user-facing "why" turns into a lie within one release.

Every engagement and keyword number comes from `ranking_core` (the same module
the live ranker imports), never from arithmetic repeated here.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

import ranking_core

# `rank_score` reads this from CFG at call time (topic_keyword_weight_factor).
# The module-level default keeps the pure functions usable without importing
# the engine; the engine passes the live value in via `keyword_normal_factor`.
CFG_FACTOR = 0.7

# The four signals `ranking_core.log_engagement` sums, in its own order. The
# engine's config names them w_likes / w_reposts / w_quotes / w_replies; the
# public key of each is friendlier but maps 1:1.
SIGNALS = (
    ("likes", "likeCount", "w_likes", 1.0),
    ("reposts", "repostCount", "w_reposts", 3.0),
    ("quotes", "quoteCount", "w_quotes", 2.0),
    ("replies", "replyCount", "w_replies", 0.5),
)


def _n(post: Dict[str, Any], camel: str) -> int:
    """Read a count from an AppView post or a `posts` table row.

    The ranker works on AppView dicts (`likeCount`); the route hands us rows
    from SQLite (`like_count`). Accepting both keeps one function honest
    instead of two near-identical ones that drift.
    """
    v = post.get(camel)
    if v is None:
        snake = re_snake(camel)
        v = post.get(snake)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def re_snake(camel: str) -> str:
    """likeCount -> like_count"""
    out = []
    for ch in camel:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _ranker_view(post: Dict[str, Any]) -> Dict[str, Any]:
    """The same post with the counts in the AppView shape the ranker reads.

    Everything else is passed through untouched (author, record), so this is a
    view of one object, not a copy of the ranking logic.
    """
    out = dict(post)
    for _name, camel, _wkey, _dflt in SIGNALS:
        out[camel] = _n(post, camel)
    return out


def score_breakdown(post: Dict[str, Any], weights: Dict[str, float]) -> Dict[str, Any]:
    """Engagement breakdown plus the base score, using the live primitives.

    `raw` is the weighted linear sum; `base` is what the ranker actually
    starts from — `ranking_core.log_engagement(raw)`, i.e. log1p. The engine's
    own `base_score()` is linear and is DEAD CODE (nothing calls it), so the
    log form is the one that decides the ranking and the one shown here.
    """
    parts: Dict[str, Any] = {}
    raw = 0.0
    for name, camel, wkey, dflt in SIGNALS:
        n = _n(post, camel)
        w = float(weights.get(wkey, dflt))
        pts = n * w
        raw += pts
        parts[name] = {"n": n, "weight": w, "points": pts}
    # `saves` is structurally zero: Bluesky exposes no public bookmark count.
    ws = float(weights.get("w_saves", 0.0))
    parts["saves"] = {"n": 0, "weight": ws, "points": 0.0, "note": "not public"}
    # Call the ranker's own base with a dict shaped the way IT reads it, so a
    # malformed count in one of the two accepted shapes cannot make the
    # primitive raise — `_n` already coerced and dropped the bad value.
    base = ranking_core.log_engagement(_ranker_view(post), weights)
    return {
        "raw": raw,
        "base": base,
        "parts": parts,
        "weights": {k: float(v) for k, v in (weights or {}).items()},
    }


def keyword_terms(text: str, keywords: Sequence[str]) -> List[str]:
    """The topic keywords this post's text actually contains.

    The engine's `topic_keyword_hits` returns a COUNT, and
    `ranking_core.keyword_score` returns (weighted, distinct) — neither names
    the terms, and "which words matched" is the part a reader wants. A test
    asserts the length agrees with `keyword_score`'s distinct count, so this
    cannot drift into disagreeing with the ranker.
    """
    if not text:
        return []
    low = text.lower()
    return [k for k in (keywords or ()) if k and k.lower() in low]


def explain_rank(post: Dict[str, Any], fcfg: Dict[str, Any], weights: Dict[str, float],
                 age_h: float, ctx: Dict[str, Any],
                 keyword_normal_factor: float = CFG_FACTOR) -> Dict[str, Any]:
    """Every multiplier `rank_score` applies, itemised, and the total.

    The multiplier list mirrors `feedgen.rank_score` term for term: the taste
    and topic tilts, the pinned-seed bump, the per-post keyword bonus, the
    author prior and the time decay. `assert_matches_rank_score` is the guard
    that keeps this list honest.
    """
    did = (post.get("author") or {}).get("did") or "?"
    mode = fcfg.get("ranking", "flat")
    topicw = float(fcfg.get("topic_weight", 0.0) or 0.0)
    taw = float(fcfg.get("taste_weight", 0.0) or 0.0)
    base = ranking_core.log_engagement(post, weights)

    aff = float((ctx.get("topic_affinity") or {}).get(did, 0.0))
    ta = float((ctx.get("taste") or {}).get(did, 0.0))
    is_seed = did in (ctx.get("topic_seeds") or ())
    eff_aff = 1.0 if is_seed else aff

    steps: List[Dict[str, Any]] = [
        {"step": "engagement", "value": round(base, 6),
         "detail": f"log1p of {round(score_breakdown(post, weights)['raw'], 1)} weighted points"},
    ]
    if topicw:
        steps.append({"step": "topic_affinity", "value": round(1.0 + topicw * eff_aff, 6),
                      "detail": f"author shares {round(eff_aff, 4)} topic words"
                                + (" (pinned seed)" if is_seed else "")})
    if taw:
        steps.append({"step": "owner_taste", "value": round(1.0 + taw * ta, 6),
                      "detail": f"author is {round(ta, 4)} of the curator's top taste"})

    seed_bonus = 0.0
    if mode == "topic" and is_seed:
        seed_bonus = float(fcfg.get("topic_seed_bonus", 0.0) or 0.0)
        if seed_bonus:
            steps.append({"step": "topic_seed_bonus", "value": seed_bonus,
                          "detail": "flat bump for a pinned seed account"})

    if topicw or taw:
        text = ""
        rec = post.get("record")
        if isinstance(rec, dict):
            text = rec.get("text") or ""
        wscore, _distinct = ranking_core.keyword_score(
            text, ctx.get("topic_keywords") or [], keyword_normal_factor)
        pa = ranking_core.post_keyword_affinity(wscore, fcfg.get("keyword_saturate", 4.0))
        kwb = float(fcfg.get("topic_keyword_bonus", 0.0) or 0.0)
        if pa > 0 and kwb:
            steps.append({"step": "post_keywords", "value": round(1.0 + pa * kwb, 6),
                          "detail": "this post's own text hits topic words"})

    prior = (ctx.get("author_priors") or {}).get(did)
    if prior:
        steps.append({"step": "author_prior", "value": round(float(prior), 6),
                      "detail": "consistent over/under-performer vs the list median"})

    # Time: the engine switches on mode, and so do we — same three branches.
    if mode == "trending":
        decay = {"multiplier": ranking_core.velocity(age_h, fcfg.get("decay_factor", 0.8)),
                 "params": f"gravity={fcfg.get('decay_factor', 0.8)}"}
    elif mode in ("velocity", "deep", "foryou", "topic"):
        g = fcfg.get("gravity", fcfg.get("decay_factor", 0.6))
        hl = fcfg.get("half_life_hours", 2.0)
        decay = {"multiplier": ranking_core.velocity(age_h, g, hl),
                 "params": f"gravity={g}, half_life={hl}h"}
    else:
        decay = {"multiplier": 1.0, "params": "no time decay in this mode"}

    total = base
    for s in steps[1:]:
        if s["step"] == "topic_seed_bonus":
            total += s["value"]
        else:
            total *= s["value"]
    total *= decay["multiplier"]

    return {
        "ranking": mode,
        "steps": steps,
        "time": {"age_hours": round(age_h, 3), **decay},
        "total": total,
        "topic_seed_bonus_applied": seed_bonus,
    }


# `rank_score` reads this from CFG at call time; the module-level default keeps
# the pure function usable without importing the engine.
CFG_FACTOR = 0.7


def assert_matches_rank_score(reconstructed: float, actual: float,
                             rel_tol: float = 1e-5) -> bool:
    """True when our itemised total equals the ranker's own return value.

    This is the guard on the module's central promise.

    The tolerance is relative and 1e-5, not an equality or a 1e-9 epsilon: the
    score is a product of four to six doubles, and the route's own last step
    (the age penalty) is computed from a fresh `time.time()` rather than the
    refresh timestamp, so a few ULP of drift is expected and harmless. 1e-5
    still catches any real logic divergence — a missing multiplier is off by
    orders of magnitude, not by the seventh decimal.
    """
    if actual is None:
        return False
    return abs(reconstructed - actual) <= max(abs(actual) * rel_tol, 1e-9)


def gate_report(post: Dict[str, Any], fcfg: Dict[str, Any],
                score: float) -> Dict[str, Any]:
    """Did this post clear each configured gate, and by how much?

    A post *near* the bar is the most interesting thing to explain, so this
    reports the margin on every gate, not just pass/fail. `saves` is never a
    gate: the number does not exist.
    """
    likes = _n(post, "likeCount")
    reposts = _n(post, "repostCount")
    share = (reposts / likes) if likes > 0 else 0.0
    out: Dict[str, Any] = {
        "min_likes": {"required": fcfg.get("min_likes"), "actual": likes,
                      "test": ">="},
        "min_score": {"required": fcfg.get("min_score"), "actual": round(score, 3),
                      "test": ">="},
        "min_reposts_share": {"required": fcfg.get("min_reposts_share"),
                              "actual": round(share, 4), "test": ">="},
        "max_likes": {"required": fcfg.get("max_likes"), "actual": likes,
                      "test": "<="},
    }
    for row in out.values():
        req = row["required"]
        try:
            active = req is not None and float(req) > 0
        except (TypeError, ValueError):
            active = False
        row["active"] = active
        if not active:
            row["pass"] = None
            continue
        if row["test"] == "<=":
            row["pass"] = row["actual"] <= float(req)
        else:
            row["pass"] = row["actual"] >= float(req)
    return out


def summarize(breakdown: Dict[str, Any], rank: Optional[Dict[str, Any]],
              personalized: bool, topic_terms: Optional[Sequence[str]] = None) -> str:
    """One human line assembled from the numbers above — never hardcoded.

    A test asserts several literals from the inputs appear in the output, so
    the sentence cannot drift away from the data it describes.
    """
    parts = breakdown["parts"]
    live = {k: v for k, v in parts.items() if v["n"]}
    top = max(live.items(), key=lambda kv: (kv[1]["points"], kv[0])) if live else None
    bits: List[str] = []
    if top:
        name, row = top
        bits.append(f"{row['n']} {name} (x{row['weight']} = {round(row['points'], 1)})")
    bits.append(f"base {round(breakdown['base'], 2)}")
    if rank:
        bits.append(f"{round(rank['total'], 2)} after "
                    f"{len(rank['steps']) - 1} modifier(s)")
        bits.append(f"{round(rank['time']['age_hours'], 1)}h old")
    if topic_terms:
        bits.append("topic: " + ", ".join(list(topic_terms)[:3]))
    if personalized:
        bits.append("personalized for you")
    else:
        bits.append("not personalized for you")
    return "; ".join(bits)


# ---------------------------------------------------------------------------
# taste vectors
# ---------------------------------------------------------------------------

def author_of_uri(uri: str) -> str:
    """The author DID encoded in an AT-URI: at://<did>/app.bsky.feed.post/<rkey>.

    A user_likes row stores only the post URI, but the author is inside it, so
    a per-account like profile needs no join against `posts` — which matters
    because most liked posts are never in the corpus at all.
    """
    if not uri or not uri.startswith("at://"):
        return ""
    rest = uri[len("at://"):]
    parts = rest.split("/")
    return parts[0] if parts and parts[0] else ""


def like_vector(pairs: Iterable[Sequence[Any]]) -> Dict[str, float]:
    """[{author_did, weight}, ...] -> {author_did: weight}."""
    out: Dict[str, float] = {}
    for row in pairs:
        did = row[0]
        if not did:
            continue
        try:
            out[did] = out.get(did, 0.0) + float(row[1] or 0)
        except (TypeError, ValueError):
            continue
    return out


def cosine_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity of two sparse count vectors. 0.0 when either is empty.

    Keys are author DIDs, values are like counts. Sparse by construction, so
    iterate the intersection rather than building dense vectors.
    """
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    num = sum(a[k] * b[k] for k in common)
    da = math.sqrt(sum(v * v for v in a.values()))
    db_ = math.sqrt(sum(v * v for v in b.values()))
    if da == 0.0 or db_ == 0.0:
        return 0.0
    return num / (da * db_)


def similar_people(target: Dict[str, float], corpus: Dict[str, Dict[str, float]],
                   *, blocked: Optional[set] = None, limit: int = 10
                   ) -> List[Dict[str, Any]]:
    """Corpus accounts most taste-similar to `target`, best first.

    `corpus` maps account_did -> {author_did: weight} — that is, every
    candidate's own like profile. Ties break on DID so the output is
    deterministic across calls (a test asserts stability).
    """
    blocked = blocked or set()
    out: List[Dict[str, Any]] = []
    for did, vec in corpus.items():
        if did in target or did in blocked:
            continue
        s = cosine_similarity(target, vec)
        if s > 0:
            out.append({"did": did, "score": round(s, 6),
                        "shared_authors": len(set(target) & set(vec))})
    out.sort(key=lambda r: (-r["score"], r["did"]))
    return out[:int(limit)]


def rank_candidates(cands: Sequence[Dict[str, Any]], weights: Dict[str, float],
                    *, limit: int = 20, exclude: Optional[Iterable[str]] = None,
                    blocked: Optional[set] = None,
                    recency_hours: Optional[float] = None
                    ) -> List[Dict[str, Any]]:
    """Order also-liked candidates the way this deployment's own ranker would.

    Same base score the ranker uses (log1p of weighted engagement), and the
    same age penalty `ranking_core.velocity` applies, so "also liked" is a
    recommendation from the feed's own taste rather than a second opinion.
    Deterministic: equal scores break on the post URI.
    """
    ex = set(exclude or ())
    bl = blocked or set()
    out = []
    for c in cands:
        uri = c.get("uri") or ""
        did = c.get("author_did") or author_of_uri(uri)
        if not uri or uri in ex or (did and did in bl):
            continue
        b = score_breakdown(c, weights)
        s = b["base"]
        if recency_hours is not None:
            s *= ranking_core.velocity(recency_hours, 0.75, 2.0)
        out.append({"post": uri, "author": c.get("author_handle") or did,
                    "author_did": did, "text": (c.get("text") or "")[:280],
                    "score": s, "likes": b["parts"]["likes"]["n"]})
    out.sort(key=lambda r: (-r["score"], r["post"]))
    return out[:int(limit)]
