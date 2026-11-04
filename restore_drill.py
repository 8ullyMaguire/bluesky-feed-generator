#!/usr/bin/env python3
"""Restore drill: prove the backup is bootable, not just that it exists.

  python3 restore_drill.py --config PATH --db PATH [--port 8199] [--keep]

Steps, all against a throwaway copy:
  1. `sqlite3 .backup` the live database (consistent while the service writes).
  2. PRAGMA integrity_check, and compare table counts against the source.
  3. Boot a second engine process on a spare port against the copy.
  4. Ask it for /.well-known/did.json and describeFeedGenerator.
  5. Ask for one feed skeleton (empty is fine while it warms: the point is
     that the database opened and the request path works).
  6. Stop it, report what happened, and delete the copy.

The copy is written next to the database by default, never in /tmp: this host's
/tmp is a 4 GB tmpfs and a 2 GB database does not fit beside its source there.
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = []


def backup(src, dst):
    """Consistent hot copy: .backup, never cp."""
    con = sqlite3.connect(src, timeout=30)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    r = subprocess.run(["sqlite3", src, f".backup '{dst}'"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"backup failed: {r.stderr.strip()}")


def counts(path, tables=("posts", "served_posts", "interaction_events", "seen"),
           full=False):
    con = sqlite3.connect(path)
    out = {}
    for t in tables:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    pragma = "integrity_check" if full else "quick_check"
    out["integrity"] = con.execute(f"PRAGMA {pragma}").fetchone()[0]
    con.close()
    return out


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.environ.get("FEEDGEN_CONFIG"))
    ap.add_argument("--db", default=os.environ.get("FEEDGEN_DB"))
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--full-check", action="store_true",
                    help="run PRAGMA integrity_check instead of quick_check "
                         "(slow on a multi-GB file)")
    ap.add_argument("--workdir", default=None,
                    help="where to put the copy (default: next to the database)")
    a = ap.parse_args()
    if not a.config or not a.db:
        sys.exit("need --config and --db (or FEEDGEN_CONFIG / FEEDGEN_DB)")
    if not os.path.exists(a.db):
        sys.exit(f"database not found: {a.db}")

    scratch_dir = a.workdir or os.path.dirname(os.path.abspath(a.db))
    tmp = tempfile.mkdtemp(prefix=".fg-drill-", dir=scratch_dir)
    if not a.keep:
        SCRATCH.append(tmp)
    copy = os.path.join(tmp, "copy.sqlite")
    print(f"[drill] source {a.db} ({os.path.getsize(a.db) // 1048576} MB)")

    t0 = time.time()
    backup(a.db, copy)
    print(f"[drill] backup written in {time.time() - t0:.1f}s "
          f"({os.path.getsize(copy) // 1048576} MB)")

    src_c, dst_c = counts(a.db, full=a.full_check), counts(copy, full=a.full_check)
    if dst_c["integrity"] != "ok":
        sys.exit(f"[drill] integrity_check: {dst_c['integrity']}")
    for t in ("posts", "served_posts", "interaction_events"):
        if src_c.get(t) is not None and dst_c.get(t) != src_c[t]:
            sys.exit(f"[drill] {t} count mismatch: {src_c[t]} vs {dst_c[t]}")
    print("[drill] integrity ok, counts match: "
          + ", ".join(f"{k}={v}" for k, v in dst_c.items() if k != "integrity"))

    cfg = json.load(open(a.config))
    cfg["port"] = a.port
    cfg["feeds"] = cfg["feeds"][:1]
    cfg_path = os.path.join(tmp, "config.json")
    json.dump(cfg, open(cfg_path, "w"))

    env = dict(os.environ, FEEDGEN_CONFIG=cfg_path, FEEDGEN_DB=copy)
    # The child logs on every phase; give it a file, never a pipe. An unread
    # pipe fills at 64 KB and then the child blocks, which makes the drill look
    # like a slow database instead of a full buffer.
    log_path = os.path.join(tmp, "drill-engine.log")
    log_fh = open(log_path, "w")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "feedgen.py")],
                            env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                            text=True)
    base = f"http://127.0.0.1:{a.port}"
    feeds = []
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                log_fh.flush()
                sys.exit("[drill] engine exited early:\n"
                         + open(log_path).read()[-2000:])
            try:
                st, body = get(f"{base}/xrpc/app.bsky.feed.describeFeedGenerator")
                if st == 200:
                    feeds = json.loads(body).get("feeds") or []
                    print(f"[drill] describeFeedGenerator ok, {len(feeds)} feed(s)")
                    break
            except Exception:
                time.sleep(1)
        else:
            sys.exit("[drill] engine never answered describeFeedGenerator")

        st, body = get(f"{base}/.well-known/did.json")
        if st != 200 or b"did:web" not in body:
            sys.exit(f"[drill] did.json bad: {st} {body[:120]!r}")
        print("[drill] did.json ok")

        uri = feeds[0]["uri"]
        q = urllib.parse.urlencode({"feed": uri, "limit": 10})
        st, body = get(f"{base}/xrpc/app.bsky.feed.getFeedSkeleton?{q}")
        d = json.loads(body)
        if st != 200 or not isinstance(d.get("feed"), list):
            sys.exit(f"[drill] skeleton bad: {st} {d}")
        print(f"[drill] getFeedSkeleton ok ({len(d['feed'])} posts; "
              f"0 is fine while warming)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        if a.keep:
            print(f"[drill] kept {tmp}")
    print(f"[drill] PASSED in {time.time() - t0:.1f}s")


def _cleanup(*_a):
    for d in SCRATCH:
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)
    try:
        main()
    finally:
        for d in SCRATCH:
            shutil.rmtree(d, ignore_errors=True)
