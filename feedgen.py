#!/usr/bin/env python3
"""ATProto (Bluesky) custom feed generator, stdlib only.

Serves many feeds built from the members of a curate list. Each feed is
configured by its own time window, engagement thresholds, ranking mode and
optional scoring override (see config.json).

Endpoints (hostname/port from the config):
  /.well-known/did.json
  /xrpc/app.bsky.feed.describeFeedGenerator
  /xrpc/app.bsky.feed.getFeedSkeleton?feed=<feed-uri>&limit=&cursor=
  /xrpc/app.bsky.feed.sendInteractions   (per-user "show less" / feedback)
  /  and  /health  (human status page for all feeds, with per-feed counts)

DESIGN NOTES
------------
Dedup is a *bounded suppression window*, not a permanent "seen" set. A post
that appears in a feed is suppressed in that feed for `seen_ttl_hours`
(default 24). After that it can return if it is still inside the feed's age
window. Rationale:

  - The original implementation marked a post seen at pick time, forever.
    Since the candidate pool is finite and posts age out of the windows
    anyway, every feed drained to 0-4 posts and stayed there.
  - The per-user "hide what I already saw or liked" is implemented on top of
    this: `user_likes` / `user_hidden` / `served_posts` + `user_excluded_uris`
    (see the per-user exclusion section further down). `seen` remains the
    global, cheap backstop. Every feed sets `hide_seen: 1`, and the
    `hide_min_board` floor (20) is what keeps a drained board from serving
    nothing — the failure this note describes.
  - Seen memory is shared by every feed of one deployment: a post served in
    any feed is not served again in another feed of the same service until
    `hide_seen_ttl_h` (24 h) expires. Deployments are independent because
    each one owns its database file.
  - `seen_ttl_hours: 0` disables dedup entirely (pure stable board).
  - `seen_ttl_hours` >= the feed's window approximates the old once-ever
    behaviour without the unbounded state growth.

The "repost share" rule is approximate: a post qualifies only if its repost
count is at least `min_reposts_share` of its like count (default 0.33).

Save/bookmark counts are structurally unavailable: Bluesky bookmarks are
private per user and no public save count exists in any AppView response.
The `w_saves` weight therefore always multiplies zero. It is kept in the
config so the intent is visible, and so the term starts working if Bluesky
ever exposes the number.

State lives in SQLite (feedgen.sqlite) so this service and any helper process
(an action bot, a list updater, a diagnostics script) share one store instead
of rewriting a JSON file on every pick. `feedgen-state.json` is migrated once
on first start and kept as a backup.
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from statistics import median
import base58
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature

import ranking_core

# --- DID-doc cache for JWT verification (10-min TTL, in-memory) ---
DID_DOC_CACHE_TTL = 600
_DID_DOC_CACHE = {}  # did -> (doc_dict, fetched_ts)

BASE = os.path.dirname(os.path.abspath(__file__))


def load_json(path):
    try:
        with open(os.path.join(BASE, path)) as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"config not found: {os.path.join(BASE, path)}\n"
            f"copy config.example.json to config.json and edit it "
            f"(or set FEEDGEN_CONFIG to another path)")


def load_dotenv(path):
    vals = {}
    try:
        with open(os.path.join(BASE, path)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return vals


CFG = load_json(os.environ.get("FEEDGEN_CONFIG") or "config.json")
ENV = load_dotenv(".env") | dict(os.environ)

HOSTNAME = CFG["hostname"]
SVC_TAG = CFG.get("svc_tag") or "feedgen"
SERVICE_DID = f"did:web:{HOSTNAME}"
PUBLISHER_DID = CFG["publisher_did"]
FEEDS = {
    f"at://{PUBLISHER_DID}/app.bsky.feed.generator/{f['rkey']}": f for f in CFG["feeds"]
}

DB_PATH = os.environ.get("FEEDGEN_DB") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
    "feedgen", "feedgen.sqlite")
LEGACY_STATE_PATH = os.environ.get("FEEDGEN_STATE_PATH") or os.path.join(
    BASE, "feedgen-state.json"
)

CACHE: dict = {
    "feeds": {}, "feed_scores": {}, "updated": 0, "scanned": 0, "members": 0,
    "error": None, "gen": 0, "stats": {}, "state_uris": 0, "taste_authors": 0,
    "topic_seeds": set(),
}
CACHE_LOCK = threading.Lock()
DB_LOCK = threading.Lock()

_db_con = None


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    uri           TEXT PRIMARY KEY,
    cid           TEXT,
    author_did    TEXT NOT NULL,
    author_handle TEXT,
    indexed_at    TEXT NOT NULL,
    like_count    INTEGER DEFAULT 0,
    repost_count  INTEGER DEFAULT 0,
    quote_count   INTEGER DEFAULT 0,
    reply_count   INTEGER DEFAULT 0,
    text          TEXT DEFAULT '',
    langs         TEXT DEFAULT '',
    record_json   TEXT NOT NULL,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_author  ON posts(author_did);
CREATE INDEX IF NOT EXISTS idx_posts_indexed ON posts(indexed_at);
CREATE TABLE IF NOT EXISTS engagement_snapshots (
    uri     TEXT NOT NULL,
    ts      REAL NOT NULL,
    likes    INTEGER NOT NULL DEFAULT 0,
    reposts  INTEGER NOT NULL DEFAULT 0,
    quotes   INTEGER NOT NULL DEFAULT 0,
    replies  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uri, ts)
);
CREATE INDEX IF NOT EXISTS idx_engsnap_ts ON engagement_snapshots(ts);

CREATE TABLE IF NOT EXISTS seen (
    rkey       TEXT NOT NULL,
    uri        TEXT NOT NULL,
    shown_at   REAL NOT NULL,
    shown_count INTEGER DEFAULT 1,
    PRIMARY KEY (rkey, uri)
);
CREATE INDEX IF NOT EXISTS idx_seen_shown ON seen(rkey, shown_at);

CREATE TABLE IF NOT EXISTS author_topic (
    author_did  TEXT PRIMARY KEY,
    topic_posts    INTEGER DEFAULT 0,
    total_posts INTEGER DEFAULT 0,
    affinity    REAL DEFAULT 0,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS taste (
    author_did  TEXT PRIMARY KEY,
    likes_count INTEGER DEFAULT 0,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS taste_ext (
    author_did  TEXT PRIMARY KEY,
    likes_count INTEGER DEFAULT 0,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    uri        TEXT NOT NULL,
    cid        TEXT,
    author_did TEXT,
    created_at REAL NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT,
    UNIQUE(kind, uri)
);
CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(kind, created_at);

CREATE TABLE IF NOT EXISTS promotions (
    did            TEXT PRIMARY KEY,
    handle         TEXT,
    promoter       TEXT,
    promotion_type TEXT,
    evidence_uri   TEXT,
    discovered_at  REAL,
    added_at       REAL,
    listitem_rkey  TEXT,
    status         TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS follows (
    actor_did   TEXT NOT NULL,
    subject_did TEXT NOT NULL,
    handle      TEXT,
    direction   TEXT NOT NULL,
    seen_at     REAL NOT NULL,
    PRIMARY KEY (actor_did, subject_did, direction)
);

CREATE TABLE IF NOT EXISTS served_posts (
    requester_did TEXT NOT NULL,
    post_uri      TEXT NOT NULL,
    feed_rkey     TEXT NOT NULL,
    served_at     REAL NOT NULL,
    polled_at     REAL DEFAULT 0,
    PRIMARY KEY (requester_did, post_uri)
);
CREATE INDEX IF NOT EXISTS idx_served_requester ON served_posts(requester_did, served_at);
-- every feed now opts into hide_seen, so this query runs on every request
CREATE INDEX IF NOT EXISTS idx_served_user_feed ON served_posts(requester_did, feed_rkey);

CREATE TABLE IF NOT EXISTS user_affinity (
    requester_did TEXT NOT NULL,
    author_did    TEXT NOT NULL,
    score         REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (requester_did, author_did)
);
CREATE INDEX IF NOT EXISTS idx_affinity_requester ON user_affinity(requester_did, score);

CREATE TABLE IF NOT EXISTS interaction_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_uri      TEXT NOT NULL,
    feed_rkey     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    requester_did TEXT,
    processed     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_interaction_post ON interaction_events(post_uri, kind);

-- Per-user exclusion state. Written by the like poller /
-- requester-like refresher (user_likes), the sendInteractions handler and
-- process_interactions (user_hidden), and the not-interested folder
-- (user_negative_terms). Read on the serve path only — no network I/O.
CREATE TABLE IF NOT EXISTS user_likes (
    requester_did TEXT NOT NULL,
    post_uri      TEXT NOT NULL,
    fetched_at    REAL NOT NULL,
    PRIMARY KEY (requester_did, post_uri)
);
CREATE INDEX IF NOT EXISTS idx_user_likes_req ON user_likes(requester_did, fetched_at);

CREATE TABLE IF NOT EXISTS user_hidden (
    requester_did TEXT NOT NULL,
    post_uri      TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT 'less',
    created_at    REAL NOT NULL,
    PRIMARY KEY (requester_did, post_uri)
);
CREATE INDEX IF NOT EXISTS idx_user_hidden_req ON user_hidden(requester_did, created_at);

CREATE TABLE IF NOT EXISTS user_negative_terms (
    requester_did TEXT NOT NULL,
    term          TEXT NOT NULL,
    hits          INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (requester_did, term)
);

CREATE TABLE IF NOT EXISTS keyword_weights (
    keyword     TEXT PRIMARY KEY,
    weight      REAL NOT NULL DEFAULT 1.0,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_seen   REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE IF NOT EXISTS bot_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    started         REAL NOT NULL,
    finished        REAL,
    candidates      INTEGER DEFAULT 0,
    acted           INTEGER DEFAULT 0,
    dry_run         INTEGER DEFAULT 1,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS metrics_log (
    ts REAL NOT NULL, feed_rkey TEXT NOT NULL, posts INTEGER,
    unique_authors INTEGER, median_age_hours REAL,
    score_median REAL, score_p90 REAL,
    PRIMARY KEY (ts, feed_rkey))
"""


def db():
    global _db_con
    if _db_con is None:
        con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(SCHEMA)
        _db_con = con
    return _db_con


def migrate_schema():
    """Idempotent column migrations for DBs created by older versions."""
    with DB_LOCK:
        ev = {r[1] for r in db().execute(
            "PRAGMA table_info(interaction_events)").fetchall()}
        if "requester_did" not in ev:
            db().execute("ALTER TABLE interaction_events ADD COLUMN requester_did TEXT")
        if "processed" not in ev:
            db().execute("ALTER TABLE interaction_events ADD COLUMN processed INTEGER DEFAULT 0")
        sp = {r[1] for r in db().execute(
            "PRAGMA table_info(served_posts)").fetchall()}
        if "polled_at" not in sp:
            db().execute("ALTER TABLE served_posts ADD COLUMN polled_at REAL DEFAULT 0")


def meta_get(k, default=None):
    try:
        row = db().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default
    except sqlite3.Error:
        return default


def meta_set(k, v):
    db().execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )


def migrate_legacy_state():
    """Import the old JSON seen-set once: {rkey: [uri, ...]} -> seen table.

    Imported entries get shown_at = now, so they stay suppressed for
    `seen_ttl_hours` and then are free to return (instead of being permanent).
    """
    if meta_get("legacy_state_migrated") == "1":
        return 0
    n = 0
    try:
        with open(LEGACY_STATE_PATH) as fh:
            snap = json.load(fh)
        now = time.time()
        with DB_LOCK:
            for rkey, uris in snap.items():
                if not isinstance(uris, list):
                    continue
                rows = [(rkey, u, now, 1) for u in uris if isinstance(u, str)]
                db().executemany(
                    "INSERT OR IGNORE INTO seen(rkey,uri,shown_at,shown_count) "
                    "VALUES(?,?,?,?)", rows)
                n += len(rows)
        if n and os.path.exists(LEGACY_STATE_PATH):
            backup = LEGACY_STATE_PATH + ".migrated"
            if not os.path.exists(backup):
                os.replace(LEGACY_STATE_PATH, backup)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        if not isinstance(e, FileNotFoundError):
            print(f"[{SVC_TAG}] legacy state migration skipped: {e}", flush=True)
    meta_set("legacy_state_migrated", "1")
    print(f"[{SVC_TAG}] migrated {n} legacy seen entries into sqlite", flush=True)
    return n


def prune_state(now_ts=None):
    """Drop suppression rows that are long past any feed's TTL."""
    now_ts = now_ts or time.time()
    longest = max([f.get("seen_ttl_hours", 24) for f in CFG["feeds"]] + [24])
    cutoff = now_ts - (longest * 3600 + 3600)
    with DB_LOCK:
        cur = db().execute("DELETE FROM seen WHERE shown_at < ?", (cutoff,))
        db().execute("DELETE FROM posts WHERE last_seen < ?", (now_ts - 90 * 86400,))
        db().execute("DELETE FROM served_posts WHERE served_at < ?", (now_ts - 30 * 86400,))
        db().execute("DELETE FROM interaction_events WHERE created_at < ?",
                     (now_ts - 30 * 86400,))
        db().execute("DELETE FROM user_affinity WHERE updated_at < ?",
                     (now_ts - 60 * 86400,))
        # Per-user exclusion memory. user_likes is a cache of public likes
        # (cheap to re-fetch); user_hidden / user_negative_terms carry the
        # user's explicit intent, so they live a year. A post old enough to
        # have aged out of every feed's window long ago is what gets dropped.
        db().execute("DELETE FROM user_likes WHERE fetched_at < ?",
                     (now_ts - 90 * 86400,))
        db().execute("DELETE FROM user_hidden WHERE created_at < ?",
                     (now_ts - 365 * 86400,))
        db().execute("DELETE FROM user_negative_terms WHERE updated_at < ?",
                     (now_ts - 365 * 86400,))
    return cur.rowcount


# --------------------------------------------------------------------------
# atproto
# --------------------------------------------------------------------------

def rpc(host, method, data=None, token=None, timeout=30):
    req = urllib.request.Request(
        f"{host}/xrpc/{method}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "feedgen/1.0"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} HTTP {e.code}: {e.read().decode()[:200]}")


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def weights_for(fcfg):
    """Global scoring block, with an optional per-feed override."""
    w = dict(CFG["scoring"])
    w.update(fcfg.get("scoring") or {})
    return w


def base_score(p, w):
    """Engagement score. `saves` is structurally 0 — see module docstring."""
    return (
        p.get("likeCount", 0) * w.get("w_likes", 1.0)
        + p.get("repostCount", 0) * w.get("w_reposts", 3.0)
        + p.get("quoteCount", 0) * w.get("w_quotes", 2.0)
        + p.get("replyCount", 0) * w.get("w_replies", 0.5)
        + 0 * w.get("w_saves", 0.0)
    )


def topic_keyword_hits(text, keywords):
    if not text:
        return 0
    low = text.lower()
    return sum(1 for k in keywords if k in low)


def rank_score(p, fcfg, w, age_h, ctx):
    """Apply the feed's ranking mode + affinity modifiers to a base score.

    This is the GLOBAL baseline only — per-user personalization happens at
    serve time (personalize_page). Shape for every mode:

        log_engagement * author_prior * taste/keyword multipliers * velocity
    """
    s = ranking_core.log_engagement(p, w)
    did = (p.get("author") or {}).get("did") or "?"
    mode = fcfg.get("ranking", "flat")
    topicw = fcfg.get("topic_weight", 0.0)
    taw = fcfg.get("taste_weight", 0.0)

    # Owner taste + per-author keyword affinity. Any feed can set
    # taste_weight/topic_weight: the Top feeds carry a small taste_weight so
    # the curator's likes gently shape every feed ("tension"), foryou/topic
    # carry more.
    if topicw or taw:
        aff = ctx["topic_affinity"].get(did, 0.0)
        ta = ctx["taste"].get(did, 0.0)
        if did in ctx["topic_seeds"]:
            aff = 1.0
        if topicw:
            s *= 1.0 + topicw * aff
        if taw:
            s *= 1.0 + taw * ta
        if mode == "topic" and did in ctx["topic_seeds"]:
            s += fcfg.get("topic_seed_bonus", 0.0)
        text = (p.get("record") or {}).get("text", "") if isinstance(
            p.get("record"), dict) else ""
        hits, _ = ranking_core.keyword_score(
            text, ctx["topic_keywords"], CFG.get("topic_keyword_weight_factor", 0.7))
        pa = ranking_core.post_keyword_affinity(hits, fcfg.get("keyword_saturate", 4.0))
        if pa > 0:
            s *= 1.0 + pa * fcfg.get("topic_keyword_bonus", 0.0)

    prior = (ctx.get("author_priors") or {}).get(did)
    if prior:
        s *= prior

    # Time: HN-style velocity with a half-life floor. `velocity` is the new
    # mode for the Top-N windows; trending keeps its own gravity; foryou/topic
    # fall back to their old decay_factor as gravity.
    if mode == "trending":
        s *= ranking_core.velocity(age_h, fcfg.get("decay_factor", 0.8))
    elif mode in ("velocity", "deep", "foryou", "topic"):
        s *= ranking_core.velocity(
            age_h, fcfg.get("gravity", fcfg.get("decay_factor", 0.6)),
            fcfg.get("half_life_hours", 2.0))
    return s


def select(posts, fcfg, now, owner, ctx, suppressed):
    """Rank the posts for one feed. Returns (uris, stats).

    `suppressed` is the set of URIs currently inside this feed's suppression
    window (written back by the caller when the run finishes).
    """
    w = weights_for(fcfg)
    rkey = fcfg["rkey"]
    ttl = fcfg.get("seen_ttl_hours", 24)
    owner_did = (owner or {}).get("did")
    gate_mode = fcfg.get("gate_mode", "all")
    stats = {"in_window": 0, "pass_share": 0, "pass_gates": 0,
             "suppressed": 0, "final": 0, "owner": 0,
             "vintage_cands": 0, "vintage_picked": 0}
    ranking_mode = fcfg.get("ranking", "flat")
    vfrac = fcfg.get("vintage_slot_fraction", 0.0) or 0.0
    v_min = fcfg.get("vintage_min_age_hours", 6)
    v_max = fcfg.get("vintage_max_age_hours", 0) or 0
    win_h = fcfg.get("window_hours", 6)
    hl_h = fcfg.get("half_life_hours", 72)
    vintage_on = bool(vfrac and v_max)
    track = {}
    now_ts = now.timestamp()
    cands = []

    for p in posts:
        ts = p.get("indexedAt") or (p.get("record") or {}).get("createdAt")
        if not ts:
            continue
        try:
            age_h = (now - parse_time(ts)).total_seconds() / 3600
        except (ValueError, TypeError):
            continue
        if not (fcfg.get("min_age_hours", 0) <= age_h <= fcfg["max_age_hours"]):
            continue
        if vintage_on and ranking_mode == "trending" and age_h > v_max:
            continue
        stats["in_window"] += 1

        did = (p.get("author") or {}).get("did")
        if did in CFG.get("blocked_dids", []):
            continue
        is_owner = bool(owner.get("always_include") and owner_did and did == owner_did)
        likes = p.get("likeCount", 0)
        reposts = p.get("repostCount", 0)

        # --- owner posts bypass every gate, including suppression ---
        if not is_owner:
            # Adaptive gates: automatically adjust thresholds to maintain
            # target number of non-owner posts (adaptive_target)
            target = fcfg.get("adaptive_target", int(fcfg.get("max_posts", 100) * 0.75))
            max_likes_gate = fcfg.get("min_likes", 0) or 0
            max_share_gate = fcfg.get("min_reposts_share", 0) or 0
            non_owner_so_far = sum(1 for _, _, d in cands if d != owner_did)
            
            # Calculate adaptive thresholds based on how many we've collected
            if non_owner_so_far < target * 0.5:
                min_likes = 0
                min_share = 0.0
            elif non_owner_so_far < target * 0.75:
                min_likes = min(max_likes_gate, 1) if max_likes_gate > 0 else 0
                min_share = min(max_share_gate, 0.05) if max_share_gate > 0 else 0.0
            else:
                min_likes = max_likes_gate
                min_share = max_share_gate
            
            if min_share and (likes <= 0 or reposts < likes * min_share):
                continue
            stats["pass_share"] += 1
            uri = p.get("uri")
            if vintage_on and ranking_mode == "trending" and age_h >= v_min:
                if not uri:
                    continue
                e_now = (likes * w.get("w_likes", 1.0)
                         + reposts * w.get("w_reposts", 3.0)
                         + p.get("quoteCount", 0) * w.get("w_quotes", 2.0)
                         + p.get("replyCount", 0) * w.get("w_replies", 0.5))
                then = engagement_then(uri, now_ts - win_h * 3600, w)
                if then is None:
                    continue          # no history anchor -> cannot prove recency
                e_then, t_then = then
                s = ranking_core.recent_rate_score(
                    e_now - e_then, (now_ts - t_then) / 3600.0, age_h, hl_h)
                if s <= 0:
                    continue          # stagnation scores nothing
                stats["vintage_cands"] += 1
                track[uri] = "vintage"
            else:
                s = rank_score(p, fcfg, w, age_h, ctx)
                if uri:
                    track[uri] = "fresh"
            gates = []
            if (fcfg.get("min_score") or 0) > 0:
                gates.append(s >= fcfg["min_score"])
            if min_likes > 0:
                gates.append(likes >= min_likes)
            if gates:
                ok = all(gates) if gate_mode == "all" else any(gates)
                if not ok:
                    continue
            stats["pass_gates"] += 1
            uri = p.get("uri")
            if not uri:
                continue
            if ttl and uri in suppressed:
                stats["suppressed"] += 1
                continue
        else:
            s = rank_score(p, fcfg, w, age_h, ctx)
            s = s * owner.get("boost", 1.0) + owner.get("bonus", 0.0)
            stats["owner"] += 1
            stats["pass_share"] += 1
            stats["pass_gates"] += 1
            uri = p.get("uri")
            if not uri:
                continue

        cands.append((s, uri, did))

    # Deterministic order: score desc, then URI. Same inputs -> same board,
    # so a refresh does not reshuffle a subscriber's feed.
    cands.sort(key=lambda t: (-t[0], t[1]))

    cap = fcfg.get("max_per_author", 0) or 0
    max_ratio = owner.get("max_ratio", 0) or 0

    non_owner_cands = [(s, uri, did) for s, uri, did in cands if did != owner_did]
    owner_cands_list = [(s, uri, did) for s, uri, did in cands if did == owner_did]

    # First pass: non-owner posts, respecting the per-author cap
    out, per_author = [], {}
    for s, uri, did in non_owner_cands:
        if len(out) >= fcfg.get("max_posts", 100):
            break
        if cap and per_author.get(did, 0) >= cap:
            continue
        per_author[did] = per_author.get(did, 0) + 1
        out.append((uri, s))

    # Second pass: interleave owner posts at regular intervals, but never
    # past the max_ratio cap. (The old code dumped the surplus at the tail,
    # pushing the owner share far past the configured ratio.)
    if 0 < max_ratio < 1 and owner_cands_list:
        interval = max(1, int(1 / max_ratio))
        max_owner = int(len(out) * max_ratio)
        oi = 0
        result = []
        for i, (u, s) in enumerate(out):
            if (i + 1) % interval == 0 and oi < min(len(owner_cands_list), max_owner):
                result.append((owner_cands_list[oi][1], owner_cands_list[oi][0]))
                oi += 1
            result.append((u, s))
        out = result

    # Fallback: a feed with no non-owner candidates still shows owner posts
    if not out:
        out = [(u, s) for _s, u, _d in owner_cands_list][:fcfg.get("max_posts", 100)]

    if vintage_on and ranking_mode == "trending":
        fresh_out = [(u, s) for u, s in out if track.get(u) != "vintage"]
        vin_out = [(u, s) for u, s in out if track.get(u) == "vintage"]
        out = ranking_core.interleave_tracks(fresh_out, vin_out, vfrac)
        stats["vintage_picked"] = len(vin_out)

    stats["final"] = len(out)
    return out, stats


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

OWNER_POST_PAGES = 20


def fetch_all(token):
    """One full read pass: list feed + owner posts. Returns (posts, stats)."""
    src = CFG["source"]
    appview = src["appview_host"]
    owner_did = CFG.get("owner", {}).get("did")
    now = datetime.now(timezone.utc)
    max_window_h = max(f.get("max_age_hours", 48) for f in CFG["feeds"])
    cutoff_ts = (now - timedelta(hours=max_window_h)).isoformat()

    list_items = 0
    pages = 0
    owner_posts = 0
    by_uri = {}

    cursor = None
    for page in range(200):
        q = urllib.parse.urlencode(
            {"list": src["list_uri"], "limit": 100}
            | ({"cursor": cursor} if cursor else {}))
        if page:
            time.sleep(0.25)          # keep the page scan from looking like a burst
        d = None
        for attempt in range(3):
            try:
                d = rpc(appview, f"app.bsky.feed.getListFeed?{q}", token=token, timeout=45)
                break
            except Exception as e:
                wait = 2 * (3 ** attempt)          # 2s, 6s, 18s
                print(f"[{SVC_TAG}] getListFeed page {page} attempt {attempt + 1} "
                      f"failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
                if attempt < 2:
                    time.sleep(wait)
        if d is None:
            print(f"[{SVC_TAG}] getListFeed page {page} giving up; using "
                  f"{list_items} items collected so far", flush=True)
            break
        items = d.get("feed", [])
        if not items:
            break
        list_items += len(items)
        pages += 1
        for item in items:
            if "reason" in item and item["reason"] != "DIRECT":
                continue
            p = item.get("post")
            if not p:
                continue
            ts = p.get("indexedAt") or (p.get("record") or {}).get("createdAt")
            if ts and ts >= cutoff_ts:
                by_uri[p["uri"]] = p
        cursor = d.get("cursor")
        if not cursor:
            break

    if owner_did:
        cursor = None
        for _ in range(OWNER_POST_PAGES):
            q = urllib.parse.urlencode(
                {"actor": owner_did, "limit": 100}
                | ({"cursor": cursor} if cursor else {}))
            try:
                d = rpc(appview, f"app.bsky.feed.getAuthorFeed?{q}", token=token, timeout=45)
            except RuntimeError:
                break
            items = d.get("feed", [])
            if not items:
                break
            for i in items:
                if src.get("include_reposts") or "reason" not in i:
                    p = i.get("post")
                    if not p:
                        continue
                    ts = p.get("indexedAt") or (p.get("record") or {}).get("createdAt")
                    if ts and ts >= cutoff_ts and p["uri"] not in by_uri:
                        by_uri[p["uri"]] = p
                        owner_posts += 1
            cursor = d.get("cursor")
            if not cursor:
                break

    return list(by_uri.values()), {
        "list_items": list_items, "pages": pages, "owner_posts": owner_posts,
    }


def record_posts(posts):
    now = time.time()
    rows = []
    for p in posts:
        rec = p.get("record") if isinstance(p.get("record"), dict) else {}
        langs = rec.get("langs") or []
        rows.append((
            p.get("uri"), p.get("cid"), (p.get("author") or {}).get("did", "?"),
            (p.get("author") or {}).get("handle", ""),
            p.get("indexedAt") or rec.get("createdAt") or "",
            p.get("likeCount", 0), p.get("repostCount", 0),
            p.get("quoteCount", 0), p.get("replyCount", 0),
            (rec.get("text") or "")[:2000], ",".join(langs) if isinstance(langs, list) else "",
            json.dumps(p), now, now,
        ))
    with DB_LOCK:
        db().executemany(
            """INSERT INTO posts(uri,cid,author_did,author_handle,indexed_at,
                   like_count,repost_count,quote_count,reply_count,text,langs,
                   record_json,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uri) DO UPDATE SET
                 cid=excluded.cid, like_count=excluded.like_count,
                 repost_count=excluded.repost_count, quote_count=excluded.quote_count,
                 reply_count=excluded.reply_count, text=excluded.text,
                 langs=excluded.langs, record_json=excluded.record_json,
                 last_seen=excluded.last_seen""", rows)
    return len(rows)


def snapshot_engagements(posts, now=None):
    """Hourly engagement snapshots feeding the rediscovery track.

    One row per post per snapshot_interval_hours with the raw per-kind
    counts; weights are applied at read time so config changes never
    corrupt history. Prunes rows past snapshot_prune_hours every pass.
    Disabled (writes nothing) unless snapshot_interval_hours is set.
    """
    interval = CFG.get("snapshot_interval_hours", 0)
    if not interval or not posts:
        return 0
    now = time.time() if now is None else now
    cutoff = now - CFG.get("snapshot_prune_hours", 192) * 3600
    written = 0
    with DB_LOCK:
        con = db()
        con.execute("DELETE FROM engagement_snapshots WHERE ts < ?", (cutoff,))
        for p in posts:
            uri = p.get("uri")
            if not uri:
                continue
            row = con.execute(
                "SELECT ts FROM engagement_snapshots WHERE uri=? "
                "ORDER BY ts DESC LIMIT 1", (uri,)).fetchone()
            if row and now - row[0] < interval * 3600:
                continue
            con.execute(
                "INSERT OR REPLACE INTO engagement_snapshots"
                "(uri, ts, likes, reposts, quotes, replies) VALUES(?,?,?,?,?,?)",
                (uri, now, p.get("likeCount", 0), p.get("repostCount", 0),
                 p.get("quoteCount", 0), p.get("replyCount", 0)))
            written += 1
    return written


def engagement_then(uri, cutoff_ts, w):
    """(weighted_engagement, ts) at the newest snapshot at or before cutoff_ts."""
    with DB_LOCK:
        row = db().execute(
            "SELECT ts, likes, reposts, quotes, replies FROM engagement_snapshots "
            "WHERE uri=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (uri, cutoff_ts)).fetchone()
    if not row:
        return None
    return (row[1] * w.get("w_likes", 1.0) + row[2] * w.get("w_reposts", 3.0)
            + row[3] * w.get("w_quotes", 2.0) + row[4] * w.get("w_replies", 0.5),
            row[0])


def build_author_priors(min_posts=3):
    """(priors, list_median): per-author engagement-rate multiplier.

    priors[did] = author_prior(author_median_engagement, n_posts,
    list_median) over the stored 90-day corpus. Authors who consistently
    over-perform the list median rise a little, quiet accounts sink a
    little; computed once per refresh and passed via ctx["author_priors"].
    """
    rows = db().execute(
        "SELECT author_did, "
        "like_count + 3*repost_count + 2*quote_count + 0.5*reply_count "
        "FROM posts WHERE last_seen > ?",
        (time.time() - 90 * 86400,)).fetchall()
    per = {}
    for did, eng in rows:
        per.setdefault(did, []).append(eng or 0.0)
    medians = {d: median(v) for d, v in per.items() if len(v) >= min_posts}
    list_median = median(list(medians.values())) if medians else 0.0
    priors = {d: ranking_core.author_prior(m, len(per[d]), list_median)
              for d, m in medians.items()}
    return priors, list_median


def build_topic_affinity():
    """Per-author topic affinity from the corpus we already stored.

    affinity = share of the author's stored posts that hit a topic keyword.
    Config `topic_seeds` (handles or DIDs) are pinned to 1.0.
    """
    keywords = [k.lower() for k in CFG.get("topic_keywords", [])]
    aff = {}
    if keywords:
        rows = db().execute(
            "SELECT author_did, text FROM posts WHERE last_seen > ?",
            (time.time() - 90 * 86400,)).fetchall()
        tot, hits = {}, {}
        for did, text in rows:
            tot[did] = tot.get(did, 0) + 1
            low = (text or "").lower()
            if any(k in low for k in keywords):
                hits[did] = hits.get(did, 0) + 1
        for did, n in tot.items():
            aff[did] = round(hits.get(did, 0) / n, 4) if n else 0.0
    seeds = set()
    # resolve seed handles -> dids (public, cheap)
    for s in CFG.get("topic_seeds", []) or []:
        if s.startswith("did:"):
            seeds.add(s)
            continue
        try:
            q = urllib.parse.urlencode({"handle": s})
            r = rpc(CFG["source"]["appview_host"],
                    f"com.atproto.identity.resolveHandle?{q}")
            if r.get("did"):
                seeds.add(r["did"])
        except Exception:
            continue
    for did in seeds:
        aff[did] = max(aff.get(did, 0.0), 1.0)
    return aff, seeds


# --- adaptive keyword weights (optional, config-gated) ----------------------
# A deployment can let the engine learn how strongly each topic term is
# actually used in the content its curator engages with. Config terms are
# seeded from `topic_keywords`; every term carries a weight whose EMA has its
# fixed point at 1.0 — decay pulls a term below it over time, and an engaged
# post pulls it back toward 1.0 — so a term the community stops using fades
# out (down to a per-source floor) and a term it keeps using stays at 1.0.
# Terms already known can enter the table as source='auto' when the config did
# not list them, but nothing here invents brand-new terms: discovery is
# limited to the config plus whatever is already in the table. All of it lives
# in `keyword_weights`; ranking does NOT read these weights (the topic tilt is
# computed per post in ranking_core.post_keyword_affinity) — they are a
# reporting/analysis signal.
#
# Everything is off unless the config opts in:
#   "keyword_weights": {"enabled": true, "half_life_days": 30, ...}
# A deployment that leaves it out keeps its hands off the table entirely.

KW_DEFAULTS = {"enabled": False, "half_life_days": 30.0, "decay_secs": 3600,
               "config_floor": 0.5, "auto_floor": 0.1, "hit_ema": 0.3,
               "max_weight": 2.0, "seed_weight": 1.0, "auto_start": 0.5,
               "min_weight": 0.2}


def kw_cfg():
    """The keyword-weight knobs, defaults filled in."""
    merged = dict(KW_DEFAULTS)
    merged.update(CFG.get("keyword_weights") or {})
    return merged


def kw_enabled():
    return bool(kw_cfg()["enabled"])


def kw_all(con=None):
    """Known topic terms: the config's plus learned ones above the weight floor."""
    kws = {k.lower() for k in (CFG.get("topic_keywords") or [])}
    if con is not None:
        kws.update(r[0] for r in con.execute(
            "SELECT keyword FROM keyword_weights WHERE weight > ?",
            (kw_cfg()["min_weight"],)))
    return list(kws)


def kw_extract(text, known):
    """Which known terms the text uses. '#term' counts as 'term'."""
    if not text:
        return []
    low = re.sub(r"#(\w+)", r"\1", text.lower())
    return [kw for kw in known if kw in low]


def kw_init():
    """Seed `keyword_weights` from the config. Idempotent; returns new rows."""
    if not kw_enabled():
        return 0
    n, now = 0, time.time()
    for kw in (CFG.get("topic_keywords") or []):
        cur = db().execute(
            "INSERT OR IGNORE INTO keyword_weights"
            "(keyword, weight, hits, last_seen, source) VALUES (?,?,0,?,?)",
            (kw.lower(), kw_cfg()["seed_weight"], now, "config"))
        n += cur.rowcount
    return n


def kw_update(con, text, now):
    """Fold one engaged post into the weights. Returns how many terms matched."""
    if not kw_enabled() or not text:
        return 0
    c = kw_cfg()
    matched = kw_extract(text, kw_all(con))
    for kw in matched:
        row = con.execute(
            "SELECT weight FROM keyword_weights WHERE keyword=?", (kw,)).fetchone()
        if row:
            new = min(c["max_weight"], row[0] * (1.0 - c["hit_ema"]) + c["hit_ema"])
            con.execute("UPDATE keyword_weights SET weight=?, hits=hits+1, "
                        "last_seen=? WHERE keyword=?", (new, now, kw))
        else:
            con.execute("INSERT OR IGNORE INTO keyword_weights"
                        "(keyword, weight, hits, last_seen, source) VALUES (?,?,1,?,?)",
                        (kw, c["auto_start"], now, "auto"))
    return len(matched)


def kw_decay(con, now):
    """Age every weight by the half-life; config terms keep a higher floor."""
    if not kw_enabled():
        return 0
    c = kw_cfg()
    n = 0
    for kw, weight, last_seen, source in con.execute(
            "SELECT keyword, weight, last_seen, source FROM keyword_weights").fetchall():
        age_days = max(0.0, (now - (last_seen or now)) / 86400.0)
        floor = c["config_floor"] if source == "config" else c["auto_floor"]
        con.execute("UPDATE keyword_weights SET weight=? WHERE keyword=?",
                    (max(floor, weight * 0.5 ** (age_days / c["half_life_days"])), kw))
        n += 1
    return n


def resolve_pds_host(did):
    """PDS endpoint for a DID, from its DID document.

    Cached in `meta` for 24h. Falls back to the configured source PDS when
    the document has no atproto_pds service or cannot be fetched — the same
    degradation the taste_sources path already has.
    """
    fallback = CFG["source"]["pds_host"]
    key = f"pds_host:{did}"
    cached = meta_get(key)
    if cached:
        ts, _, host = cached.partition("|")
        try:
            if (time.time() - float(ts)) < 86400 and host:
                return host
        except ValueError:
            pass
    try:
        doc = _fetch_did_doc(did)
        for svc in (doc or {}).get("service", []):
            if str(svc.get("id", "")).endswith("#atproto_pds") and svc.get("serviceEndpoint"):
                host = str(svc["serviceEndpoint"]).rstrip("/")
                meta_set(key, f"{time.time()}|{host}")
                return host
    except Exception as e:
        print(f"[{SVC_TAG}] pds resolve failed for {str(did)[:24]}: {e}", flush=True)
    return fallback


def build_taste(token):
    """Owner's own likes -> affinity toward the authors they actually like.

    This is the "learns from your tastes" half of the ML feeds. It reads only
    the owner's own public likes. NOTE: `app.bsky.feed.getActorLikes` is served
    by the PDS (bsky.social) with auth; public.api.bsky.app answers
    "Profile not found" for it.
    """
    owner_did = CFG.get("owner", {}).get("did")
    if not owner_did:
        return {}
    # Taste changes slowly; rebuilding it every refresh costs ~20 pages of API.
    ttl_h = float(CFG.get("taste_refresh_hours", 6))
    cached_at = meta_get("taste_built_at")
    rows = db().execute("SELECT author_did, likes_count FROM taste").fetchall()
    if rows and cached_at and (time.time() - float(cached_at)) < ttl_h * 3600:
        top = max(r[1] for r in rows) or 1
        return {r[0]: round(r[1] / top, 4) for r in rows}
    counts, cursor, pages = {}, None, 0
    host = CFG["source"]["pds_host"]
    try:
        while pages < 10:
            q = urllib.parse.urlencode(
                {"actor": owner_did, "limit": 100}
                | ({"cursor": cursor} if cursor else {}))
            d = rpc(host, f"app.bsky.feed.getActorLikes?{q}", token=token, timeout=45)
            items = d.get("feed", [])
            if not items:
                break
            for it in items:
                did = ((it.get("post") or {}).get("author") or {}).get("did")
                if did:
                    counts[did] = counts.get(did, 0) + 1
                    text = ((it.get("post") or {}).get("record")
                            or {}).get("text", "")
                    if text:
                        kw_update(db(), text, time.time())
            cursor = d.get("cursor")
            pages += 1
            if not cursor:
                break
    except Exception as e:
        print(f"[{SVC_TAG}] taste signal unavailable: {e}", flush=True)
        return {}
    if not counts:
        return {}
    top = max(counts.values())
    taste = {did: round(n / top, 4) for did, n in counts.items()}
    now = time.time()
    with DB_LOCK:
        db().execute("DELETE FROM taste")
        db().executemany(
            "INSERT OR REPLACE INTO taste(author_did,likes_count,updated_at) VALUES(?,?,?)",
            [(did, n, now) for did, n in counts.items()])
    meta_set("taste_built_at", now)
    return taste


def fetch_requester_likes(repo_did, max_pages=3):
    """Public like records for any repo via the account-free
    com.atproto.repo.listRecords endpoint.

    Returns (uris, counts, ok):
      * uris   — set of liked post AT-URIs; drives the per-user "already
                 liked" exclusion (a post you liked is never served again)
      * counts — {author_did: like_count}, the author DID being part 3 of
                 that URI (`at://did:plc:.../app.bsky.feed.post/...`), so no
                 AppView resolution is needed
      * ok     — True if at least one page came back. Callers must NOT treat
                 a failed fetch as "this account has no likes"

    Reads the requester's OWN PDS when it resolves (resolve_pds_host); most
    accounts still answer on bsky.social. Works for accounts we hold no
    credentials for — the whole point of taste_sources.
    """
    uris, counts, cursor, pages, ok = set(), {}, None, 0, False
    host = resolve_pds_host(repo_did) if repo_did else CFG["source"]["pds_host"]
    while pages < max_pages:
        params = {"repo": repo_did, "collection": "app.bsky.feed.like",
                  "limit": 100}
        if cursor:
            params["cursor"] = cursor
        q = urllib.parse.urlencode(params)
        try:
            d = rpc(host, f"com.atproto.repo.listRecords?{q}", timeout=45)
        except Exception as e:
            print(f"[{SVC_TAG}] listRecords failed for {str(repo_did)[:24]}: "
                  f"{e}", flush=True)
            break
        ok = True
        for rec in d.get("records", []):
            subject = ((rec.get("value") or {}).get("subject") or {}).get("uri") or ""
            parts = subject.split("/")
            if len(parts) >= 4 and parts[0] == "at:":
                uris.add(subject)
                did = parts[2]
                counts[did] = counts.get(did, 0) + 1
        cursor = d.get("cursor")
        pages += 1
        if not cursor:
            break
        time.sleep(0.1)
    return uris, counts, ok


def fetch_liked_authors(repo_did, max_pages=20):
    """Author like-counts for taste/taste_sources — thin wrapper over
    fetch_requester_likes so both consumers share exactly one fetch."""
    return fetch_requester_likes(repo_did, max_pages)[1]


def refresh_all_requester_likes(budget=None):
    """Keep `user_likes` warm for requesters we have recently served.

    Runs in the background refresh thread (never on the request path), most
    recently served first, at most once per `tension.likes_refresh_secs` per
    requester, and at most `tension.likes_refresh_budget` requesters per
    cycle. A failed fetch leaves the previous snapshot in place and is
    retried next cycle (the meta flag is only set on success).
    """
    tcfg = CFG.get("tension") or {}
    ttl = float(tcfg.get("likes_refresh_secs", 900))
    budget = int(budget or tcfg.get("likes_refresh_budget", 10))
    rows = db().execute(
        "SELECT requester_did, MAX(served_at) AS last FROM served_posts "
        "WHERE served_at > ? GROUP BY requester_did "
        "ORDER BY last DESC LIMIT ?",
        (time.time() - 30 * 86400, budget * 4)).fetchall()
    n = 0
    for did, _last in rows:
        if n >= budget:
            break
        key = f"ulikes:{did}"
        cached = meta_get(key)
        try:
            if cached and (time.time() - float(cached)) < ttl:
                continue
        except ValueError:
            pass
        uris, _counts, ok = fetch_requester_likes(
            did, int(tcfg.get("requester_likes_max_pages", 3)))
        if not ok:
            continue
        now = time.time()
        with DB_LOCK:
            if uris:
                db().executemany(
                    "INSERT OR REPLACE INTO user_likes(requester_did, post_uri, "
                    "fetched_at) VALUES (?,?,?)",
                    [(did, u, now) for u in uris])
                db().execute(
                    "DELETE FROM user_likes WHERE requester_did=? AND fetched_at < ?",
                    (did, now - 90 * 86400))
            db().commit()
        meta_set(key, now)
        n += 1
    return n


def build_taste_ext():
    """Extended taste: likes from every account listed in
    tension.taste_sources (a neighbouring feed's account, a second
    curator account, any unrelated handle), read via the public repo
    records API - no credentials for those accounts are needed.

    Stored separately from `taste` (this deployment's own owner account) so
    the two vectors can drift user_affinity at different rates
    (months vs years). Cached for taste_refresh_hours.
    """
    sources = (CFG.get("tension") or {}).get("taste_sources") or []
    if not sources:
        return {}
    ttl_h = float(CFG.get("taste_refresh_hours", 6))
    cached_at = meta_get("taste_ext_built_at")
    rows = db().execute("SELECT author_did, likes_count FROM taste_ext").fetchall()
    if rows and cached_at and (time.time() - float(cached_at)) < ttl_h * 3600:
        return {r[0]: r[1] for r in rows}
    counts, appview, tag = {}, CFG["source"]["appview_host"], SVC_TAG
    for src in sources:
        did = src if src.startswith("did:") else None
        if not did:
            try:
                q = urllib.parse.urlencode({"handle": src})
                r = rpc(appview, f"com.atproto.identity.resolveHandle?{q}")
                did = r.get("did")
            except Exception:
                continue
        if not did:
            continue
        for a, n in fetch_liked_authors(did).items():
            counts[a] = counts.get(a, 0) + n
    if not counts:
        return {}
    now = time.time()
    with DB_LOCK:
        db().execute("DELETE FROM taste_ext")
        db().executemany(
            "INSERT OR REPLACE INTO taste_ext(author_did, likes_count, updated_at) "
            "VALUES(?,?,?)",
            [(d, n, now) for d, n in counts.items()])
    db().commit()
    meta_set("taste_ext_built_at", now)
    print(f"[{tag}] taste_ext: {len(counts)} authors from "
          f"{len(sources)} sources", flush=True)
    return counts


def verify_jwt(auth_header, expected_lxm="app.bsky.feed.getFeedSkeleton"):
    """Verify JWT from Authorization header and extract requester DID.

    `expected_lxm` differs per endpoint: getFeedSkeleton for reads,
    sendInteractions for interaction posts.

    Verification resolves the DID document named by the header ``kid`` (the
    signing key's DID — for AppView-minted tokens that is
    ``did:web:bsky.social#atproto``, NOT the issuer's document), matches the
    verification method by exact id, and checks the ES256K signature
    (``publicKeyMultibase`` or ``publicKeyJwk``).

    Fail-closed: any resolver/parse failure returns None, i.e. the request is
    served unauthenticated (no personalization) — a token we could not verify
    is never accepted. Feeds keep running; only personalization pauses.
    """
    if not auth_header:
        return None
    try:
        iss = None
        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != "Bearer":
            return None
        jwt = parts[1]
        jwt_parts = jwt.split(".")
        if len(jwt_parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = jwt_parts
        # decode header and payload (urlsafe base64)
        def b64d(s):
            p = s + "=" * (-len(s) % 4)
            return json.loads(base64.urlsafe_b64decode(p))
        header = b64d(header_b64)
        payload = b64d(payload_b64)
        iss = None
        iss = payload.get("iss")
        aud = payload.get("aud")
        lxm = payload.get("lxm")
        exp = payload.get("exp")
        if not iss:
            return None
        if aud and aud != SERVICE_DID:
            return None
        if lxm and lxm != expected_lxm:
            return None
        if exp is not None and exp < time.time():
            return None
        kid = header.get("kid") or f"{iss}#atproto"
        if not kid or "#" not in kid:
            return None
        # Resolve the KID's DID document (the AppView service key signs the
        # token; kid is e.g. did:web:bsky.social#atproto) — NOT the issuer's.
        doc = _fetch_did_doc(kid.split("#")[0])
        # exact match on the full verification-method id
        vm = None
        for m in doc.get("verificationMethod", []):
            if m.get("id") == kid:
                vm = m
                break
        if vm is None:
            return None
        multibase = vm.get("publicKeyMultibase")
        if multibase:
            pubkey = _b58_to_pubkey(multibase)
        else:
            # some DID docs (did:web) publish publicKeyJwk instead
            jwk = vm.get("publicKeyJwk") or {}
            if jwk.get("kty") != "EC" or jwk.get("crv") != "secp256k1":
                return None
            x = int.from_bytes(base64.urlsafe_b64decode(
                jwk["x"] + "=" * (-len(jwk["x"]) % 4)), "big")
            y = int.from_bytes(base64.urlsafe_b64decode(
                jwk["y"] + "=" * (-len(jwk["y"]) % 4)), "big")
            pubkey = ec.EllipticCurvePublicNumbers(
                x, y, ec.SECP256K1()).public_key()
        # verify the JWS signature (DER first, raw r||s fallback)
        signing_input = f"{header_b64}.{payload_b64}".encode()
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        try:
            pubkey.verify(sig, signing_input, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            if len(sig) != 64:
                return None
            pubkey.verify(_der_from_raw(sig[:32], sig[32:]), signing_input,
                          ec.ECDSA(hashes.SHA256()))
        return iss
    except Exception:
        # fail-closed: an unverifiable token is an unauthenticated request
        # (requester None) — never accept a token we could not verify.
        return None


# --------------------------------------------------------------------------
# personalization (per-user affinity + owner-taste tension)
# --------------------------------------------------------------------------

def _b58_to_pubkey(multibase: str):
    """Decode a multibase(base58btc, z-prefix) DID-doc key to a SECP256K1
    public key. PLC DIDs frame the 33-byte compressed key with a 2-byte
    prefix (0xe7 0x01); scan for the SECP256K1 point prefix."""
    raw = base58.b58decode(multibase[1:])  # strip z
    for i in range(min(4, len(raw))):
        if raw[i] in (0x02, 0x03, 0x04) and (len(raw) - i) in (33, 65):
            return ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256K1(), raw[i:])
    raise ValueError(f"no SECP256K1 key in multibase {multibase[:12]}...")


def _fetch_did_doc(did: str) -> dict:
    """Fetch a DID document — did:plc from plc.directory, did:web from
    https://<host>/.well-known/did.json — cached DID_DOC_CACHE_TTL seconds.
    On a fetch error, falls back to any stale cached doc for that DID
    (keys are stable; this keeps personalization alive through resolver
    outages) and only raises when we have never seen the DID."""
    now = time.time()
    if did in _DID_DOC_CACHE:
        doc, ts = _DID_DOC_CACHE[did]
        if now - ts < DID_DOC_CACHE_TTL:
            return doc
    if did.startswith("did:web:"):
        host = did[len("did:web:"):].split("/")[0]
        url = f"https://{host}/.well-known/did.json"
    else:
        url = f"https://plc.directory/{did}"
    try:
        doc = json.loads(urllib.request.urlopen(
            url, timeout=15).read().decode())
    except Exception:
        stale = _DID_DOC_CACHE.get(did)
        if stale:
            return stale[0]
        raise
    _DID_DOC_CACHE[did] = (doc, now)
    return doc


def _der_from_raw(r: bytes, s: bytes) -> bytes:
    """Encode two 32-byte integers r,s as ASN.1 DER INTEGER SEQUENCE."""
    def enc(b):
        if b[0] & 0x80:
            return b"\x00" + b
        return b
    dr = b"\x02" + bytes([len(enc(r))]) + enc(r)
    ds = b"\x02" + bytes([len(enc(s))]) + enc(s)
    return b"\x30" + bytes([len(dr) + len(ds)]) + dr + ds


EVENT_KINDS = {
    "interactionSeen": "seen", "requestMore": "more", "requestLess": "less",
    "interactionLike": "like", "interactionRepost": "repost",
    "interactionReply": "reply", "interactionQuote": "quote",
}

# Per-event additive step is INTERACTION_WEIGHTS[kind] * AFFINITY_LR, clipped
# to [AFFINITY_MIN, AFFINITY_MAX]. `seen` records an impression but does not
# change affinity (rewarding impressions rich-get-richer's whoever we showed).
# `less` is deliberately violent: one requestLess drives the author of that
# post straight to AFFINITY_MIN (AFFINITY_LR x -5.0 = -1.0), i.e. their weight
# multiplier (1 + affinity) collapses to 0. A "not interested" must be visible
# on the very next page, not after four taps.
INTERACTION_WEIGHTS = {
    "seen": 0.0, "like": 1.0, "repost": 1.5, "reply": 1.2,
    "quote": 1.3, "more": 2.0, "less": -5.0,
}
AFFINITY_LR = 0.2
AFFINITY_MIN, AFFINITY_MAX = -1.0, 2.0


def log_served_posts(requester_did, rkey, uris):
    """Log which posts were served to which user (drives per-user dedup and
    the interaction poller)."""
    if not requester_did or not uris:
        return
    now = time.time()
    with DB_LOCK:
        db().executemany(
            "INSERT OR REPLACE INTO served_posts(requester_did, post_uri, "
            "feed_rkey, served_at, polled_at) VALUES (?,?,?,?,0)",
            [(requester_did, uri, rkey, now) for uri in uris])


def user_excluded_uris(requester_did, include_seen=False, protect_did=None):
    """Post URIs this requester must not be shown.

    Union of:
      * `user_likes`  — posts they already liked. Liking is a global "I have
        had this" signal, so it applies to EVERY feed, not just the one the
        like came from.
      * `user_hidden` — posts they marked "not interested" (sendInteractions
        requestLess), also every feed.
      * `served_posts` — only when the feed opts in with `hide_seen: true`.
        Seen memory is shared by every feed of THIS SERVICE: a post already shown
        in one feed does not come back in another feed of the same
        deployment. Each deployment owns its own database file, so two
        services never share a seen set.
        Bounded by `hide_seen_ttl_h` — past that age a post is eligible again,
        so a heavy scroller cannot exclude every board at once; the
        `hide_min_board` floor is the second guard.

    `protect_did` (callers pass CFG["owner"]["did"]) is never excluded: the
    owner's own posts must keep appearing in the owner's feeds at their
    configured ratio, no matter how often they saw or liked them.
    """
    if not requester_did:
        return set()
    out = set()
    for (uri,) in db().execute(
            "SELECT post_uri FROM user_likes WHERE requester_did=? "
            "UNION SELECT post_uri FROM user_hidden WHERE requester_did=?",
            (requester_did, requester_did)).fetchall():
        out.add(uri)
    if include_seen:
        ttl_h = CFG.get("hide_seen_ttl_h", 24) or 24
        cutoff = time.time() - ttl_h * 3600
        # No feed_rkey filter (shared across the service); the existing
        # idx_served_requester(requester_did, served_at) index serves this.
        for (uri,) in db().execute(
                "SELECT post_uri FROM served_posts WHERE requester_did=? "
                "AND served_at >= ?", (requester_did, cutoff)).fetchall():
            out.add(uri)
    if protect_did and out:
        # Unknown-author URIs stay excluded; only known owner posts are
        # carved back out (chunked: SQLite's variable limit is 999).
        uris = list(out)
        for i in range(0, len(uris), 400):
            chunk = uris[i:i + 400]
            ph = ",".join("?" * len(chunk))
            out -= {r[0] for r in db().execute(
                f"SELECT uri FROM posts WHERE author_did=? AND uri IN ({ph})",
                [protect_did] + chunk).fetchall()}
    return out


def apply_user_exclusions(picks, excluded, hard_hide=False, min_board=20):
    """Remove `excluded` URIs from a board.

    hard_hide=False (the top/window feeds) keeps the historical behaviour:
    the excluded posts sink to the tail, so the board keeps its length and
    the feed never looks empty.
    hard_hide=True (For You feeds) removes them outright, unless that would
    leave fewer than `min_board` posts — then the excluded posts are
    appended at the tail as a starvation fallback. The candidate pool is
    global and finite; without this floor a heavy user could drain the feed
    to nothing (the exact regression that killed the first permanent-seen
    attempt), so keep the floor low but non-zero.
    """
    if not excluded or not picks:
        return list(picks)
    kept = [p for p in picks if p not in excluded]
    if not hard_hide or len(kept) < int(min_board):
        return kept + [p for p in picks if p in excluded]
    return kept


def record_less_hide(requester_did, post_uri, reason="less"):
    """Hard-hide one post for one requester (their "not interested").

    Written by the sendInteractions handler the moment a client reports
    requestLess, so the post is gone from their next page even before the
    poller/process cycle runs; process_interactions() calls it too, for
    events that arrived without a verified requester or from an older client.
    """
    if not requester_did or not post_uri:
        return 0
    with DB_LOCK:
        db().execute(
            "INSERT OR REPLACE INTO user_hidden(requester_did, post_uri, reason, "
            "created_at) VALUES(?,?,?,?)",
            (requester_did, post_uri, reason, time.time()))
        db().commit()
    return 1


TERM_RE = re.compile(r"[a-z0-9]{3,}")

# Tiny stopword list covering the language mix these feeds carry (EN + ES).
# Crude on purpose: a term has to survive as a pattern across several "not
# interested" taps before it bites, so noise self-filters.
NEGATIVE_TERM_STOP = frozenset([
    "the", "and", "for", "you", "your", "with", "that", "this", "from",
    "have", "has", "was", "were", "are", "not", "but", "all", "any", "can",
    "his", "her", "she", "him", "they", "them", "their", "who", "what",
    "when", "how", "why", "our", "out", "one", "two", "get", "got", "just",
    "como", "para", "con", "los", "las", "una", "uno", "que", "por", "mas",
    "del", "sus", "este", "esta", "son", "era", "hay", "muy", "sin", "sobre",
    "pero", "todo", "toda", "todos", "tiene", "hace", "porque", "cuando",
    "https", "http", "www", "com", "htm", "html", "bsky", "app",
])


def extract_terms(text, limit=20):
    """Content terms of a post body: words/numbers >= 3 chars, minus
    stopwords. Hashtags are covered because '#' is not part of the pattern
    ('#photography' -> 'photography')."""
    return [w for w in dict.fromkeys(TERM_RE.findall((text or "").lower()))
            if w not in NEGATIVE_TERM_STOP][:limit]


def learn_negative_terms(requester_did, text, limit=20):
    """Accumulate the vocabulary of a post the requester said "not
    interested" to. Hits matter: one tap is a weak signal, a pattern is a
    strong one."""
    if not requester_did or not text:
        return 0
    terms = extract_terms(text, limit)
    if not terms:
        return 0
    now = time.time()
    with DB_LOCK:
        db().executemany(
            "INSERT INTO user_negative_terms(requester_did, term, hits, "
            "updated_at) VALUES(?,?,1,?) ON CONFLICT(requester_did, term) "
            "DO UPDATE SET hits=user_negative_terms.hits+1, "
            "updated_at=excluded.updated_at",
            [(requester_did, t, now) for t in terms])
        db().commit()
    return len(terms)


def get_negative_terms(requester_did, min_hits=1, cap=50):
    """{term: hits} for a requester, only terms at/above `min_hits`, capped
    to the highest-hit `cap` terms."""
    if not requester_did:
        return {}
    rows = db().execute(
        "SELECT term, hits FROM user_negative_terms WHERE requester_did=? "
        "AND hits >= ? ORDER BY hits DESC, term LIMIT ?",
        (requester_did, int(min_hits), int(cap))).fetchall()
    return {r[0]: r[1] for r in rows}


def negative_term_penalty(text, terms, factor=0.25):
    """Multiplier in [0.5, 1.0] for a post whose body contains terms the
    requester asked to see less of. Saturating: the first hit costs `factor`,
    five or more floor at 0.5 — a down-rank, never a hard hide (an author we
    have no other signal for must still be able to reach the user)."""
    if not terms or not text:
        return 1.0
    low = text.lower()
    hits = sum(h for t, h in terms.items() if t in low)
    if not hits:
        return 1.0
    return max(0.5, 1.0 - float(factor) * min(hits, 5))


def personalize_page(requester_did, rkey, board, scores):
    """Serve-time per-user personalization for CFG["tension"]["personal_feeds"]
    (the For You feeds).

    1. Re-rank the whole board by global_score * (1 + user_affinity[author]).
       Affinity lives in [-1, 2]: a requestLess'd author sinks toward zero
       weight, a much-liked author rises up to 3x. Deterministic (URI tiebreak).
    2. Weave exploration slots through the board: posts from authors the
       OWNER clearly likes (taste top half / topic_seeds) that this user has no
       affinity signal for yet. This is the visible part of "tension": the
       owner's taste reaches users before they interact with it.

    All other feeds are returned unchanged — they carry the owner's taste
    through the shared baseline (taste_weight in rank_score) instead.
    """
    tcfg = CFG.get("tension") or {}
    if rkey not in set(tcfg.get("personal_feeds") or []):
        return board
    if not board or not scores:
        return board

    aff = {r[0]: r[1] for r in db().execute(
        "SELECT author_did, score FROM user_affinity WHERE requester_did=?",
        (requester_did,)).fetchall()}
    ph = ",".join("?" * len(board))
    rows = db().execute(
        f"SELECT uri, author_did, text FROM posts WHERE uri IN ({ph})",
        list(board)).fetchall()
    auth = {u: d for u, d, _t in rows}
    texts = {u: (t or "") for u, _d, t in rows}
    # What this requester asked to see less of (sendInteractions requestLess)
    neg = get_negative_terms(requester_did,
                             tcfg.get("negative_term_min_hits", 1))
    nfac = float(tcfg.get("negative_term_factor", 0.25))

    def adj(uri):
        a = max(-0.9, min(2.0, aff.get(auth.get(uri, ""), 0.0)))
        pen = negative_term_penalty(texts.get(uri, ""), neg, nfac)
        return scores.get(uri, 0.0) * (1.0 + a) * pen

    ranked = sorted(board, key=lambda u: (-adj(u), u))

    share = float(tcfg.get("explore_share", 0.0))
    if share > 0:
        ranked = inject_explore_slots(ranked, scores, aff, auth, share)
    return ranked


COLD_START_PAGES = 2
COLD_START_SCORE = 0.3


def seed_cold_start(requester_did):
    """Cold-start: seed a brand-new requester's user_affinity from their
    PUBLIC likes (com.atproto.repo.listRecords is public on the PDS), so
    their very first page is already personal — and record those liked post
    URIs in `user_likes`, so a post they already liked stops being served to
    them from their very first request. One-shot per user via a meta flag;
    failures are logged and retried on a later request (the flag is only set
    after a successful fetch)."""
    if not requester_did:
        return 0
    flag = f"coldstart:{requester_did}"
    if meta_get(flag):
        return 0
    if db().execute("SELECT 1 FROM user_affinity WHERE requester_did=? LIMIT 1",
                    (requester_did,)).fetchone():
        meta_set(flag, str(time.time()))
        return 0
    uris, counts, ok = fetch_requester_likes(requester_did, COLD_START_PAGES)
    if not ok:
        return 0
    now = time.time()
    with DB_LOCK:
        if counts:
            db().executemany(
                "INSERT OR REPLACE INTO user_affinity(requester_did, author_did, "
                "score, updated_at) VALUES (?,?,?,?)",
                [(requester_did, a, COLD_START_SCORE, now) for a in counts])
        if uris:
            db().executemany(
                "INSERT OR REPLACE INTO user_likes(requester_did, post_uri, "
                "fetched_at) VALUES (?,?,?)",
                [(requester_did, u, now) for u in uris])
        db().commit()
    meta_set(flag, str(now))
    meta_set(f"ulikes:{requester_did}", now)
    print(f"[{SVC_TAG}] cold-start seeded {len(counts)} authors / "
          f"{len(uris)} liked posts for {requester_did[:24]}", flush=True)
    return len(counts)


def log_metrics(picks_by_feed, feed_scores, now_ts):
    """Append per-feed diversity/freshness/score metrics for this refresh;
    prune rows older than 90 days. Never raises."""
    try:
        ts = time.time()

        def _med(v):
            if not v:
                return None
            v = sorted(v)
            return v[len(v) // 2] if len(v) % 2 else \
                (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2

        rows = []
        for rkey, uris in (picks_by_feed or {}).items():
            uris = list(uris or [])
            if not uris:
                rows.append((ts, rkey, 0, 0, None, None, None))
                continue
            scores = (feed_scores or {}).get(rkey, {})
            authors, ages, sc = set(), [], []
            for chunk_start in range(0, len(uris), 500):
                chunk = uris[chunk_start:chunk_start + 500]
                q = ",".join("?" * len(chunk))
                for uri, a, idx in db().execute(
                        f"SELECT uri, author_did, indexed_at FROM posts "
                        f"WHERE uri IN ({q})", chunk):
                    authors.add(a)
                    try:
                        ages.append((now_ts - parse_time(idx)).total_seconds()
                                    / 3600)
                    except Exception:
                        pass
                    s = scores.get(uri)
                    if s is not None:
                        sc.append(s)
            sc_sorted = sorted(sc)
            p90 = sc_sorted[min(len(sc_sorted) - 1,
                                int(len(sc_sorted) * 0.9))] if sc_sorted else None
            rows.append((ts, rkey, len(uris), len(authors),
                         round(_med(ages), 2) if ages else None,
                         round(_med(sc), 2) if sc else None,
                         round(p90, 2) if p90 is not None else None))
        with DB_LOCK:
            db().executemany(
                "INSERT OR REPLACE INTO metrics_log(ts, feed_rkey, posts, "
                "unique_authors, median_age_hours, score_median, score_p90) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            db().execute("DELETE FROM metrics_log WHERE ts < ?",
                         (ts - 90 * 86400,))
        print(f"[{SVC_TAG}] metrics logged for {len(rows)} feeds", flush=True)
    except Exception as e:
        print(f"[{SVC_TAG}] metrics failed: {e}", flush=True)


def inject_explore_slots(ranked, scores, aff, auth, share):
    """Weave owner-taste posts (no user signal yet) through the board at
    regular intervals. Deterministic: slot positions and picks are stable
    given the same data."""
    taste = {r[0]: r[1] for r in db().execute(
        "SELECT author_did, likes_count FROM taste").fetchall()}
    with CACHE_LOCK:
        seeds = set(CACHE.get("topic_seeds") or [])
    if not taste and not seeds:
        return ranked
    top = max(taste.values()) or 1
    owner_did = CFG.get("owner", {}).get("did")

    def explore_worthy(author):
        if not author or author == owner_did:
            return False
        if aff.get(author, 0.0) != 0.0:
            return False
        return author in seeds or (taste.get(author, 0) / top) >= 0.5

    picks, rest = [], []
    for u in ranked:
        if explore_worthy(auth.get(u)):
            picks.append(u)
        else:
            rest.append(u)
    if not picks:
        return ranked
    spacing = max(4, int(round(1.0 / share)))
    out = []
    qi = 0
    for i, u in enumerate(rest):
        if qi < len(picks) and (i + 1) % spacing == 0:
            out.append(picks[qi])
            qi += 1
        out.append(u)
    out.extend(picks[qi:])
    return out


def poll_interactions():
    """Turn served posts into observed interactions.

    The official client reports only seen/requestMore/requestLess through
    sendInteractions — likes and reposts are ordinary records with no
    per-user visibility. This poller closes that gap for our own audience:
    for posts we served to a requester recently, ask the AppView who
    liked/reposted the post; if the requester appears, that is a like or
    repost BY that requester of the AUTHOR's post, and becomes an
    interaction event.

    Budget-capped: at most poll_budget posts per run, each post polled once
    (polled_at flag), and posts with zero likes/reposts are skipped without
    an API call.
    """
    now = time.time()
    last = float(meta_get("affinity_poll_at", 0))
    tcfg = CFG.get("tension") or {}
    if now - last < float(tcfg.get("poll_interval_secs", 1800)):
        return
    budget = int(tcfg.get("poll_budget", 120))
    rows = db().execute(
        "SELECT sp.requester_did, sp.post_uri, p.author_did, "
        "p.like_count, p.repost_count FROM served_posts sp "
        "JOIN posts p ON p.uri = sp.post_uri "
        "WHERE sp.polled_at = 0 AND sp.served_at > ? AND p.author_did IS NOT NULL "
        "ORDER BY sp.served_at DESC LIMIT ?",
        (now - 48 * 3600, budget)).fetchall()
    if not rows:
        meta_set("affinity_poll_at", now)
        return
    handle = ENV.get("BSKY_HANDLE")
    password = ENV.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        return
    try:
        sess = rpc(CFG["source"]["pds_host"], "com.atproto.server.createSession",
                   {"identifier": handle, "password": password})
        token = sess["accessJwt"]
    except Exception as e:
        print(f"[{SVC_TAG}] affinity poll: auth failed: {e}", flush=True)
        return
    appview = CFG["source"]["appview_host"]

    events, marked = [], []
    for requester_did, post_uri, author_did, likes, reposts in rows:
        if requester_did and author_did != requester_did and (likes or reposts):
            for method, kind in (("app.bsky.feed.getLikes", "like"),
                                 ("app.bsky.feed.getRepostedBy", "repost")):
                if kind == "like" and not likes:
                    continue
                if kind == "repost" and not reposts:
                    continue
                try:
                    qq = urllib.parse.urlencode({"uri": post_uri, "limit": 100})
                    d = rpc(appview, f"{method}?{qq}", token=token, timeout=30)
                except Exception as e:
                    print(f"[{SVC_TAG}] affinity poll: {method} failed: {e}", flush=True)
                    break
                if kind == "like":
                    actors = [l.get("actor", {}).get("did") for l in d.get("likes", [])]
                else:
                    actors = [a.get("did") for a in d.get("repostedBy", [])]
                if requester_did in actors:
                    events.append((post_uri, "", kind, now, requester_did))
            time.sleep(0.1)
        marked.append((now, requester_did, post_uri))
    with DB_LOCK:
        if events:
            db().executemany(
                "INSERT INTO interaction_events(post_uri, feed_rkey, kind, "
                "created_at, requester_did) VALUES(?,?,?,?,?)", events)
        db().executemany(
            "UPDATE served_posts SET polled_at=? WHERE requester_did=? AND post_uri=?",
            marked)
    db().commit()
    meta_set("affinity_poll_at", now)
    print(f"[{SVC_TAG}] affinity poll: {len(rows)} posts, {len(events)} new events",
          flush=True)


def process_interactions():
    """Fold unprocessed interaction events into per-user, per-author affinity.

    Additive steps with clipping — predictable, and `less` reliably pushes
    an author down instead of being swallowed by a multiplicative blend.

    `less` (the client's "not interested") does two extra things here: it
    hard-hides that post for that requester (record_less_hide — the
    sendInteractions handler already did it when the event carried a verified
    requester, and this covers events that arrived without one) and folds the
    post's vocabulary into the requester's negative-term set.
    """
    now = time.time()
    rows = db().execute(
        "SELECT ie.id, ie.requester_did, ie.kind, p.author_did, ie.post_uri, "
        "p.text FROM interaction_events ie LEFT JOIN posts p "
        "ON p.uri = ie.post_uri "
        "WHERE ie.processed = 0 ORDER BY ie.id LIMIT 5000").fetchall()
    if not rows:
        return 0
    folded = 0
    for _id, requester, kind, author, post_uri, ptext in rows:
        if requester and author and author != requester:
            w = INTERACTION_WEIGHTS.get(kind, 0.0)
            if w:
                step = AFFINITY_LR * w
                db().execute(
                    "INSERT INTO user_affinity(requester_did, author_did, "
                    "score, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(requester_did, author_did) DO UPDATE SET "
                    "score = MAX(?, MIN(?, user_affinity.score + ?)), "
                    "updated_at = ?",
                    (requester, author, step, now,
                     AFFINITY_MIN, AFFINITY_MAX, step, now))
                folded += 1
        if requester and kind == "less" and post_uri:
            record_less_hide(requester, post_uri, "less")
            learn_negative_terms(requester, ptext)
        db().execute("UPDATE interaction_events SET processed = 1 WHERE id = ?",
                     (_id,))
    db().commit()
    return folded


def apply_owner_gravity():
    """Two-rate owner-taste drift (runs at most once per UTC day).

    Every existing user_affinity row blends toward TWO targets:
      - local  taste (this feed's own owner account) at
        owner_gravity_daily -- default 0.004/day, ~30% convergence
        in ~3 months
      - extended taste (taste_sources: the other feed's account and
        any unrelated handles) at extended_gravity_daily -- default
        0.0004/day, ~30% convergence in ~2.4 years ("much slighter")

    If the extended vector is empty (no taste_sources configured),
    g_ext is clamped to 0 so the behaviour stays byte-identical to
    the previous single-rate implementation. Owners never drift
    below their own fresh interactions.
    """
    tcfg = CFG.get("tension") or {}
    g_local = float(tcfg.get("owner_gravity_daily", 0.0))
    g_ext   = float(tcfg.get("extended_gravity_daily", 0.0))
    if g_local <= 0 and g_ext <= 0:
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if meta_get("gravity_day") == today:
        return 0
    local = {r[0]: r[1] for r in
             db().execute("SELECT author_did, likes_count FROM taste").fetchall()}
    ext   = {r[0]: r[1] for r in
             db().execute("SELECT author_did, likes_count FROM taste_ext").fetchall()}
    if not local and not ext:
        meta_set("gravity_day", today)
        return 0
    lt = max(local.values()) if local else 1
    et = max(ext.values()) if ext else 1
    if not ext:
        g_ext = 0.0
    ua = db().execute(
        "SELECT requester_did, author_did, score FROM user_affinity").fetchall()
    if not ua:
        meta_set("gravity_day", today)
        return 0
    now = time.time()
    with DB_LOCK:
        db().executemany(
            "UPDATE user_affinity SET score = ?, updated_at = ? "
            "WHERE requester_did = ? AND author_did = ?",
            [(min(AFFINITY_MAX, max(AFFINITY_MIN,
                  s * (1 - g_local - g_ext)
                  + (local.get(a, 0) / lt) * g_local
                  + (ext.get(a, 0) / et) * g_ext)),
              now, req, a) for req, a, s in ua])
    db().commit()
    meta_set("gravity_day", today)
    print(f"[{SVC_TAG}] owner gravity: drifted {len(ua)} rows "
          f"(local g={g_local}, extended g={g_ext})", flush=True)
    return len(ua)


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------

def refresh():
    # Adaptive keyword weights age continuously: decay them (at most once
    # an hour) so a term the community stopped using fades out.
    if kw_enabled():
        kw_now = time.time()
        if kw_now - float(meta_get("keyword_decay_at", 0) or 0) > kw_cfg().get("decay_secs", 3600):
            with DB_LOCK:
                kw_decay(db(), kw_now)
            meta_set("keyword_decay_at", kw_now)

    src = CFG["source"]
    handle = ENV.get("BSKY_HANDLE")
    password = ENV.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        raise RuntimeError("missing BSKY_HANDLE/BSKY_APP_PASSWORD in feedgen .env")
    sess = rpc(src["pds_host"], "com.atproto.server.createSession",
               {"identifier": handle, "password": password})
    token = sess["accessJwt"]

    now = datetime.now(timezone.utc)
    posts, fstats = fetch_all(token)
    record_posts(posts)
    snapshot_engagements(posts)

    topic_affinity, topic_seeds = build_topic_affinity()
    taste = build_taste(token)
    try:
        build_taste_ext()
    except Exception as e:
        print(f"[{SVC_TAG}] taste_ext build failed: {e}", flush=True)
    priors, _list_median = build_author_priors()
    ctx = {
        "topic_affinity": topic_affinity, "topic_seeds": topic_seeds, "taste": taste,
        "topic_keywords": [k.lower() for k in CFG.get("topic_keywords", [])],
        "author_priors": priors,
    }

    owner = CFG.get("owner", {})
    picks, stats, feed_scores = {}, {}, {}
    shown = []
    now_ts = time.time()
    for f in CFG["feeds"]:
        ttl = f.get("seen_ttl_hours", 24)
        suppressed = set()
        if ttl:
            rows = db().execute(
                "SELECT uri FROM seen WHERE rkey=? AND shown_at > ?",
                (f["rkey"], now_ts - ttl * 3600)).fetchall()
            suppressed = {r[0] for r in rows}
        scored, st = select(posts, f, now, owner, ctx, suppressed)
        picks[f["rkey"]] = [u for u, _s in scored]
        feed_scores[f["rkey"]] = {u: s for u, s in scored}
        stats[f["rkey"]] = st | {"suppressed_set": len(suppressed), "ttl": ttl}
        shown.extend((f["rkey"], u) for u in picks[f["rkey"]])

    if shown:
        with DB_LOCK:
            db().executemany(
                """INSERT INTO seen(rkey,uri,shown_at,shown_count) VALUES(?,?,?,1)
                   ON CONFLICT(rkey,uri) DO UPDATE SET
                     shown_at=excluded.shown_at,
                     shown_count=seen.shown_count+1""",
                [(r, u, now_ts) for r, u in shown])
    prune_state(now_ts)

    # personalization pipeline: observe -> fold -> daily owner-taste drift
    poll_interactions()
    process_interactions()
    apply_owner_gravity()
    try:
        warm = refresh_all_requester_likes()
        if warm:
            print(f"[{SVC_TAG}] refreshed likes for {warm} requesters", flush=True)
    except Exception as e:
        print(f"[{SVC_TAG}] requester-like refresh failed: {e}", flush=True)

    meta_set("last_refresh", now_ts)
    with CACHE_LOCK:
        total_seen = db().execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        CACHE["feed_scores"] = feed_scores
        CACHE["topic_seeds"] = set(topic_seeds)
    log_metrics(CACHE["feeds"], CACHE.get("feed_scores") or {}, time.time())
    fstats["state_uris"] = total_seen
    fstats["taste_authors"] = len(taste)
    fstats["topic_authors"] = len(topic_affinity)
    fstats["posts_stored"] = len(posts)
    return picks, stats, fstats


def refresh_loop():
    while True:
        try:
            picks, stats, fstats = refresh()
            with CACHE_LOCK:
                CACHE.update(feeds=picks, stats=stats, updated=time.time(),
                             scanned=fstats["posts_stored"],
                             members=fstats["list_items"], error=None,
                             gen=CACHE["gen"] + 1, **{
                                 k: fstats[k] for k in
                                 ("state_uris", "taste_authors", "topic_authors")})
                CACHE["fetch"] = fstats
            per_feed = " ".join(f"{k}={len(v)}" for k, v in picks.items())
            print(f"[{SVC_TAG}] refresh ok: total={sum(len(v) for v in picks.values())} "
                  f"{per_feed} | scanned={fstats['list_items']} "
                  f"pages={fstats['pages']} owner={fstats['owner_posts']} "
                  f"state={fstats['state_uris']}", flush=True)
        except Exception as e:
            with CACHE_LOCK:
                CACHE["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[{SVC_TAG}] refresh failed: {e}", flush=True)
        time.sleep(CFG.get("refresh_secs", 600))


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "feedgen/1.0"

    def _send(self, code, obj, ctype="application/json", headers=None):
        body = json.dumps(obj).encode() if isinstance(obj, (dict, list)) else obj
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/.well-known/did.json":
            return self._send(200, {
                "@context": ["https://www.w3.org/ns/did/v1"],
                "id": SERVICE_DID,
                "service": [{"id": "#bsky_fg", "type": "BskyFeedGenerator",
                             "serviceEndpoint": f"https://{HOSTNAME}"}],
            })

        if u.path == "/xrpc/app.bsky.feed.describeFeedGenerator":
            return self._send(200, {"did": SERVICE_DID,
                                    "feeds": [{"uri": uri, "acceptsInteractions": True} for uri in FEEDS]})

        if u.path == "/xrpc/app.bsky.feed.getFeedSkeleton":
            uri = q.get("feed", [None])[0]
            if uri not in FEEDS:
                return self._send(400, {"error": "UnknownFeed", "message": "unknown feed"})
            try:
                limit = max(1, min(100, int(q.get("limit", ["50"])[0])))
            except ValueError:
                limit = 50
            raw = q.get("cursor", ["0"])[0]
            gen_of_cursor = None
            if ":" in raw:
                gen_of_cursor, _, raw = raw.partition(":")
            try:
                offset = max(0, int(raw))
            except ValueError:
                offset = 0

            # Extract requester DID from JWT for per-user dedup
            auth_header = self.headers.get("Authorization")
            requester_did = verify_jwt(auth_header) if auth_header else None

            if requester_did:
                try:
                    seed_cold_start(requester_did)
                except Exception:
                    pass

            with CACHE_LOCK:
                rkey = FEEDS[uri]["rkey"]
                picks = list(CACHE["feeds"].get(rkey, []))
                feed_scores = dict(CACHE.get("feed_scores", {}).get(rkey, {}))
                gen = CACHE["gen"]

            # Per-user exclusions: posts this requester already liked or
            # marked "not interested" (every feed), plus posts they were
            # already served anywhere in this service — seen memory is shared
            # across all of this deployment's feeds, bounded by hide_seen_ttl_h.
            # All reads are local; user_likes is kept warm in the background
            # by refresh_all_requester_likes(). Owner-authored posts are
            # never excluded, so the owner's own posts keep their ratio.
            hide_seen = bool(FEEDS[uri].get("hide_seen", 0))
            excluded = set()
            if requester_did:
                excluded = user_excluded_uris(
                    requester_did, hide_seen,
                    protect_did=(CFG.get("owner") or {}).get("did"))

            if excluded:
                picks = apply_user_exclusions(
                    picks, excluded, hard_hide=hide_seen,
                    min_board=CFG.get("hide_min_board", 20))

            # Per-user personalization (serve-time re-rank + exploration slots)
            if requester_did:
                picks = personalize_page(requester_did, rkey, picks, feed_scores)

            with CACHE_LOCK:
                CACHE.setdefault("hide_stats", {})[rkey] = {
                    "excluded": len(excluded), "board": len(picks)}

            page = picks[offset:offset + limit]

            if requester_did and page:
                log_served_posts(requester_did, rkey, page)

            items = []
            for p in page:
                s = feed_scores.get(p)
                fc = f"{rkey}|{int(round(s))}" if s is not None else rkey
                items.append({"post": p, "feedContext": fc[:128]})
            out = {"feed": items}
            # Generational cursor: a client paging across a refresh gets
            # current-generation data rather than a silently shifted window.
            if offset + limit < len(picks):
                out["cursor"] = f"{gen}:{offset + limit}"
            elif gen_of_cursor is not None and gen_of_cursor != str(gen):
                out["cursor"] = f"{gen}:{offset}"
            return self._send(200, out, headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache"})

        if u.path in ("/", "/health"):
            with CACHE_LOCK:
                snap = {k: v for k, v in CACHE.items()}
                snap["feeds"] = {rk: len(v) for rk, v in CACHE["feeds"].items()}
                snap["stats"] = {k: dict(v) for k, v in CACHE["stats"].items()}
            rows = "".join(
                f"<li><code>{f['rkey']}</code> ({f['display_name']}): "
                f"<b>{snap['feeds'].get(f['rkey'], 0)}</b> posts</li>"
                for f in CFG["feeds"])
            stat_rows = "".join(
                "<tr><td>{rk}</td><td>{in_window}</td><td>{pass_share}</td>"
                "<td>{pass_gates}</td><td>{suppressed}</td><td>{owner}</td>"
                "<td>{final}</td><td>{ttl}h</td></tr>".format(rk=k, **v)
                for k, v in snap["stats"].items())
            fetch = snap.get("fetch", {})
            try:
                pstats = db().execute(
                    "SELECT (SELECT COUNT(DISTINCT requester_did) FROM served_posts), "
                    "(SELECT COUNT(*) FROM interaction_events), "
                    "(SELECT COUNT(*) FROM user_affinity)").fetchone()
            except sqlite3.Error:
                pstats = (0, 0, 0)
            mrows = ""
            try:
                for rk, n, ua, ma, sm, sp in db().execute(
                        "SELECT feed_rkey, posts, unique_authors, "
                        "median_age_hours, score_median, score_p90 FROM "
                        "metrics_log WHERE ts=(SELECT MAX(ts) FROM metrics_log)"):
                    mrows += (f"<tr><td>{rk}</td><td>{n}</td><td>{ua}</td>"
                              f"<td>{ma}</td><td>{sm}</td><td>{sp}</td></tr>")
            except sqlite3.Error:
                pass
            metrics_html = (f"<h2>metrics (latest refresh)</h2><table border=1>"
                            f"<tr><th>feed</th><th>posts</th><th>unique authors</th>"
                            f"<th>median age h</th><th>score median</th>"
                            f"<th>score p90</th></tr>{mrows}</table>")
            html = (
                f"{metrics_html}"
                f"<h1>Top-posts feeds</h1><ul>{rows}</ul>"
                f"<h2>per-feed pipeline</h2>"
                f"<table border=1 cellpadding=4><tr><th>feed</th><th>in_window</th>"
                f"<th>pass_share</th><th>pass_gates</th><th>suppressed</th>"
                f"<th>owner</th><th>final</th><th>ttl</th></tr>{stat_rows}</table>"
                f"<p>scanned={snap['scanned']} list_items={fetch.get('list_items','?')} "
                f"pages={fetch.get('pages','?')} owner_posts={fetch.get('owner_posts','?')} "
                f"updated={snap['updated']:.0f} gen={snap.get('gen','?')} "
                f"error={snap['error']}</p>"
                f"<p>state_rows={snap.get('state_uris','?')} "
                f"topic_authors={snap.get('topic_authors','?')} "
                f"taste_authors={snap.get('taste_authors','?')} "
                f"db={os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0} bytes</p>"
                f"<p>personalization: requesters={pstats[0]} events={pstats[1]} "
                f"affinity_rows={pstats[2]}</p>"
                f"<p>hide: " + " ".join(
                    f"{rk}=excl{h.get('excluded', 0)}/board{h.get('board', 0)}"
                    for rk, h in sorted((snap.get("hide_stats") or {}).items()))
                + "</p>"
            ).encode()
            return self._send(200, html, "text/html")

        return self._send(404, {"error": "NotFound"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/xrpc/app.bsky.feed.sendInteractions":
            return self._handle_send_interactions()
        return self._send(404, {"error": "NotFound"})

    def _handle_send_interactions(self):
        """app.bsky.feed.sendInteractions — per the lexicon each interaction
        carries `item` (post AT-URI), `event` (e.g. 'interactionSeen',
        'requestMore', 'requestLess') and `feedContext` (what we attached to
        the skeleton item). Events are folded into user_affinity by
        process_interactions() on the next refresh; `requestLess` also hides
        the post for that requester immediately (record_less_hide), so their
        next page no longer carries it.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            data = json.loads(body or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "InvalidJSON"})
        auth_header = self.headers.get("Authorization")
        requester_did = None
        if auth_header:
            requester_did = verify_jwt(auth_header, "app.bsky.feed.sendInteractions")
        now = time.time()
        rows = []
        for inter in data.get("interactions", []):
            item = inter.get("item") or ""
            event = (inter.get("event") or "").rsplit("#", 1)[-1]
            kind = EVENT_KINDS.get(event, event)
            fctx = inter.get("feedContext") or ""
            # we emit "<rkey>|<score>"; some clients send a feed URI instead
            feed_rkey = (fctx.split("|", 1)[0] if "|" in fctx
                         else fctx.rsplit("/", 1)[-1])
            if not item or not kind:
                continue
            if kind == "less" and requester_did:
                record_less_hide(requester_did, item, "less")
            rows.append((item, feed_rkey, kind, now, requester_did))
        if rows:
            with DB_LOCK:
                db().executemany(
                    "INSERT INTO interaction_events(post_uri, feed_rkey, kind, "
                    "created_at, requester_did) VALUES(?,?,?,?,?)", rows)
            db().commit()
        return self._send(200, {})

    def log_message(self, *a):
        pass


def warm_start():
    """First refresh, then the periodic loop. Runs in a background thread so the
    HTTP port is already open while the scan runs — a cold warm-up can take
    minutes (the scan walks the list members' recent posts) and clients used to
    get 'connection refused' for that whole window. Until the first refresh
    lands the feeds serve empty, exactly as they do after a failed refresh."""
    print(f"[{SVC_TAG}] warming cache (first refresh)...", flush=True)
    try:
        picks, stats, fstats = refresh()
        with CACHE_LOCK:
            CACHE.update(feeds=picks, stats=stats, updated=time.time(),
                         scanned=fstats["posts_stored"], members=fstats["list_items"],
                         gen=1, **{k: fstats[k] for k in
                                   ("state_uris", "taste_authors", "topic_authors")})
            CACHE["fetch"] = fstats
        print(f"[{SVC_TAG}] warm ok: {sum(len(v) for v in picks.values())} picks "
              f"({', '.join(f'{k}={len(v)}' for k, v in picks.items())})", flush=True)
    except Exception as e:
        CACHE["error"] = f"warm failed: {e}"[:300]
        print(f"[{SVC_TAG}] warm failed (serving empty until refresh): {e}", flush=True)
    refresh_loop()


def main():
    migrate_legacy_state()
    migrate_schema()
    if kw_enabled():
        print(f"[{SVC_TAG}] keyword weights seeded ({kw_init()} new)", flush=True)
    threading.Thread(target=warm_start, daemon=True).start()
    print(f"[{SVC_TAG}] listening on 127.0.0.1:{CFG.get('port', 8080)} "
          f"(warming in the background)", flush=True)
    HTTPServer(("127.0.0.1", CFG.get("port", 8080)), Handler).serve_forever()


if __name__ == "__main__":
    main()
