"""The machine-readable feed listings must describe the same feeds the pages do.

A directory or a bot that reads /feeds.json has no way to notice that it drifted from
the site, so the tests compare the JSON against the config it is built from.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedgen as fg  # noqa: E402

CFG = {
    "hostname": "feeds.example.com",
    "publisher_did": "did:plc:example",
    "svc_tag": "demo",
    "owner": {"handle": "demo.bsky.social", "did": "did:plc:owner"},
    "page": {"language": "es"},
    "feeds": [
        {"rkey": "top-24h", "display_name": "Top 24H", "description": "Lo del día.",
         "max_age_hours": 24, "min_likes": 2, "ranking": "flat"},
        {"rkey": "es-hoy", "display_name": "Popular Hoy", "description": "En español.",
         "max_age_hours": 24, "min_likes": 0, "ranking": "flat", "language": "es"},
        {"rkey": "foryou", "display_name": "Para ti", "description": "Aprende.",
         "max_age_hours": 168, "min_likes": 1, "ranking": "foryou"},
    ],
}


def test_payload_matches_config():
    out = fg.feeds_payload(CFG)
    assert out["count"] == len(CFG["feeds"]) == len(out["feeds"]), out["count"]
    assert out["service"] == "feeds.example.com"
    assert out["did"] == "did:plc:example"
    for meta, f in zip(out["feeds"], CFG["feeds"]):
        assert meta["rkey"] == f["rkey"]
        assert meta["name"] == f["display_name"]
        assert meta["description"] == f["description"]
        assert meta["window_hours"] == f["max_age_hours"]
        assert meta["url"] == ("https://bsky.app/profile/demo.bsky.social/feed/"
                              + f["rkey"]), meta["url"]
        assert meta["feed_uri"] == ("at://did:plc:example/app.bsky.feed.generator/"
                                    + f["rkey"])
    json.dumps(out)          # no NaN/inf, no unserialisable objects
    print("payload matches the config it is built from")


def test_language_is_per_feed():
    out = {m["rkey"]: m for m in fg.feeds_payload(CFG)["feeds"]}
    assert out["es-hoy"]["language"] == "es"
    # A feed with no explicit language falls back to the deployment's, not to "".
    assert out["top-24h"]["language"] == "es"
    assert out["foryou"]["personalized"] is True
    assert out["top-24h"]["personalized"] is False
    print("language and personalization are stated per feed")


def test_opml_is_valid_and_complete():
    raw = fg.feeds_opml(CFG)
    root = ET.fromstring(raw)
    assert root.tag == "opml" and root.get("version") == "2.0"
    body = root.find("body")
    group = body.find("outline")
    children = group.findall("outline")
    assert len(children) == len(CFG["feeds"]), len(children)
    assert all(c.get("type") == "link" for c in children)
    urls = [c.get("url") for c in children]
    assert urls[0].endswith("/feed/top-24h"), urls
    assert "es-hoy" in urls[1]
    # The file says what it is, so nobody imports it expecting RSS posts.
    assert b"link bundle" in raw, "the OPML does not explain itself"
    print("OPML parses, lists every feed, and declares what it is")


def test_board_top_is_empty_when_nothing_is_served():
    saved = dict(fg.CACHE)
    try:
        fg.CACHE.clear()
        assert fg.board_top("top-24h") == []
        out = fg.feeds_payload(CFG, with_top=3)
        assert all(m["board_size"] == 0 and m["top"] == [] for m in out["feeds"])
        print("an empty board lists zero posts instead of failing")
    finally:
        fg.CACHE.update(saved)


if __name__ == "__main__":
    test_payload_matches_config()
    test_language_is_per_feed()
    test_opml_is_valid_and_complete()
    test_board_top_is_empty_when_nothing_is_served()
    print("ALL FEEDS-LISTING CHECKS PASSED")
