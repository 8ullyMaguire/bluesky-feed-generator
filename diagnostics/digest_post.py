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
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

GRAPHEME_CAP = 300
FACET_MENTION = re.compile(r"@([a-z0-9.-]+)")


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


def compose(cfg, db, since_days=7, top=3, max_boards=3, budget=290):
    """Fill a post's character budget with the week's best community posts.

    300 graphemes is the hard cap, and a URL costs ~62 of them, so a post can
    hold about three links and only a handful of words each. The budget is spent
    greedily: the highest-scoring post across all boards first, then the next.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp()
    feeds = {f["rkey"]: f for f in (cfg.get("feeds") or []) if f.get("rkey")}
    owner = ((cfg.get("owner") or {}).get("did")
             or (cfg.get("source") or {}).get("did"))

    pool = []
    for rkey, n in boards(db, since)[:max_boards]:
        if n < 20:
            continue                       # a board with no depth is not a digest
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/.local/share/feedgen/feedgen.sqlite"))
    ap.add_argument("--config", default=os.path.expanduser("~/feedgen/config.json"))
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--max-boards", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--publish", action="store_true",
                    help="create the post (needs BSKY_HANDLE / BSKY_APP_PASSWORD)")
    ap.add_argument("--feed-rkey", default=None,
                    help="feed to attach to the post as a link card, if any")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    db = con_ro(args.db)
    digest = compose(cfg, db, args.since_days, args.top, args.max_boards)
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
    if args.publish:
        sys.exit("publish path not enabled yet: it needs a session and the operator's "
                 "decision on what to say")


if __name__ == "__main__":
    main()
