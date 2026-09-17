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


def post(likes=10, did="did:plc:a", key="p", text="", hours=2.0):
    return {"uri": f"at://{did}/app.bsky.feed.post/{key}", "cid": "c",
            "author": {"did": did}, "record": {"text": text},
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
    fcfg = {"ranking": "ml", "topic_weight": 2.5, "topic_keyword_bonus": 0.35,
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
    """The owner's own posts are never excluded for the owner (or anyone):
    the feed keeps serving them at the configured owner ratio."""
    if not hasattr(fg, "user_excluded_uris"):
        print("ok   user_excluded_uris absent in this module (skipped)")
        return
    now = time.time()
    owner = fg.CFG["owner"]["did"]
    mine = "at://did:plc:ownerpost/app.bsky.feed.post/mine"
    db_insert_post(mine, owner)
    fg.db().execute("DELETE FROM user_likes WHERE requester_did=?", (owner,))
    fg.db().execute("DELETE FROM user_hidden WHERE requester_did=?", (owner,))
    fg.db().execute("INSERT INTO user_likes VALUES(?,?,?)", (owner, mine, now))
    fg.db().execute("INSERT INTO user_hidden VALUES(?,?,?,?)",
                    (owner, mine, "less", now))
    assert fg.user_excluded_uris(owner, True, owner) == set()
    unprotected = fg.user_excluded_uris(owner, True, None)
    assert mine in unprotected, unprotected
    print("ok   owner-authored posts are never excluded when protect_did is set")


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


if __name__ == "__main__":
    test_rank_score_velocity_decays_with_age()
    test_rank_score_taste_multiplier_on_any_mode()
    test_rank_score_hashtag_beats_plain()
    test_author_prior_multiplies()
    test_select_returns_scored_tuples()
    test_select_owner_ratio_cap()
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
    test_apply_user_exclusions_tail_vs_hard_hide()
    test_extract_terms_and_negative_penalty()
    test_less_event_hides_post_and_sinks_author()
    test_every_feed_hides_seen_posts()
    test_keyword_weights_learner_is_config_gated()
    print("ALL PASS")
