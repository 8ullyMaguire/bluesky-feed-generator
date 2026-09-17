#!/usr/bin/env python3
"""Tests for the authenticated getFeedSkeleton path.

Regression guard: a request carrying an Authorization header used to get an
empty reply (the Bluesky app shows "Error") because verify_jwt() was called but
never defined, while anonymous requests kept working.

Tokens here are really signed with a throwaway SECP256K1 key whose public half
is injected into the module's DID-document cache, so no network is needed and
the ES256K path is exercised for real.

Run: python3 test_feedgen_auth.py
Uses a throwaway SQLite file so the real feedgen.sqlite is untouched.
"""
import base64
import importlib.util
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.asymmetric import ec as _ec

TMP = tempfile.mkdtemp(prefix="feedgen-auth-test-")
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

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PRIV = _ec.generate_private_key(_ec.SECP256K1())
PUB = PRIV.public_key().public_numbers()
KID_DID = "did:plc:testverify000000000000000000000001"
FULL_KID = f"{KID_DID}#atproto"
ISSUER = "did:plc:testrequester00000000"


def _b58c(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n > 0:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "z" + s


def _pubkey_doc():
    """KID document holding this test key's public half (multibase compressed)."""
    comp = bytes([0x03 if (PUB.y & 1) else 0x02]) + PUB.x.to_bytes(32, "big")
    return {"verificationMethod": [{"id": FULL_KID, "publicKeyMultibase": _b58c(comp)}]}


fg._DID_DOC_CACHE[KID_DID] = (_pubkey_doc(), time.time())


def _b64(obj):
    return base64.urlsafe_b64encode(
        json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()


def bearer(iss=ISSUER, aud=None, lxm="app.bsky.feed.getFeedSkeleton",
           exp=None, kid=FULL_KID):
    """A signed ATProto service JWT for the test key above."""
    header = {"alg": "ES256K", "typ": "JWT", "kid": kid}
    payload = {"iss": iss, "aud": aud or fg.SERVICE_DID, "lxm": lxm}
    if exp is not None:
        payload["exp"] = exp
    hdr_b, pay_b = _b64(header), _b64(payload)
    sig = PRIV.sign(f"{hdr_b}.{pay_b}".encode(), _ec.ECDSA(_hashes.SHA256()))
    jwt = f"{hdr_b}.{pay_b}." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return "Bearer " + jwt


def test_verify_jwt_is_defined():
    assert callable(getattr(fg, "verify_jwt", None)), (
        "feedgen.verify_jwt is undefined -> NameError and an empty reply for every "
        "authenticated request (the Bluesky app shows 'Error')")
    print("ok  verify_jwt is defined")


def test_verify_jwt_cases():
    now = int(time.time())
    assert fg.verify_jwt(bearer(exp=now + 600)) == ISSUER
    assert fg.verify_jwt(bearer(exp=now + 600, aud="did:web:evil.example")) is None
    assert fg.verify_jwt(bearer(exp=now + 600, lxm="app.bsky.feed.getLikes")) is None
    assert fg.verify_jwt(bearer(exp=now - 10)) is None                       # expired
    assert fg.verify_jwt("Bearer " + bearer(exp=now + 600).split()[1][:-4] + "XXXX") is None
    assert fg.verify_jwt(None) is None
    assert fg.verify_jwt("Bearer not-a-jwt") is None
    assert fg.verify_jwt("Basic abc.def.ghi") is None
    print("ok  verify_jwt: signed passes; tampered/expired/wrong-aud/wrong-lxm/"
          "garbage rejected")


def test_verify_jwt_rejects_unknown_kid():
    """Fail-closed: a token signed by a key the KID document does not list is not
    accepted, and an unreachable resolver never opens a back door."""
    def _resolver_down(did):
        raise OSError("resolver down")

    orig_fetch = fg._fetch_did_doc
    old_doc = fg._DID_DOC_CACHE.pop(KID_DID, None)
    try:
        assert fg.verify_jwt(bearer(exp=int(time.time()) + 600)) is None
        fg._fetch_did_doc = _resolver_down
        fg._DID_DOC_CACHE[KID_DID] = (_pubkey_doc(), time.time())
        assert fg.verify_jwt(bearer(exp=int(time.time()) + 600)) is None
    finally:
        fg._fetch_did_doc = orig_fetch
        if old_doc is not None:
            fg._DID_DOC_CACHE[KID_DID] = old_doc
    print("ok  verify_jwt: unknown kid / dead resolver reject (fail-closed)")


def test_getfeedskel_with_bearer_returns_200():
    """The regression test: an authenticated request must not drop the connection."""
    key = next(iter(fg.FEEDS))
    rkey = fg.FEEDS[key]["rkey"]
    with fg.CACHE_LOCK:
        fg.CACHE["feeds"] = {rkey: [f"at://did:plc:a/app.bsky.feed.post/{i}" for i in range(5)]}
        fg.CACHE["gen"] = 1
    srv = fg.HTTPServer(("127.0.0.1", 0), fg.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = (f"http://127.0.0.1:{srv.server_address[1]}"
           f"/xrpc/app.bsky.feed.getFeedSkeleton?feed={urllib.parse.quote(key)}&limit=5")
    try:
        for label, headers in (("anonymous", {}),
                               ("bearer", {"Authorization": bearer(exp=int(time.time()) + 600)})):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.load(r)
            assert r.status == 200, (label, r.status)
            assert len(body["feed"]) == 5, (label, body)
            print(f"ok  getFeedSkeleton ({label}): HTTP 200, {len(body['feed'])} posts")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL FEEDGEN AUTH TESTS PASSED")
