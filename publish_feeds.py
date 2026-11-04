#!/usr/bin/env python3
"""Publish (or re-publish) the app.bsky.feed.generator records for every feed
in config.json that is not already on-chain.

Idempotent: an existing record with the same displayName/description is left
alone unless --force is passed. Prints, per feed, what it did.

Usage:
  python3 publish_feeds.py --dry-run
  python3 publish_feeds.py
  python3 publish_feeds.py --force
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path):
    vals = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return vals


CFG_PATH = (os.environ.get("FEEDGEN_CONFIG")
            or os.path.join(BASE, "config.json"))
CFG = json.load(open(CFG_PATH))
# The deployment's own .env must win: the script lives in the shared engine, so
# a steeltitan run that picked up ~/feedgen/.env would publish with the wrong
# account (and be rejected as a foreign repo, after confusing the operator).
ENV = {}
for p in (os.path.join(os.path.dirname(os.path.abspath(CFG_PATH)), ".env"),
          os.path.join(BASE, ".env"), os.path.expanduser("~/feedgen/.env"),
          os.path.expanduser("~/.hermes/.env")):
    # dict | dict keeps the LEFT operand's keys: the first file loaded wins, so
    # the deployment's own .env is first.
    ENV = load_dotenv(p) | ENV
ENV |= dict(os.environ)

def resolve_pds(did):
    """The account's real PDS endpoint, or None.

    Writes must go to the PDS that actually hosts the repo: steeltitan lives on
    a separate bsky.network shard, and posting its records to the config's
    default host fails with `AccountNotFound` even though reads work.
    """
    try:
        with urllib.request.urlopen(f"https://plc.directory/{did}", timeout=20) as r:
            doc = json.load(r)
        for svc in doc.get("service") or []:
            if svc.get("id") in ("#atproto_pds", "atproto_pds"):
                return (svc.get("serviceEndpoint") or "").rstrip("/") or None
    except Exception as e:                            # noqa: BLE001
        print(f"[publish] could not resolve the PDS for {did}: {e}")
    return None


PDS = CFG["source"]["pds_host"]  # rebound below, after REPO is known
APPVIEW = CFG["source"]["appview_host"]
REPO = CFG["publisher_did"]
PDS = resolve_pds(REPO) or PDS
SERVICE_DID = f"did:web:{CFG['hostname']}"


def rpc(host, method, data=None, token=None, timeout=45):
    req = urllib.request.Request(
        f"{host}/xrpc/{method}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "feedgen-publish/1.0"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} HTTP {e.code}: {e.read().decode()[:300]}")


def existing(token):
    out = {}
    try:
        q = urllib.parse.urlencode({"repo": REPO,
                                    "collection": "app.bsky.feed.generator",
                                    "limit": 100})
        d = rpc(PDS, f"com.atproto.repo.listRecords?{q}", token=token)
        for r in d.get("records", []):
            out[r["uri"].rsplit("/", 1)[-1]] = {
                "uri": r.get("uri"), "cid": r.get("cid"),
                "value": r.get("value") or {}}
    except Exception as e:
        print(f"[publish] could not list existing records: {e}")
    return out


def main(argv):
    dry = "--dry-run" in argv
    force = "--force" in argv
    handle, password = ENV.get("BSKY_HANDLE"), ENV.get("BSKY_APP_PASSWORD")
    if not handle or not password:
        print("Missing BSKY_HANDLE/BSKY_APP_PASSWORD", file=sys.stderr)
        return 2
    sess = rpc(PDS, "com.atproto.server.createSession",
               {"identifier": handle, "password": password})
    token = sess["accessJwt"]
    have = existing(token)
    print(f"[publish] on-chain records found: {sorted(have)}")

    changed = 0
    for f in CFG["feeds"]:
        rkey = f["rkey"]
        if len(f["description"]) > 300:
            print(f"ABORT: {rkey} description is {len(f['description'])} codepoints; "
                  f"the PDS caps generator descriptions at 300 graphemes",
                  file=sys.stderr)
            return 2
        cur = have.get(rkey) or {}
        cur_val = dict(cur.get("value") or {})
        want = cur_val | {
            "$type": "app.bsky.feed.generator",
            "did": SERVICE_DID,
            "displayName": f["display_name"],
            "description": f["description"][:3000],
            # Declared here, not only in describeFeedGenerator: the client reads
            # the RECORD to decide whether to offer "show more/less like this".
            "acceptsInteractions": True,
            "createdAt": cur_val.get("createdAt")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if cur_val and not force \
                and cur_val.get("displayName") == want["displayName"] \
                and cur_val.get("description") == want["description"] \
                and cur_val.get("acceptsInteractions") is True:
            print(f"  = {rkey:<18} unchanged")
            continue
        if dry:
            print(f"  DRY {'update' if cur_val else 'create'} {rkey:<18} "
                  f"{f['display_name']!r}")
            continue
        body = {"repo": REPO, "collection": "app.bsky.feed.generator",
                "rkey": rkey, "record": want}
        if cur.get("cid"):
            body["swapRecord"] = cur["cid"]
        try:
            rpc(PDS, "com.atproto.repo.putRecord", body, token)
            changed += 1
            print(f"  + {rkey:<18} {'updated' if cur else 'created'} "
                  f"{f['display_name']!r}")
        except RuntimeError as e:
            print(f"  ERR {rkey}: {e}")
        time.sleep(0.2)

    print(f"[publish] done: {changed} written, dry_run={dry}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
