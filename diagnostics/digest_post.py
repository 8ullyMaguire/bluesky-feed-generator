#!/usr/bin/env python3
"""Compose a weekly digest of what a deployment's boards are actually serving.

The boards exist and nobody reads them: the Spanish boards recorded zero readers in
their first week, and every board is a public generator that nobody has been told
about. A digest post is the cheapest distribution that does not require an OAuth
login — it points at posts, and the posts point back at the feed.

This tool only composes. It prints the text, the record it would create, and the
length checks. `--publish` is opt-in and needs a session; without it nothing is
written anywhere.

    python3 diagnostics/digest_post.py --db ~/.local/share/feedgen/feedgen.sqlite \
        --config misc/feedgen/config.json --since-days 7 --top 3

The digest is built from served board state, not from the corpus, so it can only
ever recommend something a reader could have been shown.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

GRAPHEME_CAP = 300
PDS = "https://bsky.social"
URL_RE = re.compile(r"https?://[^\s]+")


def con_ro(path):
    if not os.path.exists(path):
        sys.exit(f"no database at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def boards(db, since_ts):
    """Feed key -> (display name, description, post count) for populated boards."""
    rows = db.execute(
        "SELECT feed_rkey, COUNT(DISTINCT uri) FROM board_scores "
        "WHERE ts >= ? GROUP BY feed_rkey ORDER BY 2 DESC", (since_ts,)).fetchall()
    return rows


def top_posts(db, rkey, since_ts, limit, owner_did=None):
    rows = db.execute(
        "SELECT b.uri, b.score, p.text, p.author_handle, p.like_count, p.indexed_at, "
        "       p.author_did "
        "FROM board_scores b JOIN posts p ON p.uri = b.uri "
        "WHERE b.feed_rkey = ? AND b.ts >= ? AND p.text IS NOT NULL "
        "GROUP BY b.uri ORDER BY MAX(b.score) DESC LIMIT ?",
        (rkey, since_ts, max(limit * 6, 30))).fetchall()
    if owner_did:
        # The digest is for the community's posts; the operator can link their own.
        rows = [r for r in rows if r[6] != owner_did]
    return rows[:limit]


def post_url(uri, handle):
    rkey = uri.rsplit("/", 1)[-1]
    who = handle or uri.split("/")[2]
    return f"https://bsky.app/profile/{who}/post/{rkey}"


def shorten(text, width=110):
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if len(clean) <= width:
        return clean
    cut = clean[:width].rsplit(" ", 1)[0]
    return cut + "…"


def compose(cfg, db, since_days=7, top=3, max_boards=3, budget=290, only=None):
    """Fill a post's character budget with the week's best community posts.

    300 graphemes is the hard cap, and a URL costs ~62 of them, so a post can
    hold about three links and only a handful of words each. The budget is spent
    greedily: the highest-scoring post across all boards first, then the next.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp()
    feeds = {f["rkey"]: f for f in (cfg.get("feeds") or []) if f.get("rkey")}
    owner = ((cfg.get("owner") or {}).get("did")
             or (cfg.get("source") or {}).get("did"))

    # Not every board belongs in a digest. On the adult deployment, `bnwo-general`
    # and `bnwo-trending` surfaced mainstream political posts (a Bluesky outage joke,
    # a trans-rights post) because those boards draw from the wider follow graph; a
    # digest built from them put labelled-adult framing on posts that are not adult
    # at all. `digest.feeds` names the boards that represent the deployment.
    pool = []
    used_boards = 0
    wanted = only or ((cfg.get("digest") or {}).get("feeds") or [])
    for rkey, n in boards(db, since):
        if wanted and rkey not in wanted:
            continue
        if not wanted and used_boards >= max_boards:
            break
        if n < 20:
            continue                       # a board with no depth is not a digest
        used_boards += 1
        for row in top_posts(db, rkey, since, top, owner):
            pool.append((rkey, feeds.get(rkey, {}), row))
    if not pool:
        return None

    pool.sort(key=lambda t: -(t[2][4] or 0))
    header = "This week on the feeds:"
    lines = [header]
    links, used = [], len(header)
    for rkey, meta, (uri, _score, text, handle, likes, _ts, _did) in pool:
        url = post_url(uri, handle)
        snippet = shorten(text, 60)
        line = f"· {snippet} ({likes}♥) {url}"
        if used + 1 + len(line) > budget:
            continue
        lines.append(line)
        used += 1 + len(line)
        links.append({"uri": uri, "url": url, "feed": rkey, "likes": likes,
                      "text": snippet})
        if len(links) >= 3:
            break
    if not links:
        return None
    return {"text": "\n".join(lines), "links": links,
            "feeds": sorted({l["feed"] for l in links})}


def check(text):
    """What the PDS will reject, checked before it can."""
    problems = []
    if len(text) > GRAPHEME_CAP:
        problems.append(f"{len(text)} chars > {GRAPHEME_CAP} (post cap)")
    links = re.findall(r"https?://\S+", text)
    if len(links) > 10:
        problems.append(f"{len(links)} links; a post is happier with fewer")
    return problems


def load_env(path):
    """BSKY_HANDLE / BSKY_APP_PASSWORD from a dotfile, never printed."""
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def xrpc(method, payload=None, token=None, host=PDS):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{host}/xrpc/{method}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def facets_for(text):
    """Link facets with BYTE offsets.

    The facet index is into the UTF-8 encoding, not into characters. A digest with an
    emoji or an accent in it (this one has both, by construction) gets every link
    offset wrong if you count characters — the link lands three bytes early and the
    post reads as broken rather than as a link.
    """
    out = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)!?\u2026")
        start = len(text[:m.start()].encode("utf-8"))
        end = start + len(url.encode("utf-8"))
        out.append({"index": {"byteStart": start, "byteEnd": end},
                    "features": [{"$type": "app.bsky.richtext.facet#link",
                                  "uri": url}]})
    return out


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def already_posted(state_path, text, days=6.0):
    """Refuse to post the same digest twice in a week.

    The timer runs weekly, but a failed run gets retried by the next trigger and a
    manual run can happen any time. Posting the same five links twice is the kind of
    thing that makes an account look automated.
    """
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    last = state.get("last") or {}
    if last.get("hash") != text_hash(text):
        return None
    age_days = (datetime.now(timezone.utc).timestamp()
                - float(last.get("ts") or 0)) / 86400.0
    return last if age_days < days else None


def publish(text, env_path, host=PDS, adult=False):
    env = load_env(env_path)
    handle, password = env.get("BSKY_HANDLE"), env.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        sys.exit(f"no BSKY_HANDLE/BSKY_APP_PASSWORD in {env_path}")
    sess = xrpc("com.atproto.server.createSession",
                {"identifier": handle, "password": password}, host=host)
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "facets": facets_for(text),
    }
    if adult:
        # Self-labelling is the account's own declaration; a digest of adult posts
        # should carry it, or the post is a labelled-content leak.
        record["labels"] = {"$type": "com.atproto.label.defs#selfLabels",
                            "values": [{"val": "sexual"}]}
    res = xrpc("com.atproto.repo.createRecord",
               {"repo": sess["did"], "collection": "app.bsky.feed.post",
                "record": record}, token=sess["accessJwt"], host=host)
    rkey = res["uri"].rsplit("/", 1)[-1]
    who = sess.get("handle") or handle
    return {"uri": res["uri"], "cid": res.get("cid"),
            "url": f"https://bsky.app/profile/{who}/post/{rkey}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/.local/share/feedgen/feedgen.sqlite"))
    ap.add_argument("--config", default=os.path.expanduser("~/feedgen/config.json"))
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--max-boards", type=int, default=3)
    ap.add_argument("--feeds", default=None,
                    help="comma-separated board rkeys to digest (default: config digest.feeds)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--publish", action="store_true",
                    help="create the post (needs BSKY_HANDLE / BSKY_APP_PASSWORD)")
    ap.add_argument("--env", default=None,
                    help="dotfile with BSKY_HANDLE / BSKY_APP_PASSWORD")
    ap.add_argument("--state", default=None,
                    help="file recording the last digest posted")
    ap.add_argument("--adult", action="store_true",
                    help="self-label the post sexual")
    ap.add_argument("--force", action="store_true",
                    help="post even if the same digest went out this week")
    ap.add_argument("--host", default=PDS)
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    db = con_ro(args.db)
    only = [f.strip() for f in (args.feeds or "").split(",") if f.strip()] or None
    digest = compose(cfg, db, args.since_days, args.top, args.max_boards, only=only)
    if not digest:
        sys.exit("no board had enough depth to digest")

    problems = check(digest["text"])
    if args.json:
        print(json.dumps({**digest, "problems": problems}, indent=2, ensure_ascii=False))
    else:
        print(digest["text"])
        print()
        print(f"— {len(digest['text'])} chars, {len(digest['links'])} links, "
              f"feeds: {', '.join(digest['feeds'])}")
        for p in problems:
            print(f"  ! {p}")
        if not args.publish:
            print("\ndry run: nothing was posted (--publish to create it)")

    if problems:
        sys.exit(1)
    if not args.publish:
        return 0

    env_path = args.env or os.path.expanduser("~/.env")
    state_path = args.state or os.path.join(
        os.path.dirname(os.path.abspath(args.db)), "digest_state.json")
    if not args.force:
        prior = already_posted(state_path, digest["text"])
        if prior:
            print(f"already posted this digest at {prior.get('at')} "
                  f"({prior.get('url')}); --force to repeat")
            return 0
    posted = publish(digest["text"], env_path, args.host, args.adult)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump({"last": {"hash": text_hash(digest["text"]), "ts": time.time(),
                            "at": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"),
                            "url": posted["url"]}}, fh, indent=1)
    print(f"posted: {posted['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
