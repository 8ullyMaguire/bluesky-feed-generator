#!/usr/bin/env python3
"""Create a starter pack that bundles the leftist feeds (no accounts).

A starter pack is the one distribution artefact that is a single URL: it carries the
feeds a new reader should subscribe to, and unlike a post it does not scroll away. The
record needs a list (accounts) even when there are none, so this creates an empty
curation list and points the pack at it.

    python3 make_starter_pack.py --dry-run
    python3 make_starter_pack.py
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

PDS = "https://bsky.social"
ENV = os.path.expanduser("~/feedgen/.env")
HANDLE = "criticalnexus.bsky.social"

# A starter pack holds at most three feeds (the PDS rejects more: "array too big
# (maximum 3, got 5)"). Three is also the right editorial number: one daily in each
# language and one weekly, rather than a menu of five overlapping boards.
FEEDS = [
    ("Leftist Top 24H", "leftist-24h"),
    ("Popular Hoy (español)", "leftist-es-hoy"),
    ("Leftist Top Semanal", "leftist-week"),
]
NAME = "Feeds izquierdistas"
DESCRIPTION = ("Los feeds de la lista Leftist: lo más popular de 24 horas y de la semana, "
               "en inglés y en español. Leftist list feeds: top of the day, top of the "
               "week, in English and in Spanish.")


def load_env(path):
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def xrpc(method, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{PDS}/xrpc/{method}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"xrpc {method} -> {e.code}: {e.read().decode()[:400]}", file=sys.stderr)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env", default=ENV)
    args = ap.parse_args()

    env = load_env(args.env)
    sess = xrpc("com.atproto.server.createSession",
                {"identifier": env["BSKY_HANDLE"], "password": env["BSKY_APP_PASSWORD"]})
    did, token = sess["did"], sess["accessJwt"]

    existing = xrpc("com.atproto.repo.listRecords?repo=" + did +
                    "&collection=app.bsky.graph.starterpack&limit=10")
    if existing.get("records"):
        for r in existing["records"]:
            print(f"already have a starter pack: {r['uri']} "
                  f"({r['value'].get('name')}) — not creating another")
        return 0

    feed_refs = [{"uri": f"at://{did}/app.bsky.feed.generator/{rkey}"}
                 for _name, rkey in FEEDS]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    pack = {"$type": "app.bsky.graph.starterpack", "name": NAME,
            "description": DESCRIPTION, "feeds": feed_refs, "createdAt": now}

    if args.dry_run:
        print(json.dumps(pack, indent=1, ensure_ascii=False))
        return 0

    lists = xrpc("com.atproto.repo.listRecords?repo=" + did +
                 "&collection=app.bsky.graph.list&limit=50").get("records") or []
    mine = [r for r in lists if (r.get("value") or {}).get("name") == NAME]
    if mine:
        print("reusing list", mine[0]["uri"])
        lst = {"uri": mine[0]["uri"]}
    else:
        lst = xrpc("com.atproto.repo.createRecord",
               {"repo": did, "collection": "app.bsky.graph.list",
                "record": {"$type": "app.bsky.graph.list",
                           "purpose": "app.bsky.graph.defs#curatelist",
                           "name": NAME,
                           "description": DESCRIPTION,
                           "createdAt": now}}, token=token)
    pack["list"] = lst["uri"]
    res = xrpc("com.atproto.repo.createRecord",
               {"repo": did, "collection": "app.bsky.graph.starterpack",
                "record": pack}, token=token)
    rkey = res["uri"].rsplit("/", 1)[-1]
    print("starter pack:", res["uri"])
    print("list:", lst["uri"])
    print("url:", f"https://bsky.app/starter-pack/{sess.get('handle') or HANDLE}/{rkey}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
