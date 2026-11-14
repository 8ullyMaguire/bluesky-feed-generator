"""The rising page and the guide blocks: two things that are easy to get subtly wrong.

The rising list is a claim about the world ("this grew the most"), so the rules that
make the claim honest are tested directly: a post we only just started watching must
not appear, and the number must be growth, not the post's total.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedgen as fg  # noqa: E402


def fresh_db():
    path = tempfile.mktemp(suffix=".sqlite")
    fg.DB_PATH = path
    fg._LOCAL = type(fg._LOCAL)()          # drop the cached connection
    con = fg.db()
    return con


def test_rising_counts_growth_and_ignores_the_young():
    con = fresh_db()
    now = time.time()
    rows = [
        # old post, big growth: must top the list
        ("at://a/old", now - 6 * 86400, 100, 5),
        ("at://a/old", now - 3600, 900, 40),
        # old post, no growth: must not appear (below the threshold)
        ("at://b/flat", now - 6 * 86400, 500, 10),
        ("at://b/flat", now - 3600, 505, 10),
        # young post, huge growth in a short span: must not appear
        ("at://c/young", now - 2 * 3600, 0, 0),
        ("at://c/young", now - 600, 5000, 100),
    ]
    for uri, ts, likes, reposts in rows:
        con.execute(
            "INSERT INTO engagement_snapshots (uri, ts, likes, reposts, quotes, replies) "
            "VALUES (?,?,?,?,0,0)", (uri, ts, likes, reposts))
    for uri, handle, text in [
        ("at://a/old", "alguien.bsky.social", "Texto del post viejo que creció."),
        ("at://b/flat", "otro.bsky.social", "Texto del post plano."),
        ("at://c/young", "joven.bsky.social", "Texto del post joven."),
    ]:
        con.execute(
            "INSERT INTO posts (uri, author_did, author_handle, indexed_at, text, "
            "like_count, repost_count, record_json, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,0,0,'{}',?,?)",
            (uri, "did:plc:" + handle, handle, "2026-01-01T00:00:00Z", text,
             now - 6 * 86400, now))

    out = fg.compute_rising(fg.CFG, 168, 48, 10)
    uris = [p["uri"] for p in out["posts"]]
    assert uris == ["at://a/old"], uris
    assert out["posts"][0]["delta_likes"] == 800, out["posts"][0]
    assert out["posts"][0]["likes"] == 900
    assert out["authors"][0]["handle"] == "alguien.bsky.social"
    json.dumps(out)
    print("rising: growth only, young posts excluded, delta not total")


def test_rising_is_empty_rather_than_wrong():
    fresh_db()
    out = fg.compute_rising(fg.CFG, 168, 48, 10)
    assert out["count"] == 0 and out["posts"] == [] and out["authors"] == []
    print("rising: an empty database produces an empty list, not an error")


def test_guide_blocks_follow_the_renderer_order():
    section = {"id": "x", "h": "Titulo", "p": ["uno", "dos"],
               "steps": ["paso"], "bullets": ["punto"],
               "code": "echo hola", "table": [["a", "b"]]}
    kinds = [b["type"] for b in fg.guide_blocks(section)]
    assert kinds == ["p", "p", "steps", "bullets", "code", "table"], kinds
    # and the HTML renderer must emit them in that same order
    html = fg.render_guide_page({"page": {"guide": {"title": "T", "sections": [section]}}})
    if isinstance(html, bytes):
        html = html.decode("utf-8")
    order = [html.index(x) for x in
             ("<p>uno</p>", "<ol class=\"steps\">", "<ul class=\"bullets\">",
              "<pre", "<table>")]
    assert order == sorted(order), order
    print("guide: block order matches the HTML renderer")


def test_english_guide_mirrors_the_spanish_one():
    """If a deployment ships an English guide, it must cover the same sections.

    A half-translated guide is worse than none: the reader follows the table of contents
    into a section that does not exist. Structure is compared, not wording.
    """
    page = fg.page_cfg(fg.CFG)
    es = page.get("guide") or {}
    en = page.get("guide_en")
    if not en:
        print("english guide: not configured here, nothing to compare")
        return
    es_ids = [s.get("id") for s in es.get("sections") or []]
    en_ids = [s.get("id") for s in en.get("sections") or []]
    assert en_ids == es_ids, (en_ids, es_ids)
    for s_es, s_en in zip(es.get("sections") or [], en.get("sections") or []):
        for key in ("p", "steps", "bullets"):
            assert len(s_en.get(key) or []) == len(s_es.get(key) or []), \
                f"{s_es.get('id')}: {key} count differs"
        assert bool(s_en.get("code")) == bool(s_es.get("code")), s_es.get("id")
        assert len(s_en.get("table") or []) == len(s_es.get("table") or []), \
            f"{s_es.get('id')}: table rows differ"
    print(f"english guide mirrors all {len(en_ids)} sections")


def test_suggestion_bar_is_stated():
    assert len(fg.SUGGEST_BAR) >= 4
    assert all(len(line) > 20 for line in fg.SUGGEST_BAR)
    print("suggestions: the bar is explicit, not implied")


if __name__ == "__main__":
    test_rising_counts_growth_and_ignores_the_young()
    test_rising_is_empty_rather_than_wrong()
    test_guide_blocks_follow_the_renderer_order()
    test_english_guide_mirrors_the_spanish_one()
    test_suggestion_bar_is_stated()
    print("ALL RISING/GUIDE CHECKS PASSED")
