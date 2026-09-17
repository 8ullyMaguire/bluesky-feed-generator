#!/usr/bin/env python3
"""Prove the per-user exclusion + not-interested path end-to-end against a
COPY of the live DB. The live store is only read; nothing here writes to
production data.

  python3 diagnostics/simulate_user_exclusions.py <live.sqlite> <service.py> <rkey>

What it checks, using the owner DID as a synthetic requester:
  1. picks a real corpus board (highest-engagement posts)
  2. inserts the top post into user_likes and the second into user_hidden,
     plus the third into served_posts when the feed sets hide_seen
  3. user_excluded_uris() must return exactly those URIs (owner posts aside)
  4. apply_user_exclusions() must drop them from the served board (hard hide
     for hide_seen feeds) or tail them (window feeds)
  5. a synthetic `less` interaction event for a real stored post is folded by
     process_interactions() -> user_hidden + user_negative_terms rows
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time

live = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.local/share/feedgen/feedgen.sqlite")
modname = sys.argv[2] if len(sys.argv) > 2 else "feedgen.py"
rkey = sys.argv[3] if len(sys.argv) > 3 else "for-you"

tmp = tempfile.mkdtemp(prefix="sim-excl-")
copy = os.path.join(tmp, "copy.sqlite")
# sqlite may be in WAL mode: copy the sidecars too, or the copy is stale
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(live + suffix):
        shutil.copy2(live + suffix, copy + suffix)
os.environ["FEEDGEN_DB"] = copy

here = os.path.dirname(os.path.abspath(__file__))
svc_dir = os.path.dirname(here)
if svc_dir not in sys.path:
    sys.path.insert(0, svc_dir)   # the service imports ranking_core by bare name
path = os.path.join(svc_dir, modname)
spec = importlib.util.spec_from_file_location("svc", path)
svc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(svc)

fcfg = next(f for f in svc.CFG["feeds"] if f["rkey"] == rkey)
hide_seen = bool(fcfg.get("hide_seen", 0))
min_board = svc.CFG.get("hide_min_board", 20)
owner = (svc.CFG.get("owner") or {}).get("did")
print(f"service={modname} rkey={rkey} hide_seen={hide_seen} "
      f"hide_min_board={min_board} owner_ratio={svc.CFG['owner']['max_ratio']} "
      f"max_posts={fcfg.get('max_posts')}")

board = [r[0] for r in svc.db().execute(
    "SELECT uri FROM posts WHERE author_did != ? "
    "ORDER BY like_count DESC LIMIT 40", (owner,)).fetchall()]
assert len(board) >= 5, "corpus too small for this probe"
requester = owner or "did:plc:probe"

liked, hidden, served = board[0], board[1], board[2]
now = time.time()
svc.db().execute("DELETE FROM user_likes WHERE requester_did=?", (requester,))
svc.db().execute("DELETE FROM user_hidden WHERE requester_did=?", (requester,))
svc.db().execute("DELETE FROM served_posts WHERE requester_did=?", (requester,))
svc.db().execute("INSERT INTO user_likes VALUES(?,?,?)", (requester, liked, now))
svc.db().execute("INSERT INTO user_hidden VALUES(?,?,?,?)",
                 (requester, hidden, "less", now))
if hide_seen:
    svc.db().execute("INSERT INTO served_posts VALUES(?,?,?,?,0)",
                     (requester, served, rkey, now))

excl = svc.user_excluded_uris(requester, hide_seen, protect_did=owner)
assert liked in excl and hidden in excl, excl
if hide_seen:
    assert served in excl, excl
page = svc.apply_user_exclusions(board, excl, hard_hide=hide_seen,
                                 min_board=min_board)
if hide_seen and (len(board) - len(excl)) >= min_board:
    # hard hide: gone from the whole served board
    assert liked not in page, "liked post still served"
    assert hidden not in page, "hidden post still served"
    assert len(page) == len(board) - len(excl), (len(page), len(board), len(excl))
    print(f"excluded={len(excl)} board={len(board)} served={len(page)} "
          f"mode=hard-hide liked_absent=ok hidden_absent=ok")
else:
    # tail ordering (non-hide_seen feeds, or a starvation-floor fallback)
    assert len(page) == len(board), (len(page), len(board))
    assert page.index(liked) > board.index(liked), "liked post not tailed"
    print(f"excluded={len(excl)} board={len(board)} served={len(page)} "
          f"mode=tail liked_tailed=ok hidden_tailed=ok")

# not-interested learning, through the real folder
victim = board[3]
svc.db().execute("DELETE FROM interaction_events WHERE requester_did=?",
                 (requester,))
svc.db().execute("INSERT INTO interaction_events(post_uri, feed_rkey, kind, "
                 "created_at, requester_did) VALUES(?,?,?,?,?)",
                 (victim, rkey, "less", now, requester))
svc.process_interactions()
n_hidden = svc.db().execute(
    "SELECT COUNT(*) FROM user_hidden WHERE requester_did=? AND post_uri=?",
    (requester, victim)).fetchone()[0]
n_terms = svc.db().execute(
    "SELECT COUNT(*) FROM user_negative_terms WHERE requester_did=?",
    (requester,)).fetchone()[0]
assert n_hidden == 1, n_hidden
assert n_terms >= 1, n_terms
titles = [r[0] for r in svc.db().execute(
    "SELECT term FROM user_negative_terms WHERE requester_did=? LIMIT 5",
    (requester,)).fetchall()]
print(f"less_folded hidden_rows={n_hidden} learned_terms={n_terms} "
      f"sample_terms={titles}")

# the owner's own posts are never excluded, even when liked/hidden
mine = svc.db().execute(
    "SELECT uri FROM posts WHERE author_did=? LIMIT 1", (owner,)).fetchone()
if mine:
    svc.db().execute("INSERT OR REPLACE INTO user_likes VALUES(?,?,?)",
                     (requester, mine[0], now))
    after = svc.user_excluded_uris(requester, hide_seen, protect_did=owner)
    assert mine[0] not in after, "owner post was excluded"
    print(f"owner_protection=ok ({mine[0][:48]})")

print("SIMULATION PASS")
