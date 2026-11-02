"""Internal HTTP API for the Discord bot (discord_bot/): account linking and gift claims.

Served on its own port (SAOUB_BOT_PORT, default 8090) inside the game server process, only when SAOUB_BOT_SECRET is
set. The port is never published nor proxied by Caddy: the bot container reaches it on the Docker network. Every
request carries "Authorization: Bearer <SAOUB_BOT_SECRET>". JSON in, JSON out; business errors come back as HTTP 200
{"ok": false, "error": <code>, "message": <text shown to the player>}.

Linking: /link gives the Discord user a short code; the player puts it in their in-game profile comment (UserInfo.comment,
saved by the game with user/push) and the bot calls link/check, which looks for the code in the accounts' comments.

Claims: every claim is written to the ledger (db.discord_claims). The rules file (SAOUB_DISCORD_RULES, JSON, re-read
when it changes; see discord_bot/rules.example.json) decides caps, cooldowns and what is auto-approved; by default
everything waits for a staff member. On approval the account is snapshotted, then the reward goes to its gift box
(api.add_gift), like a gift from the admin panel.
"""
import hmac
import json
import os
import re
import secrets
import threading
import time

import admin
import api
import config
import db
import master
import userdata

SECRET = os.environ.get("SAOUB_BOT_SECRET", "")
PORT = int(os.environ.get("SAOUB_BOT_PORT", "8090"))
HOST = os.environ.get("SAOUB_BOT_HOST", "127.0.0.1")
RULES_PATH = os.environ.get("SAOUB_DISCORD_RULES") or os.path.join(os.path.dirname(config.DB_PATH),
                                                                    "discord_rules.json")
CODE_PREFIX = "UB"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: typed by hand on a phone
CODE_SECONDS = 3600
KINDS = {"character": 5, "cubes": 3, "item": 1, "equipment": 2}  # claim kind -> api.add_gift rewardType
# per-claim amount when the rules file does not say: equipment fills the bag (one row each, "weapon inventory over
# the bag limit", 2026-10-21), characters are one each
DEFAULT_MAX = {"character": 1, "equipment": 10}
COUNTED = ("pending", "approved")  # claims that use up caps and cooldowns (refused or failed ones do not)

DEFAULT_RULES = {
    "auto_approve": {"users": [], "rewards": []},  # nothing is auto-approved
    "cooldown_seconds": 0,
    "max_pending": 3,
    "max_amount": {"cubes": 100000, "item": 9999, "equipment": 10},
    "caps": {},
    "users": {},
}

_decide_lock = threading.Lock()  # one decision at a time: a claim is never granted twice
_rules_cache = {"mtime": None, "rules": DEFAULT_RULES}
_names_cache = {}


# ---------------------------------------------------------------- rules

def rules() -> dict:
    """The rules file merged over DEFAULT_RULES; a missing or broken file means the defaults (all manual)."""
    try:
        mtime = os.path.getmtime(RULES_PATH)
    except OSError:
        return DEFAULT_RULES
    if mtime != _rules_cache["mtime"]:
        try:
            with open(RULES_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("not an object")
            _rules_cache["rules"] = {**DEFAULT_RULES, **{k: v for k, v in loaded.items() if not k.startswith("_")}}
        except (OSError, ValueError) as e:
            print(f"botapi: {RULES_PATH} ignored ({e}), defaults used", flush=True)
            _rules_cache["rules"] = DEFAULT_RULES
        _rules_cache["mtime"] = mtime
    return _rules_cache["rules"]


def user_rules(discord_id: str) -> dict:
    """Global rules with the per-user overrides ("users": {"<discord id>": {...}}) applied; caps merged per kind."""
    r = rules()
    over = (r.get("users") or {}).get(discord_id) or {}
    out = {**r, **{k: v for k, v in over.items() if k != "caps"}}
    out["caps"] = {**(r.get("caps") or {}), **(over.get("caps") or {})}
    return out


def _auto_approved(r: dict, discord_id: str, kind: str, target: int, amount: int) -> bool:
    auto = r.get("auto_approve") or {}
    if discord_id in [str(u) for u in auto.get("users") or []]:
        return True
    for rule in auto.get("rewards") or []:
        if rule.get("kind") != kind:
            continue
        if "id" in rule and int(rule["id"]) != target:
            continue
        if amount <= int(rule.get("max_amount", 1)):
            return True
    return False


def _check_limits(r: dict, discord_id: str, kind: str, amount: int):
    """Error message when this claim breaks a cooldown, the pending limit or a cap, else None."""
    now = int(time.time())
    mine = [c for c in db.list_discord_claims(discord_id=discord_id, since=now - 366 * 86400, limit=100000)
            if c["status"] in COUNTED]
    pending = [c for c in mine if c["status"] == "pending"]
    if r.get("max_pending") and len(pending) >= int(r["max_pending"]):
        return f"You already have {len(pending)} claim(s) waiting for staff approval; wait for them first."
    cooldown = int(r.get("cooldown_seconds") or 0)
    if cooldown and mine and now - mine[0]["created_at"] < cooldown:
        wait = cooldown - (now - mine[0]["created_at"])
        return f"Please wait {_duration(wait)} before your next claim."
    for scope, cap in (r.get("caps") or {}).items():
        if scope not in ("all", kind) or not isinstance(cap, dict):
            continue
        days = float(cap.get("days", 7))
        recent = [c for c in mine if c["created_at"] >= now - days * 86400 and scope in ("all", c["kind"])]
        what = "claims" if scope == "all" else f"{scope} claims"
        if "max_claims" in cap and len(recent) + 1 > int(cap["max_claims"]):
            return f"Limit reached: {int(cap['max_claims'])} {what} per {_days(days)}."
        if "max_total" in cap and sum(c["amount"] for c in recent) + amount > int(cap["max_total"]):
            left = max(0, int(cap["max_total"]) - sum(c["amount"] for c in recent))
            return f"Limit reached: {int(cap['max_total'])} {scope} per {_days(days)} ({left} left)."
    return None


def _duration(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    return f"{max(1, seconds // 60)} min"


def _days(days: float) -> str:
    return "day" if days == 1 else f"{days:g} days"


# ---------------------------------------------------------------- names

def character_names() -> dict:
    """{pcId (str): English display name, e.g. "[Dusk Approaching] Rain"} from localization.bin, cached in
    server_cache/master_pc_names_en.json. Falls back to the admin panel names (label + Japanese) where the raw
    master data is missing (the VPS only has server_cache)."""
    if "pcs" not in _names_cache:
        try:
            names = master._cached("pc_names_en", _build_character_names)
        except (OSError, ValueError):
            names = {}
        fallback = master.NAMES.get("pcs", {})
        _names_cache["pcs"] = {k: names.get(k) or v for k, v in fallback.items()} or names
    return _names_cache["pcs"]


def _build_character_names() -> dict:
    def rows(name):
        with open(os.path.join(master.JSON_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    full, short = {}, {}
    for r in rows("localization.0.json"):
        label, field, text = r.get("1"), r.get("3"), r.get("4")
        if not (isinstance(label, str) and label.startswith("character.") and isinstance(text, str) and text):
            continue
        if field == "name" and text.startswith("["):
            full[label] = text
        elif field == "shortName":
            short[label] = text
    out = {}
    for r in rows("pc.0.json"):
        label = r.get("6") or ""
        name = full.get(label) or short.get(label)
        if name:
            out[str(r["0"])] = name
    return out


def _build_placeholder_pcs() -> list:
    """Unfinished characters of the master (pc.0 with no passive skill, fields 46-49, and no English name): the
    trainee Kirito / Eugeo, field Kirito, swimsuit Silica. Claimed, they came as broken units (tester report, 2026-10-21)."""
    return sorted(str(r[0]) for r in master._json_rows("pc", 0) if all(r.get(f) is None for f in (46, 47, 48, 49)))


def placeholder_pcs() -> set:
    if "placeholders" not in _names_cache:
        try:
            _names_cache["placeholders"] = set(master._cached("pc_placeholders", _build_placeholder_pcs))
        except (OSError, ValueError):
            _names_cache["placeholders"] = set()
    return _names_cache["placeholders"]


def model_only_equipment() -> set:
    """The "(3D)" weapons (label *_3d): models for the battle scenes, with no icon nor stats. Claimed, they showed as
    empty equipment (sun, tester report, 2026-10-25)."""
    return {k for k, v in master.NAMES.get("equipment", {}).items() if str(v).rstrip(")").endswith("_3d")}


def character_choices() -> list:
    """[[pcId, name], ...] sorted by name; names shared by several ids get the id appended. No placeholder."""
    names = {k: v for k, v in character_names().items() if k not in placeholder_pcs()}
    seen = {}
    for n in names.values():
        seen[n] = seen.get(n, 0) + 1
    return sorted(([k, v if seen[v] == 1 else f"{v} ({k})"] for k, v in names.items()), key=lambda x: x[1].lower())


def resolve_character(text) -> int | None:
    """pcId from an id, an exact name, or a name fragment matching a single character."""
    raw = str(text).strip()
    names = character_names()
    if raw.isdigit():
        return int(raw) if raw in names and raw not in placeholder_pcs() else None
    low = raw.lower()
    choices = character_choices()
    exact = [int(k) for k, v in choices if v.lower() == low]
    if len(exact) == 1:
        return exact[0]
    part = [int(k) for k, v in choices if low and low in v.lower()]
    return part[0] if len(part) == 1 else None


def reward_label(kind: str, target: int, amount: int) -> str:
    if kind == "character":
        return character_names().get(str(target), str(target))
    if kind == "cubes":
        return f"{amount} Diamond Cubes"
    if kind == "equipment":
        return f"{amount} × {admin.name_of('equipment', target) or target}"
    return f"{amount} × {admin.name_of('items', target) or target}"


# ---------------------------------------------------------------- accounts

def _info(uid: int) -> dict:
    return (userdata.table(uid, "info") or [{}])[0]


def account_summary(uid: int) -> dict:
    info = _info(uid)
    return {"uid": uid, "name": info.get("name", ""), "rank": master.player_rank(info.get("totalExp", 0)),
            "characters": len(userdata.table(uid, "pc"))}


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def link_start(discord_id: str) -> dict:
    current = db.discord_link(discord_id)
    pending = db.discord_link_code(discord_id)
    if pending and time.time() - pending["created_at"] < CODE_SECONDS:
        code = pending["code"]
    else:
        while True:
            code = CODE_PREFIX + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            try:
                db.set_discord_link_code(discord_id, code)
                break
            except db.sqlite3.IntegrityError:  # same code as someone else's, draw again
                continue
        pending = db.discord_link_code(discord_id)
    return {"ok": True, "code": code, "expires_at": pending["created_at"] + CODE_SECONDS,
            "linked": account_summary(current["user_id"]) if current else None}


def link_check(discord_id: str, uid=None) -> dict:
    """Looks for the Discord user's code in the profile comment of `uid`, or of every account."""
    pending = db.discord_link_code(discord_id)
    if not pending or time.time() - pending["created_at"] >= CODE_SECONDS:
        return _err("no_code", "No link code in progress (or it expired): use /link to get a new one.")
    code = pending["code"]
    uids = [int(uid)] if str(uid or "").isdigit() else db.user_ids()
    found = [u for u in uids if db.get_user(u) and code in _norm(_info(u).get("comment", ""))]
    if not found:
        return _err("not_found", f"Code {code} was not found in any profile comment. In game: Profile → edit the "
                                 "comment, paste the code, save, then go back to the Home screen so the game saves "
                                 "it to the server, and try again.")
    if len(found) > 1:
        return _err("ambiguous", "This code is in several accounts' comments: keep it on one account only.")
    owner = db.discord_link_of_user(found[0])
    if owner and owner["discord_id"] != discord_id:
        return _err("taken", "This game account is already linked to another Discord user. Ask the staff.")
    db.set_discord_link(discord_id, found[0])
    return {"ok": True, "account": account_summary(found[0])}


def unlink(discord_id: str) -> dict:
    if not db.delete_discord_link(discord_id):
        return _err("not_linked", "No game account is linked to this Discord user.")
    return {"ok": True}


def whoami(discord_id: str) -> dict:
    link = db.discord_link(discord_id)
    if not link:
        return {"ok": True, "linked": False}
    if not db.get_user(link["user_id"]):
        return {"ok": True, "linked": False}
    return {"ok": True, "linked": True, "linked_at": link["linked_at"], "account": account_summary(link["user_id"])}


# ---------------------------------------------------------------- device transfer
# A player moves the linked account to a new device (BlueStacks -> phone...). Proofs, all required:
#  1. the Discord user is linked to the account (/link already proved control of it);
#  2. a one-time code, issued for that account, is in the profile comment of BOTH accounts at the same time: the old
#     one (control of the account now, not only a past link) and the fresh account the new device created at its
#     first launch (control of the new device);
#  3. that fresh account is new and barely played, and linked to nobody else: nobody's real account can be pushed
#     off its device.
# Then the two accounts swap devices in one transaction (db.swap_devices): the old device gets the empty account.
# Lost old device: staff, admin panel "link". One transfer per account per TRANSFER_COOLDOWN.
TRANSFER_PREFIX = "TR"  # never valid as a /link code (UB...)
TRANSFER_SECONDS = 15 * 60
TRANSFER_COOLDOWN = 7 * 86400
TRANSFER_NEW_MAX_AGE = 7 * 86400  # the new device's account: created at most this long ago...
TRANSFER_NEW_MAX_RANK = 10  # ...and at most this player rank


def transfer_start(discord_id: str) -> dict:
    link = db.discord_link(discord_id)
    if not link or not db.get_user(link["user_id"]):
        return _err("not_linked", "Link your game account first with /link (from the device you play on now).")
    uid = link["user_id"]
    wait = db.last_transfer_at(uid) + TRANSFER_COOLDOWN - time.time()
    if wait > 0:
        return _err("cooldown", f"This account was moved recently: next transfer possible <t:{int(time.time() + wait)}:R>.")
    pending = db.transfer_code(discord_id)
    if pending and pending["user_id"] == uid and time.time() - pending["created_at"] < TRANSFER_SECONDS:
        code = pending["code"]
    else:
        while True:
            code = TRANSFER_PREFIX + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            try:
                db.set_transfer_code(discord_id, uid, code)
                break
            except db.sqlite3.IntegrityError:
                continue
        pending = db.transfer_code(discord_id)
    return {"ok": True, "code": code, "expires_at": pending["created_at"] + TRANSFER_SECONDS,
            "account": account_summary(uid)}


def transfer_check(discord_id: str) -> dict:
    pending = db.transfer_code(discord_id)
    if not pending or time.time() - pending["created_at"] >= TRANSFER_SECONDS:
        return _err("no_code", "No transfer in progress (or the code expired): use /transfer to get a new one.")
    link = db.discord_link(discord_id)
    uid = pending["user_id"]
    if not link or link["user_id"] != uid:
        db.delete_transfer_code(discord_id)
        return _err("not_linked", "Your linked account changed since the code was issued: use /transfer again.")
    code = pending["code"]
    found = [u for u in db.user_ids() if db.get_user(u) and code in _norm(_info(u).get("comment", ""))]
    if uid not in found:
        return _err("old_missing", f"Code {code} is not in the profile comment of your current account ({uid}). "
                                   "On your OLD device: Profile → comment → paste the code → save → Home screen.")
    others = [u for u in found if u != uid]
    if not others:
        return _err("new_missing", f"Code {code} is not in a new account yet. On your NEW device: launch the game "
                                   "once, Profile → comment → paste the code → save → Home screen.")
    if len(others) > 1:
        return _err("ambiguous", "This code is in several new accounts: keep it on one only.")
    new = others[0]
    info = _info(new)
    if time.time() - db.user_created_at(new) > TRANSFER_NEW_MAX_AGE             or master.player_rank(info.get("totalExp", 0)) > TRANSFER_NEW_MAX_RANK:
        return _err("new_not_fresh", f"Account {new} on the new device is not a fresh one (created more than 7 days "
                                     f"ago or above rank {TRANSFER_NEW_MAX_RANK}): ask the staff.")
    owner = db.discord_link_of_user(new)
    if owner and owner["discord_id"] != discord_id:
        return _err("new_linked", f"Account {new} is linked to another Discord user: ask the staff.")
    if db.last_transfer_at(uid) + TRANSFER_COOLDOWN > time.time():
        return _err("cooldown", "This account was moved recently: ask the staff.")
    if not db.devices_of_xuid(str(new)):
        return _err("no_device", f"Account {new} has no device: launch the game on the new device first.")
    db.delete_transfer_code(discord_id)  # one use
    admin.snapshot(uid, "before device transfer")
    admin.snapshot(new, "before device transfer")
    db.swap_devices(uid, new, discord_id)
    print(f"TRANSFER account {uid} -> devices of {new} (Discord {discord_id})", flush=True)
    return {"ok": True, "account": account_summary(uid), "other": new}


# ---------------------------------------------------------------- claims

def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": code, "message": message}


def _character_refusal(uid: int, pc_id: int, ignore_claim: int = 0):
    if str(pc_id) in placeholder_pcs():
        return _err("not_claimable", "This character is an unfinished placeholder of the game and cannot be claimed.")
    if any(p.get("pcId") == pc_id for p in userdata.table(uid, "pc")):
        return _err("owned", "This account already has this character.")
    if any(g.get("rewardType") == 5 and g.get("itemId") == pc_id and g.get("state") == 0
           for g in userdata.table(uid, "gift")):
        return _err("in_gifts", "This character is already waiting in the account's gift box: open it in game.")
    if any(c["user_id"] == uid and c["target"] == pc_id and c["kind"] == "character" and c["id"] != ignore_claim
           for c in db.list_discord_claims(status="pending", limit=100000)):
        return _err("already_claimed", "This character was already claimed for this account and awaits approval.")
    return None


def create_claim(discord_id: str, discord_name: str, kind: str, target=None, amount=1) -> dict:
    link = db.discord_link(discord_id)
    if not link or not db.get_user(link["user_id"]):
        return _err("not_linked", "Link your game account first with /link.")
    uid = link["user_id"]
    if kind not in KINDS:
        return _err("bad_kind", "Unknown reward type.")
    r = user_rules(discord_id)
    try:
        amount = int(amount if amount not in (None, "") else 1)
    except (TypeError, ValueError):
        return _err("bad_amount", "The amount must be a number.")
    if kind == "character":
        pc_id = resolve_character(target or "")
        if pc_id is None:
            return _err("unknown_character", "Unknown character: pick one from the suggestions.")
        target, amount = pc_id, 1
        refusal = _character_refusal(uid, pc_id)
        if refusal:
            return refusal
    elif kind == "cubes":
        target = 0
    elif kind == "equipment":
        raw = str(target or "").strip().split(" ")[0]
        if not raw.isdigit() or not admin.name_of("equipment", raw):
            return _err("unknown_equipment", "Unknown equipment: pick one from the suggestions.")
        if raw in model_only_equipment():
            return _err("not_claimable", "This equipment is a 3D model of the game with no icon or stats: it cannot be "
                                         "claimed.")
        target = int(raw)
    else:
        raw = str(target or "").strip().split(" ")[0]
        if not raw.isdigit() or not admin.name_of("items", raw):
            return _err("unknown_item", "Unknown item id.")
        target = int(raw)
        if target in api.unique_items():
            amount = 1  # guild stamps / icons are owned or not: 11 of one blocked the gift box (tester report, 2026-10-22)
            if any(i.get("itemId") == target and i.get("amount", 0) > 0 for i in userdata.table(uid, "item")):
                return _err("owned", "This account already has it.")
        if target not in api.giftable_items():
            return _err("not_claimable", "This item cannot be claimed (it would crash the gift box): pick one from "
                                         "the suggestions.")
    cap = int((r.get("max_amount") or {}).get(kind, DEFAULT_MAX.get(kind, 9999)))
    if amount < 1 or amount > cap:
        return _err("bad_amount", f"The amount must be between 1 and {cap}.")
    refusal = _check_limits(r, discord_id, kind, amount)
    if refusal:
        return _err("limit", refusal)
    reward_type = 7 if kind == "item" and api.is_imagination(target) else KINDS[kind]  # Imaginarium: rewardType 7
    claim = {"discord_id": discord_id, "discord_name": str(discord_name or "")[:100], "user_id": uid, "kind": kind,
             "reward_type": reward_type, "target": target, "amount": amount,
             "label": reward_label(kind, target, amount), "status": "pending", "auto": 0}
    auto = _auto_approved(r, discord_id, kind, target, amount)
    claim["auto"] = int(auto)
    cid = db.add_discord_claim(claim)
    if auto:
        out = decide(cid, True, "rules", "auto-approval")
        out["auto"] = True
        return out
    return {"ok": True, "auto": False, "claim": claim_view(cid)}


def claim_view(cid: int) -> dict:
    c = db.get_discord_claim(cid)
    if c:
        c["account"] = account_summary(c["user_id"]) if db.get_user(c["user_id"]) else {"uid": c["user_id"]}
    return c


def decide(cid: int, approve: bool, staff_id: str, staff_name: str, note: str = "") -> dict:
    with _decide_lock:
        c = db.get_discord_claim(cid)
        if not c:
            return _err("unknown_claim", f"Claim #{cid} does not exist.")
        if c["status"] != "pending":
            return _err("already_decided", f"Claim #{cid} is already {c['status']}.")
        if not approve:
            db.decide_discord_claim(cid, "refused", staff_id, staff_name, note)
            return {"ok": True, "claim": claim_view(cid)}
        uid = c["user_id"]
        if not db.get_user(uid):
            db.decide_discord_claim(cid, "failed", staff_id, staff_name, "account not found")
            return _err("no_account", f"Account {uid} no longer exists; claim #{cid} marked failed.")
        if c["kind"] == "character":
            refusal = _character_refusal(uid, c["target"], ignore_claim=cid)
            if refusal:  # got it meanwhile (summon, another gift): nothing to grant
                db.decide_discord_claim(cid, "refused", staff_id, staff_name, refusal["message"])
                refusal["claim"] = claim_view(cid)
                return refusal
        try:
            # same as admin.snapshot(), keeping the id for the ledger
            snap = db.add_snapshot(uid, f"discord claim #{cid}: {c['label']}", userdata.load(uid))
            api.add_gift(uid, c["reward_type"], c["target"], c["amount"], 1, int(admin.DEFAULT_REASON))
        except Exception as e:  # noqa: BLE001
            db.decide_discord_claim(cid, "failed", staff_id, staff_name, f"error: {e}"[:300])
            return _err("grant_failed", f"Claim #{cid} could not be granted: {e}")
        db.decide_discord_claim(cid, "approved", staff_id, staff_name, note, snapshot_id=snap)
        return {"ok": True, "claim": claim_view(cid)}


# ---------------------------------------------------------------- HTTP

def handle(method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
    """(HTTP status, JSON response). headers: any case."""
    headers = {k.upper(): v for k, v in headers.items()}
    if not SECRET:
        return 503, {"ok": False, "error": "disabled"}
    auth = headers.get("AUTHORIZATION", "")
    if not hmac.compare_digest(auth.encode(), f"Bearer {SECRET}".encode()):
        return 401, {"ok": False, "error": "unauthorized"}
    path = path.split("?", 1)[0].rstrip("/")
    try:
        req = json.loads(body or b"{}")
        if not isinstance(req, dict):
            raise ValueError
    except ValueError:
        return 400, {"ok": False, "error": "bad_json"}
    did = str(req.get("discord_id", "")).strip()
    if path == "/bot/characters" and method == "GET":
        return 200, {"ok": True, "characters": character_choices()}
    if path == "/bot/equipment" and method == "GET":
        return 200, {"ok": True, "equipment": sorted(((k, v) for k, v in master.NAMES.get("equipment", {}).items()
                                                      if k not in model_only_equipment()), key=lambda x: int(x[0]))}
    if path == "/bot/items" and method == "GET":
        ok = api.giftable_items()
        return 200, {"ok": True, "items": sorted(((k, v) for k, v in master.NAMES.get("items", {}).items()
                                                 if int(k) in ok), key=lambda x: int(x[0]))}
    if method != "POST":
        return 404, {"ok": False, "error": "not_found"}
    m = re.fullmatch(r"/bot/claims/(\d+)/decide", path)
    if m:
        return 200, decide(int(m.group(1)), bool(req.get("approve")), str(req.get("staff_id", "")),
                           str(req.get("staff_name", ""))[:100], str(req.get("note", ""))[:300])
    m = re.fullmatch(r"/bot/claims/(\d+)", path)
    if m:
        c = claim_view(int(m.group(1)))
        return 200, {"ok": True, "claim": c} if c else _err("unknown_claim", "No such claim.")
    if path == "/bot/claims/list":
        return 200, {"ok": True, "claims": db.list_discord_claims(discord_id=did, status=str(req.get("status", "")),
                                                                  limit=min(100, int(req.get("limit") or 20)))}
    if not did.isdigit():
        return 400, {"ok": False, "error": "discord_id required"}
    routes = {
        "/bot/link/start": lambda: link_start(did),
        "/bot/link/check": lambda: link_check(did, req.get("uid")),
        "/bot/unlink": lambda: unlink(did),
        "/bot/whoami": lambda: whoami(did),
        "/bot/transfer/start": lambda: transfer_start(did),
        "/bot/transfer/check": lambda: transfer_check(did),
        "/bot/claims": lambda: create_claim(did, req.get("discord_name", ""), str(req.get("kind", "")),
                                            req.get("target"), req.get("amount", 1)),
    }
    if path not in routes:
        return 404, {"ok": False, "error": "not_found"}
    return 200, routes[path]()


def serve(log=print):
    """Run the bot API in a background thread (called by main.py when SAOUB_BOT_SECRET is set)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 60
        server_version, sys_version = "SAOUB", ""

        def log_message(self, fmt, *args):
            pass

        def _do(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 64 * 1024:
                code, out = 413, {"ok": False, "error": "too_large"}
                self.close_connection = True
            else:
                body = self.rfile.read(length) if length else b""
                try:
                    code, out = handle(self.command, self.path, dict(self.headers), body)
                except Exception as e:  # noqa: BLE001
                    log(f"BOT ERROR {self.path}: {e!r}")
                    code, out = 500, {"ok": False, "error": "internal"}
                if code != 200 or not out.get("ok", True):
                    log(f"BOT {self.command} {self.path} -> {code} {out.get('error', '')}")
                elif self.command == "POST":
                    log(f"BOT POST {self.path}")
            data = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _do

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="botapi").start()
    return srv
