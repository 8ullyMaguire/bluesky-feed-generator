"""A restart must serve content immediately: the last computed boards are
persisted and reloaded, and a missing or corrupt file is not an error.

The defect this guards: a cold refresh walks every list member's recent posts
and takes minutes, and until it lands the port answers empty skeletons -- which
is exactly what a failed refresh looks like to a reader. The persisted board is
what turns that window into slightly stale content instead of nothing.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fg-boards-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "b.sqlite")
os.environ["FEEDGEN_BOARDS"] = os.path.join(TMP, "boards.json")

cfg = json.load(open("/home/alvaro/feedgen/config.json"))
cfg["feeds"] = [{"rkey": "t", "display_name": "T", "max_age_hours": 48,
                 "max_posts": 20}]
PATH = os.path.join(TMP, "config.json")
json.dump(cfg, open(PATH, "w"))
os.environ["FEEDGEN_CONFIG"] = PATH

sys.path.insert(0, HERE)
import feedgen as fg  # noqa: E402

assert fg.BOARDS_PATH == os.path.join(TMP, "boards.json"), fg.BOARDS_PATH

# Nothing persisted yet: the first refresh runs exactly as it did before.
assert fg.load_boards() is None
print("no file -> None (a first run is unchanged)")

# A board round-trips.
picks = {"at://did:plc:a/app.bsky.feed.generator/t":
         ["at://did:plc:a/app.bsky.feed.post/1",
          "at://did:plc:a/app.bsky.feed.post/2"]}
fg.save_boards(picks)
assert fg.load_boards() == picks
print("board round-trips:", sum(len(v) for v in fg.load_boards().values()), "picks")

# A crash mid-write must not leave a file that loads as a board.
with open(fg.BOARDS_PATH, "w") as fh:
    fh.write("{not json")
assert fg.load_boards() is None
print("corrupt file -> None, not an exception")

# An empty board is not worth serving: the first refresh should run instead.
fg.save_boards({})
assert fg.load_boards() is None
print("empty board -> None")

# The write is atomic, so a reader never sees a partial board.
fg.save_boards(picks)
assert not os.path.exists(fg.BOARDS_PATH + ".tmp"), "temp file left behind"
print("no .tmp left behind")

print("ALL BOARD CHECKS PASSED")
