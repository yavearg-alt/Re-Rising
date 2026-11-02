"""Emulation of the GREE gamelib account server (bn-payment.wrightflyer.net, SDK net.gree.gamelib 1.5.9).

Responses are `{"result": "OK", ...}` with HTTP 200 (net.gree.gamelib.core.http.ResponseAdapter);
errors are `{"code": int, "message": str}` with a non-200 status. Requests are OAuth-signed; we don't
verify signatures, we only read the device uuid from the Authorization header.
"""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import urllib.parse
import threading
import time
import uuid as uuidlib

import config
import db
import userdata

OAUTH_RE = re.compile(r'([-_a-z]+)="([^"]*)"')

# GREE app id -> OAuth consumer secret, per region. Stored AES-ECB encrypted in libapp.so (tables 0x27589f4 /
# 0x2758a00) with key "dzirUyLK0L39oKih" and decrypted by net.gree.gamelib.core.internal.f.
CONSUMER_SECRETS = {
    "207888378626135": "399c9776cd04f8e06cfdb93f6fddf9bd",
    "917471393710884": "6b66e0700d700f2d0ef43866ca9b2a73",
    "714809431779014": "126143e58cd8a6903d8aff5d85d2705b",
}


def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="-._~")


def _sign_response(method: str, url: str, consumer_key: str) -> str:
    """X-GREE-Authorization header, verified by SignedRequest.onPostRequest (HMAC-SHA1, key = raw secret).

    The base string is the request's method + URL + (our oauth params + the request query parameters).
    """
    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": str(random.getrandbits(62)),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    signed = dict(params)
    base_url, _, query = url.partition("?")
    for pair in filter(None, query.split("&")):
        k, _, v = pair.partition("=")
        signed[k] = v
    param_str = "&".join(f"{_enc(k)}={_enc(signed[k])}" for k in sorted(signed))
    base = f"{_enc(method)}&{_enc(base_url)}&{_enc(param_str)}"
    secret = CONSUMER_SECRETS.get(consumer_key, "")
    sig = base64.b64encode(hmac.new(secret.encode(), base.encode(), hashlib.sha1).digest()).decode()
    params["oauth_signature"] = sig
    return "OAuth " + ",".join(f'{k}="{_enc(v)}"' for k, v in sorted(params.items()))


def _oauth(headers: dict) -> dict:
    auth = headers.get("AUTHORIZATION") or headers.get("X-GREE-AUTHORIZATION") or ""
    return dict(OAUTH_RE.findall(auth))


def _ok(**fields):
    return 200, json.dumps({"result": "OK", **fields}).encode(), "application/json", {}


CREATIONS_PER_HOUR = 10  # new devices / accounts per client IP (the game makes one per install)
_creations: dict = {}
_creations_lock = threading.Lock()


def _creation_allowed(ip: str) -> bool:
    """Rate limit on device/account creation: a script can't fill the disk with accounts."""
    if not ip:
        return True
    with _creations_lock:
        now = time.time()
        for k in [k for k, v in _creations.items() if not v or now - v[-1] > 3600]:
            _creations.pop(k, None)
        recent = [t for t in _creations.get(ip, []) if now - t < 3600]
        if len(recent) >= CREATIONS_PER_HOUR:
            return False
        _creations[ip] = recent + [now]
        return True


def handle_gree(method: str, full_path: str, headers: dict, body: bytes, log, ip: str = "", public_url: str = ""):
    """`public_url`: the URL the client called when it differs from ours (iOS GREE SDK behind Caddy: it keeps
    only the host of its server URL, so it calls https://<host>/v1.0/...): the response is signed with it."""
    path = full_path.split("?", 1)[0]
    code, out, ctype, extra = _route(method, path, headers, body, log, ip)
    consumer_key = _oauth(headers).get("oauth_consumer_key", "")
    url = public_url or "http://" + headers.get("HOST", "") + full_path
    extra = dict(extra, **{"X-GREE-Authorization": _sign_response(method, url, consumer_key)})
    return code, out, ctype, extra


def _route(method: str, path: str, headers: dict, body: bytes, log, ip: str = ""):
    route = path[len("/gree"):]
    oauth = _oauth(headers)
    try:
        req = json.loads(body) if body else {}
    except ValueError:
        req = {"_raw": body[:200].decode("latin1")}
    device_uuid = oauth.get("xoauth_requestor_id") or req.get("uuid") or ""
    log(f"GREE {method} {route} uuid={device_uuid or '-'} body={json.dumps(req)[:200]}")
    with open(config.log_file("gree_requests.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "method": method, "path": path, "headers": headers,
                            "body": body.decode("utf-8", "replace")}) + "\n")

    if route == "/v1.0/auth/initialize":
        if not _creation_allowed(ip):
            log(f"GREE refused initialize: too many new devices from {ip}")
            return 429, json.dumps({"result": "NG"}).encode(), "application/json", {}
        new_uuid = str(uuidlib.uuid4().int)[:19]
        db.create_device(new_uuid, req.get("device_id", ""))
        return _ok(uuid=new_uuid)

    if route == "/v1.0/auth/authorize":
        return _ok()

    if route == "/v1.0/auth/x_uid":
        x_app_id = oauth.get("oauth_consumer_key", "")
        if method == "POST":
            # never sent by the game (accounts are bound by the GET below or the admin panel); it let any device take
            # another player's account, so it may only bind a fresh device to a fresh account
            target = str(req.get("x_uid", ""))
            dev = db.get_device(device_uuid) if device_uuid else None
            if not device_uuid or (dev and dev["x_uid"]) or db.devices_of_xuid(target):
                log(f"GREE refused x_uid rebind {device_uuid or '-'} -> {target}")
                return _ok()
            db.bind_xuid(device_uuid, target, str(req.get("x_app_id", x_app_id)))
            return _ok()
        dev = db.get_device(device_uuid) if device_uuid else None
        if dev is None or not dev["x_uid"]:
            if dev is None and not _creation_allowed(ip):
                # a device not issued by auth/initialize (which is rate-limited) falls under the same limit
                log(f"GREE refused new account: too many from {ip}")
                return 429, json.dumps({"result": "NG"}).encode(), "application/json", {}
            # First launch on this device: create the game account right away and bind it.
            user_id = db.create_user()
            db.bind_xuid(device_uuid, str(user_id), x_app_id)
            log(f"GREE new user {user_id} for device {device_uuid}")
            return _ok(x_uid=str(user_id), x_app_id=x_app_id)
        return _ok(x_uid=dev["x_uid"], x_app_id=dev["x_app_id"] or x_app_id)

    if route == "/v1.0/moderate/keywordlist":
        # chat keyword filter (net.gree.gamelib.moderation.internal.d): no banned words on a private server
        # moderation responses are wrapped in "entry" (internal/t.a())
        # The SDK only writes its local "kwlt" file when the list is non-empty, and name validation reads that
        # file, so ship a single keyword nobody will ever type.
        keyword = {"id": 1, "type": 0, "rank": 1, "keyword": "saoubprivateserverdummykeyword"}
        return _ok(entry={"keywords": [keyword], "timestamp": str(int(time.time()))})

    if route.startswith("/v1.0/moderate"):
        # /moderate/filtering/commit (internal/g$a): every submitted text is accepted
        return _ok(entry={"results": [], "timestamp": str(int(time.time()))})

    # Payment / deposit book (net.gree.gamelib.payment): data lives under "entry" (internal/q).
    if route in ("/v1.0/payment/productlist", "/v1.0/payment/subscription/productlist"):
        return _ok(entry={"products": [], "welcome": 0})  # shop/ProductList: nothing on sale
    if route in ("/v1.0/payment/subscription/status", "/v1.0/payment/subscription/history",
                 "/v1.0/payment/history"):
        return _ok(entry=[])  # shop/SubscriptionList etc. parse an array
    if route in ("/v1.0/payment/balance", "/v1.0/payment/refund/balance"):
        # depositbook/Balance: premium currency ("Diamond Cubes") was held by GREE
        dev = db.get_device(device_uuid) if device_uuid else None
        uid = int(dev["x_uid"]) if dev and str(dev["x_uid"] or "").isdigit() else 0
        free = userdata.gems(uid) if uid else int(config.START_FREE_GEMS)
        return _ok(entry={"balance_charge_gem": 0, "balance_free_gem": free, "balance_total_gem": free,
                          "payback_code": 0})

    if route == "/v1.0/deviceverification/nonce":
        return _ok(nonce=uuidlib.uuid4().hex)

    # clientlog, deviceverification/verify, migration/*, /v2/purchase ...: accept everything for now
    return _ok(timestamp=int(time.time()))
