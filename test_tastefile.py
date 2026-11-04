"""Signed taste files: publish, verify, tamper, follow with a cap."""
import http.server
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fg-taste-")
os.environ["FEEDGEN_DB"] = os.path.join(TMP, "t.sqlite")
os.environ.setdefault("FEEDGEN_CONFIG", "/home/alvaro/feedgen/config.json")
sys.path.insert(0, HERE)

import feedgen as fg  # noqa: E402

fg.migrate_schema()
con = fg.db()
NOW = time.time()

for did, n in (("did:plc:a", 100), ("did:plc:b", 50), ("did:plc:c", 10)):
    con.execute("INSERT OR REPLACE INTO taste(author_did, likes_count, updated_at) "
                "VALUES(?,?,?)", (did, n, NOW))
con.execute("INSERT OR REPLACE INTO author_topic(author_did, topic_posts, "
            "total_posts, affinity, updated_at) VALUES(?,?,?,?,?)",
            ("did:plc:a", 10, 10, 0.8, NOW))

doc = fg.build_taste_file()
assert doc["version"] == 1 and doc["instance"].startswith("did:web:")
assert doc["authors"]["did:plc:a"] == 1.0, doc["authors"]
assert doc["authors"]["did:plc:b"] == 0.5, doc["authors"]
assert doc["signature"]["alg"] == "ed25519"
print("taste doc built:", {k: doc[k] for k in ("version", "instance")},
      "authors:", doc["authors"])

pub = fg.instance_pubkey_b64()
assert fg.verify_taste_file(doc, pub), "own signature must verify"
print("signature verifies")

tampered = json.loads(json.dumps(doc))
tampered["authors"]["did:plc:a"] = 0.01
assert not fg.verify_taste_file(tampered, pub), "tampering must break the signature"
print("tampering detected")

# Serve the file and the did document from a throwaway HTTP origin, then
# follow it exactly as a second instance would.
origin = os.path.join(TMP, "origin")
os.makedirs(os.path.join(origin, ".well-known"))
json.dump(doc, open(os.path.join(origin, ".well-known", "taste.json"), "w"))
json.dump({"id": doc["instance"],
           "verificationMethod": [{"publicKeyJwk": {"kty": "OKP", "crv": "Ed25519",
                                                    "x": pub}}]},
          open(os.path.join(origin, ".well-known", "did.json"), "w"))

os.chdir(origin)
srv = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0), lambda *a, **k: http.server.SimpleHTTPRequestHandler(
        *a, directory=origin, **k))
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/.well-known/taste.json"

followed = fg.fetch_taste_source(url)
assert followed["authors"]["did:plc:a"] == 1.0
print("fetched and verified over HTTP")

local = {"did:plc:b": 0.2}
cfg2 = {"taste_sources": [{"url": url, "weight": 0.3, "max_authors": 2,
                           "cache_secs": 0}]}
merged = fg.apply_taste_sources(local, cfg2, now=time.time())
assert merged["did:plc:b"] == 0.35, merged          # 0.2 + 0.3 * 0.5
assert "did:plc:c" not in merged, merged            # capped to the top 2
assert merged["did:plc:a"] == 0.3, merged
print("followed with weight and author cap:", merged)

# A cache hit must not re-fetch: break the origin, ask again.
json.dump({"authors": {}}, open(os.path.join(origin, ".well-known", "taste.json"), "w"))
cfg3 = {"taste_sources": [{"url": url, "weight": 0.3, "max_authors": 2,
                           "cache_secs": 3600}]}
again = fg.apply_taste_sources({}, cfg3, now=time.time())
assert again.get("did:plc:a") == 0.3, again
print("cache honoured")

srv.shutdown()
print("ALL TASTE-FILE CHECKS PASSED")
