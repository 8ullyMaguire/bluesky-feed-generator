#!/usr/bin/env python3
"""Tests for the public explainer page (GET /) and the operator page (/status, /health).

Regression guards:
  * the feed API (/xrpc/...) must keep working exactly as before this page existed;
  * the public page must stay GENERIC -- no value read out of the config may reach
    it (thresholds, ranking mode names, windows, dedup TTLs, board sizes, scoring
    weights). The published descriptions advertised "one third of likers must
    repost" for weeks after the gate was relaxed to 5-10%: numbers in public copy
    drift, so the page carries prose and the config keeps the knobs.

Run: python3 test_page.py
Uses a throwaway SQLite file, so the real feedgen.sqlite is untouched.
"""
import importlib.util
import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request

TMP = tempfile.mkdtemp(prefix="feedgen-page-test-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")
os.environ["FEEDGEN_STATE_PATH"] = os.path.join(TMP, "no-legacy.json")

HERE = os.path.dirname(os.path.abspath(__file__))
if not os.environ.get("FEEDGEN_CONFIG"):
    # Runnable on a fresh clone: prefer a local config.json, else the example.
    os.environ["FEEDGEN_CONFIG"] = os.path.join(
        HERE, "config.json" if os.path.exists(os.path.join(HERE, "config.json"))
        else "config.example.json")
_spec = importlib.util.spec_from_file_location("fg", os.path.join(HERE, "feedgen.py"))
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)  # type: ignore[union-attr]

# A minimal synthetic deployment: distinctive config values (9876, 0.654321, 4321,
# 777, 555, velocityx, 12345) that must never appear in the rendered page, plus
# generic copy that must.
SYNTH = {
    "hostname": "feeds.example.test",
    "publisher_did": "did:plc:examplepublisher000000",
    "svc_tag": "example",
    "refresh_secs": 12345,
    "owner": {"handle": "curator.example.test",
              "did": "did:plc:owner0000000000000000"},
    "blocked_dids": ["did:plc:blockedsecret000000000"],
    "feeds": [{
        "rkey": "example-4h",
        "display_name": "Example Top 4H",
        "description": "The last few hours, ranked.",
        "max_age_hours": 4, "min_age_hours": 0,
        "min_likes": 9876, "min_score": 4321, "min_reposts_share": 0.654321,
        "max_per_author": 555, "max_posts": 4321, "adaptive_target": 99,
        "hide_seen": 1, "seen_ttl_hours": 777,
        "ranking": "velocityx", "gravity": 0.9, "taste_weight": 0.3,
        "topic_weight": 2.5, "topic_keyword_bonus": 0.2,
    }],
}
CONFIG_LEAKS = ("9876", "4321", "555", "777", "0.654321", "velocityx", "12345",
                "min_likes", "min_reposts_share", "seen_ttl", "adaptive_target",
                "max_per_author", "topic_weight", "gravity", "hide_seen")


def _clone(cfg):
    return json.loads(json.dumps(cfg))


def test_page_lists_every_feed_of_this_deployment():
    doc = fg.render_index_page(fg.CFG).decode()
    assert doc.startswith("<!doctype html>"), doc[:40]
    for f in fg.CFG["feeds"]:
        assert f'id="{f["rkey"]}"' in doc, f["rkey"]
        assert f"/feed/{f['rkey']}" in doc, f["rkey"]
    assert fg.CFG["owner"]["handle"] in doc
    print(f"ok  index page: all {len(fg.CFG['feeds'])} feeds listed, each with an open-in-Bluesky link")


def test_page_publishes_no_config_specifics():
    """The owner's requirement: generic information only."""
    doc = fg.render_index_page(SYNTH).decode()
    leaked = [tok for tok in CONFIG_LEAKS if tok in doc]
    assert not leaked, f"config values leaked onto the public page: {leaked}"
    # and the generic copy really is there
    assert "The last few hours, ranked." in doc
    assert "One hand-maintained list of accounts" in doc
    print("ok  index page: no config value (gate, mode, window, TTL, size) reaches the page")


def test_tagline_falls_back_to_the_published_description():
    doc = fg.render_index_page(SYNTH).decode()
    assert "The last few hours, ranked." in doc
    print("ok  index page: no page block -> the published description becomes the tagline")


def test_description_is_not_printed_twice():
    doc = fg.render_index_page(SYNTH).decode()
    assert doc.count("The last few hours, ranked.") == 1, doc.count("The last few hours, ranked.")
    print("ok  index page: the description is never printed twice")


def test_show_descriptions_false_hides_the_published_description():
    cfg = _clone(SYNTH)
    cfg["feeds"][0]["page"] = {"tagline": "A curated tagline."}
    cfg["page"] = {"show_descriptions": True}
    doc = fg.render_index_page(cfg).decode()
    assert "A curated tagline." in doc and "The last few hours, ranked." in doc
    cfg["page"] = {"show_descriptions": False}
    doc = fg.render_index_page(cfg).decode()
    assert "A curated tagline." in doc and "The last few hours, ranked." not in doc
    print("ok  index page: show_descriptions=false keeps the tagline, drops the published description")


def test_labels_are_overridable():
    cfg = _clone(SYNTH)
    cfg["page"] = {"labels": {"use_when": "USALO CUANDO", "open": "ABRIR"}}
    cfg["feeds"][0]["page"] = {"use_when": "estas aburrido"}
    doc = fg.render_index_page(cfg).decode()
    assert "USALO CUANDO" in doc and "ABRIR" in doc and "estas aburrido" in doc
    assert "Use it when" not in doc and "Open in Bluesky" not in doc
    print("ok  index page: labels come from the deployment's config")


def test_page_escapes_copy_and_publishes_no_dids():
    cfg = _clone(SYNTH)
    cfg["feeds"][0]["display_name"] = "<script>alert(1)</script>"
    cfg["feeds"][0]["page"] = {"tagline": "<b>bold</b>", "use_when": "<i>x</i>"}
    cfg["page"] = {"tagline": "<img src=x>", "faq": [{"q": "<q>", "a": "<a>"}],
                   "community_feeds": [{"name": "<c>", "handle": "c.test", "rkey": "r"}]}
    doc = fg.render_index_page(cfg).decode()
    assert "<script>alert(1)</script>" not in doc and "&lt;script&gt;" in doc
    assert "<b>bold</b>" not in doc and "&lt;b&gt;bold&lt;/b&gt;" in doc
    assert "<img src=x>" not in doc
    assert "did:plc:blockedsecret000000000" not in doc
    assert "did:plc:owner0000000000000000" not in doc
    print("ok  index page: config text is escaped and no DID is published")


def test_faq_and_community_feeds_render():
    cfg = _clone(SYNTH)
    cfg["page"] = {
        "faq": [{"q": "Why no likes?",
                 "a": "The account that runs the feeds always appears."}],
        "community_feeds": [{"name": "Neighbour feed", "handle": "n.test", "rkey": "aaa"}],
    }
    doc = fg.render_index_page(cfg).decode()
    assert "Why no likes?" in doc
    assert "The account that runs the feeds always appears." in doc
    assert "https://bsky.app/profile/n.test/feed/aaa" in doc
    print("ok  index page: FAQ and community feeds render")


def test_github_star_cta():
    """The GitHub link is a star request, not a bare visit."""
    doc = fg.render_index_page(SYNTH).decode()
    assert "<svg" not in doc, "no github configured -> no icon at all"
    cfg = _clone(SYNTH)
    cfg["page"] = {"github": "https://github.com/owner/repo"}
    doc = fg.render_index_page(cfg).decode()
    assert 'href="https://github.com/owner/repo"' in doc
    assert "<svg" in doc and "Star on GitHub" in doc
    assert 'rel="noopener"' in doc and 'target="_blank"' in doc
    cfg["page"] = {"github": "https://github.com/owner/repo",
                   "labels": {"star_link": "Dale una estrella"}}
    doc = fg.render_index_page(cfg).decode()
    assert "Dale una estrella" in doc and "Star on GitHub" not in doc
    print("ok  index page: the GitHub link is a star request (label overridable)")


def test_status_page_still_renders():
    snap = {"feeds": {"example-4h": 12},
            "stats": {"example-4h": {"in_window": 1, "pass_share": 2, "pass_gates": 3,
                                     "suppressed": 4, "owner": 5, "final": 12, "ttl": 3}},
            "updated": 0, "scanned": 0, "error": None, "gen": 1, "state_uris": 0,
            "taste_authors": 0, "topic_authors": 0, "fetch": {}}
    doc = fg.render_status_page(snap, SYNTH).decode()
    assert "per-feed pipeline" in doc and "example-4h" in doc
    print("ok  status page: the operator view still renders")


def test_http_routes():
    with fg.CACHE_LOCK:
        fg.CACHE["feeds"] = {"example-4h": ["at://did:plc:a/app.bsky.feed.post/1"]}
        fg.CACHE["gen"] = 1
    srv = fg.HTTPServer(("127.0.0.1", 0), fg.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for path, needle in (("/", b"<!doctype html>"),
                             ("/status", b"per-feed pipeline"),
                             ("/health", b"per-feed pipeline")):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                body = r.read()
            assert r.status == 200, (path, r.status)
            assert needle in body, (path, needle)
            print(f"ok  GET {path}: HTTP 200")
        with urllib.request.urlopen(base + "/xrpc/app.bsky.feed.describeFeedGenerator",
                                    timeout=10) as r:
            d = json.load(r)
        assert len(d["feeds"]) == len(fg.CFG["feeds"]), d
        uri = urllib.parse.quote(next(iter(fg.FEEDS)))
        with urllib.request.urlopen(f"{base}/xrpc/app.bsky.feed.getFeedSkeleton"
                                    f"?feed={uri}&limit=5", timeout=10) as r:
            skel = json.load(r)
        assert r.status == 200 and "feed" in skel, (r.status, skel)
        print(f"ok  describeFeedGenerator: {len(d['feeds'])} feeds; getFeedSkeleton: HTTP 200")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL PAGE TESTS PASSED")
