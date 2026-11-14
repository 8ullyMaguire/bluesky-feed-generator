#!/usr/bin/env python3
"""Watch the feed estate for the failures that look like success.

Every failure mode this checks for has happened at least once on this host: a service
that exited (the unit said "activating"), a cycle that stopped advancing (the process
alive, the corpus frozen), and a supply-bound board that quietly sits at half its cap
after a restart. All three look like a healthy deployment from the outside: the port
answers, /healthz returns 200, and a board endpoint returns posts.

Two rules, learned the hard way:

* Self-heal before paging. A dead unit is restarted here, and the restart is recorded.
  A page that only says "it is down" when it could have been up is a worse page.
* Absence of a sink must not look like health. With no ESTATE_NTFY_URL or
  ESTATE_ALERT_EMAIL the findings still land in `watchdog.json` and `alerts.jsonl`,
  and the weekly report reads them. `--strict` exits 1 so a timer can be noticed by
  systemd as failed.

    python3 diagnostics/watchdog.py --db ... --config ... --deploy ~/feedgen
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

STALE_CYCLE_SECS = 20 * 60        # a cycle is ~2-4 min; 20 min means stopped
SHORT_BOARD_SHARE = 0.8           # a board below 80% of cap is short
SHORT_CYCLES = 3                  # ... consistently, not once
NOTIFY = "/mnt/disk-important/personal/documents/code/estate/scripts/notify.py"


def now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(
        timespec="seconds")


def unit_state(unit):
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit],
                             capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr or "").strip() or "unknown"
    except Exception as exc:                                  # noqa: BLE001
        return f"unreachable: {exc}"


def restart_unit(unit):
    try:
        subprocess.run(["systemctl", "--user", "restart", unit],
                       capture_output=True, text=True, timeout=90)
        return unit_state(unit)
    except Exception as exc:                                  # noqa: BLE001
        return f"restart failed: {exc}"


def cycle_age(db, now=None):
    """Seconds since the newest metrics row, or None when there is no history."""
    now = now or time.time()
    try:
        row = db.execute("SELECT MAX(ts) FROM metrics_log").fetchone()
    except sqlite3.Error:
        # A database that has not been migrated yet is not a stale cycle; it is a
        # different fact, and reporting it as staleness would page about the wrong
        # thing. The caller treats None as "cannot say".
        return None
    if not row or row[0] is None:
        return None
    return max(0.0, now - float(row[0]))


def board_depth(db, cfg, now=None):
    """Per feed: the served board size against the feed's cap, latest snapshot only.

    Uses `board_scores` rather than the live pages, because the question is what the
    ranker last produced, not what one reader was served.
    """
    out = {}
    for f in cfg.get("feeds") or []:
        rkey, cap = f.get("rkey"), int(f.get("max_posts") or 0)
        if not rkey or not cap:
            continue
        try:
            row = db.execute(
                "SELECT COUNT(DISTINCT uri) FROM board_scores "
                "WHERE feed_rkey=? AND ts=(SELECT MAX(ts) FROM board_scores "
                "                          WHERE feed_rkey=?)", (rkey, rkey)).fetchone()
        except sqlite3.Error:
            return {}                 # no board table yet: nothing to judge
        n = int(row[0] or 0)
        # A curated board (the operator's own liked posts, chronological) cannot be
        # filled by ranking: its cap is aspirational. Counting it as a supply fault
        # would page every ten minutes about a board that is behaving as designed.
        curated = bool(f.get("require_owner_liked")) or f.get("ranking") == "chrono"
        out[rkey] = {"cap": cap, "served": n, "share": round(n / cap, 3),
                     "curated": curated}
    return out


def classify(state, depth, history):
    """Turn observations into findings. Pure, so the rules can be tested.

    `history` is the previous findings list for the same host (newest first), used to
    require a short board to be short *repeatedly* before it is called a fault.
    """
    findings = []
    if state != "active":
        findings.append({"kind": "unit_not_active", "detail": state, "page": True})
    short_prev = {}
    for h in history[:SHORT_CYCLES * 4]:
        for f in h.get("findings") or []:
            if f.get("kind") == "board_short":
                short_prev[f["feed"]] = short_prev.get(f["feed"], 0) + 1
    for rkey, d in (depth or {}).items():
        if d["share"] < SHORT_BOARD_SHARE:
            findings.append({
                "kind": "board_short_curated" if d.get("curated") else "board_short",
                "feed": rkey, "detail": f"{d['served']}/{d['cap']}",
                "page": (not d.get("curated")
                         and short_prev.get(rkey, 0) + 1 >= SHORT_CYCLES)})
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--deploy", default=None, help="deployment dir (holds the state files)")
    ap.add_argument("--unit", default="feedgen")
    ap.add_argument("--state", default=None)
    ap.add_argument("--history", type=int, default=24)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when a paging finding is present")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-sink", action="store_true",
                    help="do not try notify.py even if a sink is configured")
    args = ap.parse_args()

    deploy = args.deploy or os.getcwd()
    state_path = args.state or os.path.join(deploy, "diagnostics", "watchdog.json")
    alerts_path = os.path.join(deploy, "diagnostics", "alerts.jsonl")
    cfg = json.load(open(args.config))
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) if os.path.exists(args.db) \
        else None

    history = []
    if os.path.exists(state_path):
        try:
            history = json.load(open(state_path)).get("history") or []
        except (json.JSONDecodeError, OSError):
            history = []

    state = unit_state(args.unit)
    healed = None
    if state != "active" and not args.no_restart:
        healed = restart_unit(args.unit)
        state = healed

    age = cycle_age(db) if db else None
    depth = board_depth(db, cfg) if db else {}
    findings = classify(state, depth, history)
    if healed:
        findings.append({"kind": "unit_restarted", "detail": healed, "page": True})
    if age is not None and age > STALE_CYCLE_SECS:
        findings.append({"kind": "cycle_stale", "detail": f"{age / 60:.0f} min",
                         "page": True})
    if age is None and db is not None:
        findings.append({"kind": "no_cycles", "detail": "metrics_log empty",
                         "page": True})

    record = {"ts": now_iso(), "host": os.uname().nodename, "unit": args.unit,
              "unit_state": state, "cycle_age_secs": None if age is None else round(age),
              "findings": findings,
              "sink": bool(os.environ.get("ESTATE_NTFY_URL")
                           or os.environ.get("ESTATE_ALERT_EMAIL"))}
    history.insert(0, {"ts": record["ts"], "findings": findings})
    history = history[:args.history]

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"updated": record["ts"], "last": record, "history": history},
                  fh, indent=1)
    os.replace(tmp, state_path)

    paging = [f for f in findings if f.get("page")]
    if paging:
        with open(alerts_path, "a") as fh:
            fh.write(json.dumps({"ts": record["ts"], "findings": paging}) + "\n")
        if not args.no_sink and os.path.exists(NOTIFY) and record["sink"]:
            for f in paging:
                subprocess.run([sys.executable, NOTIFY, "--rule", "feeds_watchdog",
                                "--target", record["host"],
                                "--because", f"{f['kind']}: {f.get('feed', '')} "
                                             f"{f.get('detail', '')}".strip(),
                                "--command", f"python3 {os.path.abspath(__file__)} "
                                             f"--db {args.db} --config {args.config}"],
                               check=False, timeout=60)

    if args.json:
        print(json.dumps(record, indent=1))
    else:
        print(f"{record['ts']} {args.unit}={state} cycle_age="
              f"{'?' if age is None else f'{age / 60:.0f}m'} "
              f"findings={len(findings)} paging={len(paging)} "
              f"sink={'yes' if record['sink'] else 'none (findings recorded locally)'}")
        for f in findings:
            print(f"  - {f['kind']}: {f.get('feed', '')} {f.get('detail', '')}".rstrip())
        short = {k: v for k, v in depth.items() if v["share"] < SHORT_BOARD_SHARE}
        if short:
            print("  short boards: " + ", ".join(
                f"{k} {v['served']}/{v['cap']}"
                + (" (curated)" if v.get("curated") else "")
                for k, v in sorted(short.items())))

    return 1 if (args.strict and paging) else 0


if __name__ == "__main__":
    sys.exit(main())
