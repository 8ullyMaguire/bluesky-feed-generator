"""Checks for the /guide page: structure from config, escaping, and that each
deployment actually ships a guide long enough to be useful."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedgen as fg  # noqa: E402

SYNTH = {
    "svc_tag": "synth",
    "hostname": "feeds.example.com",
    "publisher_did": "did:plc:example",
    "feeds": [{"rkey": "now", "display_name": "Now", "description": "d"}],
    "page": {"guide": {
        "title": "Guide",
        "lede": "Two paths.",
        "toc_title": "Contents",
        "sections": [
            {"id": "a", "h": "First", "p": ["Plain <script>alert(1)</script> text."]},
            {"id": "b", "h": "Second", "steps": ["one", "two"],
             "code": "echo '<hi>'", "table": [["key", "value"]]},
        ],
    }},
}


def test_structure_renders():
    doc = fg.render_guide_page(SYNTH).decode()
    assert "<h1>Guide</h1>" in doc, "title missing"
    assert 'href="#a"' in doc and 'href="#b"' in doc, "toc missing"
    assert '<section id="a">' in doc and '<section id="b">' in doc
    assert '<ol class="steps">' in doc and "<li>one</li>" in doc, "steps missing"
    assert "<table>" in doc and "<td>key</td>" in doc, "table missing"
    assert "<pre><code>echo &#x27;&lt;hi&gt;&#x27;</code></pre>" in doc, "code block missing"
    print("structure ok")


def test_escapes_everything():
    doc = fg.render_guide_page(SYNTH).decode()
    assert "<script>" not in doc, "raw script tag leaked"
    assert "&lt;script&gt;" in doc, "text not escaped"
    print("escaping ok")


def test_empty_guide_is_safe():
    doc = fg.render_guide_page({}).decode()
    assert "No guide is configured" in doc, "empty state missing"
    print("empty state ok")


def test_deployment_guide_is_substantial():
    cfg = fg.CFG
    sections = ((cfg.get("page") or {}).get("guide") or {}).get("sections") or []
    assert len(sections) >= 10, f"guide too thin: {len(sections)} sections"
    words = sum(len(str(s.get(k, ""))) for s in sections for k in ("p", "steps", "bullets", "table"))
    assert words > 4000, f"guide too short: {words} chars"
    doc = fg.render_guide_page(cfg).decode()
    assert "/guide" not in doc or "guide_link" not in doc
    assert "did:web" in doc, "the did:web step is missing"
    assert "publish_feeds.py" in doc, "the publish step is missing"
    print(f"deployment guide ok: {len(sections)} sections, ~{words} chars")


if __name__ == "__main__":
    test_structure_renders()
    test_escapes_everything()
    test_empty_guide_is_safe()
    test_deployment_guide_is_substantial()
    print("ALL GUIDE TESTS PASSED")
