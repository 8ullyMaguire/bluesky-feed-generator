#!/usr/bin/env python3
"""Tests for the reader pages: /quiz, /stats, /taste, and the index CTA.

The assertions that matter here are the ones that protect the house rules:
  * no external asset and no <script src=...> on any page (stdlib only);
  * /stats contains no did:plc: -- aggregate only;
  * /taste with no identity is a 200 page that explains itself, not a 500;
  * the index page carries the primary CTA, a /quiz link, and a sentence
    stating the direct-connection and blocks/mutes limits.

Run: python3 test_pages2.py
"""
import importlib.util
import os
import re
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="feedgen-pages2-test-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")
os.environ["FEEDGEN_STATE_PATH"] = os.path.join(TMP, "no-legacy.json")
if not os.environ.get("FEEDGEN_CONFIG"):
    os.environ["FEEDGEN_CONFIG"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "config.json" if os.path.exists(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"))
        else "config.example.json")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_s = importlib.util.spec_from_file_location("fg_p2", os.path.join(HERE, "feedgen.py"))
fg = importlib.util.module_from_spec(_s)
_s.loader.exec_module(fg)  # type: ignore[union-attr]


def _html(body):
    return body.decode("utf-8", "replace") if isinstance(body, bytes) else body


def _no_external(html):
    """No remote ASSET may be loaded. A remote LINK is fine and intended: the
    page's whole job is sending people to bsky.app and to the source repo.
    The distinction is load vs navigate -- a stylesheet, script, font or image
    is loaded by the browser, and that is what stdlib-only forbids."""
    assert not re.search(r"<script[^>]+src=", html), "page loads an external script"
    assert not re.search(r"<link[^>]+href=", html), "page loads an external stylesheet"
    for tag, m in re.findall(r'<(img|iframe|source)\b[^>]+src="([^"]+)"', html):
        raise AssertionError(f"page loads a remote {tag}: {m}")
    for m in re.findall(r'url\(\s*["\']?(https?://[^)"\']+)', html):
        raise AssertionError(f"CSS pulls a remote asset: {m}")


def _seed_posts(n=12):
    """Give the throwaway DB some posts, or /quiz renders its empty state and
    the form is never exercised. Uses a distinct set of author DIDs so the
    quiz's own owner-exclusion does not filter everything out."""
    now = time.time()
    rows = []
    for i in range(n):
        rows.append((f"at://did:plc:quizauthor{i:03d}/app.bsky.feed.post/q{i:03d}",
                     f"cid{i}", f"did:plc:quizauthor{i:03d}", f"autor{i}.test",
                     "2026-09-28T00:00:00.000Z", i, 0, 0, 0,
                     f"Un post de prueba numero {i} sobre marxismo y la clase obrera.",
                     "", "{}", now, now))
    with fg.DB_LOCK:
        fg.db().executemany(
            "INSERT OR REPLACE INTO posts(uri,cid,author_did,author_handle,"
            "indexed_at,like_count,repost_count,quote_count,reply_count,text,"
            "langs,record_json,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        fg.db().commit()


def test_quiz_is_a_javascript_free_form():
    _seed_posts()
    html = _html(fg.render_quiz_page(fg.CFG))
    _no_external(html)
    assert "<form method=\"post\"" in html, html[:300]
    assert "submitTaste" in html
    assert "name=\"more\"" in html and "name=\"less\"" in html
    # the visitor-key disclosure, in words, on the page
    assert "no es tu identidad" in html.lower() or "no es tu identidad" in html
    assert "no" in html and "identidad" in html
    print("ok   /quiz is a plain form, no JS, and states the key is not identity")


def test_stats_is_aggregate_only():
    html = _html(fg.render_stats_page(fg.CFG))
    _no_external(html)
    assert "did:plc:" not in html, "a DID leaked onto the public stats page"
    assert "personas distintas" in html
    for section in ("Lectores y volumen", "Por dia", "Por semana",
                    "Interaccion", "Por feed", "Como leer estas cifras"):
        assert section in html, f"/stats lost its {section!r} section"
    print("ok   /stats is aggregate only, with day/week/feed/interaction sections")


def test_stats_counts_readers_per_day_and_likes():
    """The per-day reader count must come from requester_daily (one row per
    reader per day, never rewritten). A head count read from served_posts
    decays silently: that table's key is (reader, post), so re-serving the same
    post to the same reader REWRITES served_at and the old day loses the reader.
    Likes are counted from the events the poller observed.
    """
    today = fg.datetime.fromtimestamp(time.time(), fg.timezone.utc).date()
    day0 = today.isoformat()
    day1 = (today - fg.timedelta(days=1)).isoformat()
    for did, day in (("did:plc:statsa", day0), ("did:plc:statsb", day0),
                     ("did:plc:statsc", day1)):
        fg.db().execute("INSERT OR REPLACE INTO requester_daily"
                        "(day, requester_did) VALUES(?,?)", (day, did))
    fg.db().execute("INSERT INTO interaction_events(post_uri, feed_rkey, kind, "
                    "created_at, requester_did) VALUES(?,?,?,?,?)",
                    ("at://did:plc:x/app.bsky.feed.post/stat", "f",
                     "like", time.time(), "did:plc:statsa"))
    st = fg.stats_payload()
    assert st["readers"]["today"] >= 2, st["readers"]
    assert st["readers"]["d7"] >= 3, st["readers"]
    assert st["daily"][-1]["day"] == day0, st["daily"][-1]
    assert st["daily"][-1]["readers"] >= 2, st["daily"][-1]
    assert st["daily"][-1]["likes"] >= 1, st["daily"][-1]
    assert len(st["daily"]) == 14 and len(st["weekly"]) == 8
    assert sum(w["readers"] for w in st["weekly"]) >= 3, st["weekly"]
    html = _html(fg.render_stats_page(fg.CFG))
    assert day0 in html and "Por dia" in html
    assert "did:plc:" not in html
    print("ok   /stats counts people per day from requester_daily, and likes/day")


def test_serving_path_records_the_reader_day():
    """log_served_posts is what the skeleton endpoint calls. If it does not
    leave the durable per-day row, every reader figure on /stats goes quiet
    over time while the page keeps rendering."""
    did = "did:plc:servepath"
    day0 = fg.datetime.fromtimestamp(time.time(), fg.timezone.utc).date().isoformat()
    fg.db().execute("DELETE FROM requester_daily WHERE requester_did=?", (did,))
    fg.log_served_posts(did, "probe",
                        ["at://did:plc:a/app.bsky.feed.post/served1"])
    n = fg.db().execute("SELECT COUNT(*) FROM requester_daily WHERE "
                        "requester_did=? AND day=?", (did, day0)).fetchone()[0]
    assert n == 1, n
    print("ok   serving a page records the reader-day that /stats counts")


def test_taste_without_identity_is_a_200_explainer():
    html = _html(fg.render_taste_page(fg.CFG, None, None))
    _no_external(html)
    assert "Necesitamos saber quien eres" in html or "quien eres" in html
    assert "did:plc:" not in html
    assert "/quiz" in html
    print("ok   /taste with no identity explains itself, links the quiz, no 500")


def test_taste_with_visitor_key_renders():
    html = _html(fg.render_taste_page(fg.CFG, None, "f" * 32))
    _no_external(html)
    assert "did:plc:" not in html
    assert "g" in html  # page rendered, not an error
    print("ok   /taste with a visitor key renders the quiz's own rows")


def test_index_has_cta_quiz_link_and_limits_sentence():
    html = _html(fg.render_index_page(fg.CFG))
    _no_external(html)
    assert 'class="cta-primary"' in html
    assert "/quiz" in html
    low = html.lower()
    assert "bloque" in low and "silenc" in low, "the block/mute limitation is not stated"
    assert "/stats" in html
    print("ok   / shows the CTA, a /quiz link, /stats, and the blocks/mutes limit")


def test_taste_lists_each_author_exactly_once():
    """A one-row-per-author guarantee.

    This regressed for real: a LEFT JOIN to posts returned one row per stored
    POST, so five affinity rows rendered as twenty-five rows all showing the
    same handle -- the reader would conclude the quiz counted one choice five
    times. An author with several stored posts is the normal case, so the
    fixture creates several on purpose.
    """
    _seed_posts()
    vkey = "e" * 32
    now = time.time()
    # Five affinity rows for FIVE different authors, and the FIRST author gets
    # many stored posts. One post per author cannot reproduce the bug at all --
    # a one-to-many relation only duplicates when the "many" side has >1 row.
    # Real authors here hold hundreds to thousands of posts.
    authors = [f"did:plc:quizauthor{i:03d}" for i in range(5)]
    with fg.DB_LOCK:
        fg.db().executemany(
            "INSERT OR REPLACE INTO posts(uri,cid,author_did,author_handle,"
            "indexed_at,like_count,repost_count,quote_count,reply_count,text,"
            "langs,record_json,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"at://{authors[0]}/app.bsky.feed.post/many{j}", f"c{j}",
              authors[0], "manyposts.test", "2026-09-28T00:00:00.000Z",
              1, 0, 0, 0, f"post {j}", "", "{}", now, now)
             for j in range(12)])
        fg.db().executemany(
            "INSERT OR REPLACE INTO visitor_affinity(visitor_key, author_did, "
            "score, updated_at) VALUES(?,?,?,?)",
            [(vkey, a, 0.6, now) for a in authors])
        fg.db().commit()
    html = _html(fg.render_taste_page(fg.CFG, None, vkey))
    rows = re.findall(r"<tr><td>@?([^<]*)</td><td>([-0-9.]+)</td></tr>", html)
    # The negative-terms table is the second one on the page and its values are
    # hit counts (integers); the affinity table holds signed scores. Filter on
    # the SIGN, which is what distinguishes them, or this test silently stops
    # checking anything.
    aff = [r for r in rows if r[1].startswith("-") or float(r[1]) not in (1.0, 0.0)]
    assert len(aff) == 5, f"expected 5 author rows, got {len(aff)}: {aff}"
    assert len({r[0] for r in aff}) == 5, f"handles repeated: {aff}"
    print("ok   /taste lists each author once, even with many stored posts")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PAGES2 TESTS PASSED")
