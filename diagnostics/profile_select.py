"""Profile one feed's select() against the live corpus, without touching the
live database: the engine is imported against a throwaway config + DB path."""
import cProfile, io, json, os, pstats, shutil, sqlite3, sys, tempfile, time
from datetime import datetime, timezone

tmp = tempfile.mkdtemp(prefix="fgb-")
shutil.copy("/mnt/disk-important/personal/documents/code/misc/feedgen/config.json",
            os.path.join(tmp, "config.json"))
os.environ["FEEDGEN_CONFIG"] = os.path.join(tmp, "config.json")
os.environ["FEEDGEN_DB"] = os.path.join(tmp, "t.sqlite")
sys.path.insert(0, "/mnt/disk-important/personal/documents/code/misc/feedgen")
import feedgen as fg

con = sqlite3.connect("file:/home/alvaro/.local/share/feedgen/feedgen.sqlite?mode=ro", uri=True)
posts = [json.loads(r[0]) for r in con.execute(
    "SELECT record_json FROM posts ORDER BY indexed_at DESC LIMIT 20000")]
f = [x for x in fg.CFG["feeds"] if x["rkey"] == "leftist-3d"][0]
owner = fg.feed_owner(fg.CFG.get("owner") or {}, f)
ctx = {"topic_affinity": {}, "topic_seeds": {}, "taste": {}, "topic_keywords": [],
       "author_priors": {}, "topic_totals": {}, "owner_liked": set()}
now = datetime.now(timezone.utc)

t0 = time.monotonic(); fg.select(posts, f, now, owner, ctx, set()); base = time.monotonic() - t0
print("select(%s) over %d posts: %.2fs" % (f["rkey"], len(posts), base))

pr = cProfile.Profile(); pr.enable()
fg.select(posts, f, now, owner, ctx, set())
pr.disable()
s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(14)
print("\n".join(s.getvalue().splitlines()[4:22]))
