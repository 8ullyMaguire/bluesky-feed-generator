"""Is the Spanish pool actually as small as our feeds make it look?

Two measurements, both read-only:
  1. how many of the current es-hoy board's authors are members of the curate list
  2. what a broad Spanish search (sort=top, lang=es) returns outside that list —
     engagement distribution, so "the pool is thin" can be separated from
     "our list is thin".
Credentials come from ~/feedgen/.env inside this process; nothing is printed.
"""
"""Measure the pool a Spanish board is actually built from.

Answers two questions at once: how much of the board comes from outside the
curate list, and what the language-wide popular pool looks like next to it. Run
it before and after changing a source, or when someone says the board "feels
stale" — the numbers say whether the population or the ranking is at fault.

  python3 diagnostics/es_pool.py [--config path] [--env path] [--feed rkey]
"""
import argparse
import json
import os
import statistics
import urllib.parse
import urllib.request

ENV_PATH = os.path.expanduser("~/feedgen/.env")
CFG_PATH = "/mnt/disk-important/personal/documents/code/misc/feedgen/config.json"


def env(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def rpc(host, method, params, token=None, timeout=45):
    url = "%s/xrpc/%s?%s" % (host.rstrip("/"), method, urllib.parse.urlencode(params, doseq=True))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=ENV_PATH)
    ap.add_argument("--config", default=CFG_PATH)
    ap.add_argument("--feed", default="leftist-es-hoy")
    a = ap.parse_args(argv)
    E = env(a.env)
    cfg = json.load(open(a.config, encoding="utf-8"))
    owner = cfg["owner"]["did"]

    sess = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        data=json.dumps({"identifier": E["BSKY_HANDLE"], "password": E["BSKY_APP_PASSWORD"]}).encode(),
        headers={"Content-Type": "application/json"}), timeout=30))
    token, did = sess["accessJwt"], sess["did"]

    # 1. curate list membership
    list_uri = cfg["source"]["list_uri"]
    members, cursor = set(), None
    while True:
        p = {"list": list_uri, "limit": 100}
        if cursor:
            p["cursor"] = cursor
        d = rpc("https://public.api.bsky.app", "app.bsky.graph.getList", p)
        members |= {i["subject"]["did"] for i in d.get("items") or []}
        cursor = d.get("cursor")
        if not cursor or not d.get("items"):
            break
    print("curate list members: %d" % len(members))

    # 2. what the es-hoy board is built from
    board, cur = [], None
    while len(board) < 400:
        p = {"feed": "at://%s/app.bsky.feed.generator/%s" % (cfg["publisher_did"], a.feed), "limit": 100}
        if cur:
            p["cursor"] = cur
        d = rpc("https://public.api.bsky.app", "app.bsky.feed.getFeed", p)
        board += d.get("feed") or []
        cur = d.get("cursor")
        if not cur or not d.get("feed"):
            break
    likes = [f["post"].get("likeCount", 0) for f in board]
    authors = {(f["post"]["author"]["did"]) for f in board}
    outside = authors - members - {owner}
    print("%s board: %d posts, %d authors (%d outside the list), "
          "median likes %.0f, p90 %.0f, max %d, >=5 likes: %d%%"
          % (a.feed, len(board), len(authors), len(outside),
             statistics.median(likes) if likes else 0,
             statistics.quantiles(likes, n=10)[8] if len(likes) > 10 else 0,
             max(likes) if likes else 0,
             100 * sum(1 for x in likes if x >= 5) / max(1, len(likes))))

    # 3. the pool the list is not showing us: broad Spanish, sorted by engagement
    terms = ["política", "españa", "elecciones", "huelga", "vivienda", "sanidad"]
    seen, all_likes, new_authors = set(), [], set()
    for term in terms:
        try:
            d = rpc("https://bsky.social", "app.bsky.feed.searchPosts",
                    {"q": term, "limit": 100, "sort": "top", "lang": "es"}, token=token)
        except Exception as exc:                        # noqa: BLE001
            print("  search %-12s failed: %s" % (term, exc))
            continue
        posts = d.get("posts") or []
        fresh = 0
        for p in posts:
            u = p["uri"]
            if u in seen:
                continue
            seen.add(u)
            all_likes.append(p.get("likeCount", 0))
            adid = (p.get("author") or {}).get("did")
            if adid and adid not in members:
                new_authors.add(adid)
                fresh += 1
        print("  search %-12s %3d posts, %3d outside the list, median likes %.0f, max %d"
              % (term, len(posts), fresh,
                 statistics.median([p.get("likeCount", 0) for p in posts]) if posts else 0,
                 max([p.get("likeCount", 0) for p in posts] or [0])))
    if all_likes:
        print("\nbroad Spanish 'top' pool: %d posts, %d authors outside the list, "
              "median likes %.0f, >=5 likes: %d%%, >=50: %d%%"
              % (len(all_likes), len(new_authors), statistics.median(all_likes),
                 100 * sum(1 for x in all_likes if x >= 5) / len(all_likes),
                 100 * sum(1 for x in all_likes if x >= 50) / len(all_likes)))


if __name__ == "__main__":
    main()
