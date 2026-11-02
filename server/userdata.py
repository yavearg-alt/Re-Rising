"""Per-user table storage driven by the client's user/push and user/pull.

The client runs most game logic itself (Lua scripts) and pushes table changes:
  user/push  {"table": [{"trigger": str, "put": {name: [rows]}, "delete": {name: [rows]}, "signature": int}],
              "hash": {"before": {name: md5}, "after": {name: md5}}, "scripts": {...}, "lc": int}
The response body (and any other API response) may carry, at top level, what UserData::storeJson reads:
  {"hash": {name: md5}, "table": {"replace": {name: [rows]}, "put": {...}, "delete": {...}}}
md5 = md5 of the msgpack-encoded table as computed by the client, so we keep the client's "after" hashes.
"""
import hashlib
import json
import os
import threading
import time

import msgpack

import config
import db

_HERE = os.path.dirname(os.path.abspath(__file__))
# API short table name ("info") -> client table name ("UserInfo")
TABLE_NAMES = json.load(open(os.path.join(_HERE, "table_names.json"), encoding="utf-8"))
# Starting account: the archived early-game account (decrypted from the /data/data dump), re-keyed per user.
TEMPLATE_DIR = os.path.join(config.DATA_DIR, "extracted", "sample_user")


def table_hash(name: str, rows: list) -> str:
    """md5 of the msgpack-encoded table exactly as the client computes it.

    Single-row tables are encoded as the object itself, the others as an array; fields must be in the
    declared order (the template JSON keeps it).
    """
    rows = canonical(name, rows)
    value = (rows[0] if rows else []) if name in SINGLE_ROW else rows
    return hashlib.md5(msgpack.packb(value, use_single_float=True)).hexdigest()  # client floats are float32


def _rekey(obj, user_id: int):
    if isinstance(obj, dict):
        return {k: (user_id if k == "userId" else _rekey(v, user_id)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rekey(v, user_id) for v in obj]
    return obj


def seed_from_template(data: dict, user_id: int):
    """Give a brand-new account the archived early-game state, keeping the name the player chose."""
    chosen = (data["tables"].get("info") or [{}])[0].get("name")
    now = int(config.now())
    for short, long_name in TABLE_NAMES.items():
        path = os.path.join(TEMPLATE_DIR, long_name + ".json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        rows = _rekey(value if isinstance(value, list) else [value], user_id)
        if short == "info" and rows:
            rows[0]["name"] = chosen or rows[0]["name"]
            rows[0]["registeredAt"] = rows[0]["lastLoginedAt"] = now
        data["tables"][short] = rows
        data["hash"][short] = table_hash(short, rows)
    data["seeded_at"] = now

FRESH_PCS = (130111001, 130111002)  # Kirito + Eugeo: both fight in the prologue (0-1-1)
FRESH_EMPTY = {  # progress tables a brand-new player has none of
    "area", "enemySpecie", "eventProgress", "gift", "item", "loginBonus", "lotteryInfo", "lotteryPrice",
    "playHistory", "questProgress", "skill", "stageStatus", "systemFlag", "tutorial", "questNovelFlag",
}


# Starter items: the enhance / skill-slot tutorials (after 1-2-3) make the player use exp items and seeds that a
# real account had collected by then (the archived account holds them); without them those tutorials soft-lock.
FRESH_ITEMS = {246000011: 10, 246000013: 3, 246000023: 3, 246000033: 3, 246000043: 3, 246000053: 3, 246000063: 3,
               246000073: 3, 221001011: 10, 221001021: 5, 221001031: 5, 221001041: 5, 221001051: 5, 221001061: 5,
               221001071: 5, 221001081: 2}


def seed_fresh(data: dict, user_id: int):
    """A brand-new player at the very start of the game (no tutorial done, nothing cleared).

    Built from the archived account so every table keeps its shape; single-row tables keep a row (an empty
    replace is ignored by the client), progress is reset and only Kirito and Eugeo (level 1) are owned.
    """
    import master  # noqa: PLC0415 (master imports the decoded game data, only needed here)

    seed_from_template(data, user_id)
    t = data["tables"]
    for name in FRESH_EMPTY:
        if name in t:
            t[name] = []
    t["item"] = [{"userId": user_id, "itemId": i, "amount": n, "reservedAmount": 0, "readState": 0}
                 for i, n in FRESH_ITEMS.items()]
    now = int(config.now())
    info = t["info"][0]
    info.update(totalExp=0, lastGiftId=0, lastEquipmentStockId=101, baseApUpdatedAt=now, homePcId=FRESH_PCS[0],
                profilePcId=FRESH_PCS[0], rankPoint=0, registeredAt=now, lastLoginedAt=now)
    pcs = []
    for pc_id in FRESH_PCS:
        hp, mp = master.PC_STATS.get(str(pc_id), [1000, 140])
        pcs.append({"userId": user_id, "pcId": pc_id, "totalExp": 0, "hp": hp, "mp": mp,
                    # no weapon equipped: the equipment tutorial (100000015) has the player equip one
                    "weaponEquipmentStockId": 0, "armorEquipmentStockId": 0,
                    "badgeEquipmentStockId": 0, "slotSkills": [], "joiningState": 1, "limitBreakCount": 0,
                    "partyEmblemId": 0, "pvpPassiveSkillIds": [], "imaginationSlots": [],
                    "lastImaginationUnlockCount": 0, "levelUpperCount": 0})
    t["pc"] = pcs
    for party in t.get("party", []):
        for slot in party.get("slots", []):
            if slot.get("pcId") not in FRESH_PCS:
                slot["pcId"] = 0
    home = t["homeSetting"][0]
    for slot in home.get("favoriteCardSlots", []) + home.get("favoriteSpineSlots", []):
        slot["pcId"] = FRESH_PCS[0]
    for m in t.get("mission", []):
        m.update(progress=0, state=2, readState=0, completedAt=0, isNotifiable=False, progressUpdatedAt=0)
    for tr in t.get("profileTracker", []):
        tr.update(isOwned=False, value=0, readState=0)
    t["anotherForce"] = [dict(t["anotherForce"][0], isUnlocked=False, gauge=0)]
    t["battleLog"] = [dict(t["battleLog"][0], battleLogId=0, battleLogType=0, unitLog=[])]
    t["questStatus"] = [dict(t["questStatus"][0], questId=0, stageId=0, type=0, startTime=0)]
    t["questResult"] = [dict(t["questResult"][0], questId=0, readState=1,
                             beforeParams={"userTotalExp": 0, "bonusMission": [0, 0, 0]},
                             afterParams={"userTotalExp": 0, "bonusMission": [0, 0, 0]})]
    t["lotteryResult"] = [dict(t["lotteryResult"][0], lotteryPriceId=0, resultItems=[], displayed=True)]
    data["idle_quest_status"] = t["questStatus"][0]
    for name, rows in t.items():
        data["hash"][name] = table_hash(name, rows)
    data["seeded_fresh"] = now


# Primary keys observed in the archived account (msgpack field order: userId, then the key).
PRIMARY_KEYS = {
    "area": ["areaId"], "enemySpecie": ["enemySpecieId"], "equipmentSpecie": ["equipmentSpecieId"],
    "equipmentStock": ["id"], "eventProgress": ["eventId"], "gift": ["id"], "guest": ["guestUserId"], "exchangedItem": ["exchangeOutputItemId"], "pcTowerStatus": ["pcId"], "pvp": ["pvpId"], "pvpNpcParty": ["partyIndex"], "seasonReward": ["seasonRewardId"], "seasonPointBoost": ["seasonPointBoostId"], "premiumPass": ["premiumPassId"], "towerEvent": ["eventId"], "guildNotification": ["guildId"],
    "homeOpening": ["homeOpeningId"], "item": ["itemId"], "loginBonus": ["loginBonusId"],
    "lotteryInfo": ["lotteryId"], "lotteryPrice": ["lotteryPriceId"], "mission": ["missionId"],
    "pc": ["pcId"], "party": ["id"], "playHistory": ["historyType"], "profileTracker": ["profileTrackerId"],
    "questProgress": ["questLevelId"], "skill": ["skillId"], "stageStatus": ["stageId"],
    "supporterPcSlot": ["id"], "systemFlag": ["systemFlagId"], "tutorial": ["tutorialId"],
}
# Tables stored as a single object by the client (LevelDB value is a map, not an array).
SINGLE_ROW = {
    "anotherForce", "boostSetting", "battleLog", "homeSetting", "info", "lotteryResult", "position",
    "pvpResult", "pvpStatus", "questResult", "questSetting", "questStatus", "questSupporterSlot",
    "raidEventWorldBossDailyResult", "raidEventWorldBossDefeatResult", "rushEventStatus", "setting",
    "towerEventStatus",
}

def _order_tree(row: dict) -> dict:
    """Field order of a reference row, with the order of nested objects (first element of lists)."""
    nested = {}
    for k, v in row.items():
        if isinstance(v, dict) and v:
            nested[k] = _order_tree(v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            nested[k] = _order_tree(v[0])
    return {"fields": list(row), "nested": nested}


def _load_field_order() -> dict:
    """Client field order per table: the archived account (written by the client itself, in its declared order),
    then table_fields_extra.json (client serializers) for tables it never had."""
    order = {}
    for short, long_name in TABLE_NAMES.items():
        path = os.path.join(TEMPLATE_DIR, long_name + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                value = json.load(f)
            rows = value if isinstance(value, list) else [value]
            if rows and isinstance(rows[0], dict):
                order[short] = _order_tree(rows[0])
    with open(os.path.join(_HERE, "table_fields_extra.json"), encoding="utf-8") as f:
        for name, fields in json.load(f).items():
            if not name.startswith("_") and name not in order:
                order[name] = {"fields": fields, "nested": {}}
    return order


FIELD_ORDER = _load_field_order()


def _reorder(value, tree: dict):
    if isinstance(value, list):
        return [_reorder(v, tree) for v in value]
    if not isinstance(value, dict):
        return value
    fields = tree["fields"]
    keys = [k for k in fields if k in value] + [k for k in value if k not in fields]
    nested = tree["nested"]
    return {k: (_reorder(value[k], nested[k]) if k in nested else value[k]) for k in keys}


def canonical(name: str, rows: list) -> list:
    """Rows with their fields in the client's order.

    The client sends user/push as JSON with alphabetically sorted keys (nlohmann::json over std::map) but
    hashes its tables in its declared field order: rows stored as pushed gave hashes the client never matched.
    """
    tree = FIELD_ORDER.get(name)
    return [_reorder(r, tree) for r in rows] if tree else rows


_lock = threading.Lock()


def _pk(name: str, row: dict):
    keys = PRIMARY_KEYS.get(name)
    if keys is None:
        if "id" in row:
            keys = ["id"]
        else:
            keys = sorted(k for k in row if k.endswith("Id") and k != "userId")[:1]
    return tuple(row.get(k) for k in keys) if keys else None


def _apply(tables: dict, name: str, put_rows: list, delete_rows: list):
    rows = tables.get(name, [])
    if name in SINGLE_ROW or (put_rows and _pk(name, put_rows[0]) is None):
        if put_rows:
            rows = [put_rows[-1]]
    else:
        index = {_pk(name, r): i for i, r in enumerate(rows)}
        for r in put_rows:
            k = _pk(name, r)
            if k in index:
                rows[index[k]] = r
            else:
                index[k] = len(rows)
                rows.append(r)
        if delete_rows:
            gone = {_pk(name, r) for r in delete_rows}
            rows = [r for r in rows if _pk(name, r) not in gone]
    if name in SINGLE_ROW and delete_rows:
        rows = []
    tables[name] = rows


def load(user_id: int) -> dict:
    u = db.get_user(user_id)
    if u is None:
        return {"tables": {}, "hash": {}}
    data = u[1]
    data.setdefault("tables", {})
    data.setdefault("hash", {})
    return data


MAX_ACCOUNT_BYTES = 8 * 1024 * 1024


def push(user_id: int, body: dict) -> dict:
    """Apply a user/push body; returns the top-level fields to merge into the response."""
    with _lock:
        data = load(user_id)
        for change in body.get("table") or []:
            puts = change.get("put") or {}
            deletes = change.get("delete") or {}
            for name in set(puts) | set(deletes):
                _apply(data["tables"], name, canonical(name, puts.get(name) or []), deletes.get(name) or [])
        after = (body.get("hash") or {}).get("after") or {}
        data["hash"].update(after)
        data["updated_at"] = int(time.time())
        size = len(json.dumps(data, separators=(",", ":")))
        if size > MAX_ACCOUNT_BYTES:
            # the biggest real account is under 1 MB: a client filling the database is refused, nothing is saved
            return {"code": 413}
        db.save_user(user_id, data)
    return {"hash": after}


# Tables the client changes locally without a push of their own: UserSetting (battle x2 speed, "Use Active Link
# during Auto"...) only rides along other pushes, so at the next boot its hash differed and user/pull sent the
# server's stale copy back, resetting the player's choices (tester report, 2026-10-17). The client's copy wins, except
# when it is the default row (reinstall / new device): then the saved one is restored.
# UserHomeSetting likewise: home customization and the card illustrations (favoriteCardSlots.limitBreakCount) came
# back after a reboot (tester report, 2026-10-17; one account's pulls never matched the server's hash in a whole day).
CLIENT_OWNED = {"setting", "homeSetting"}


def default_hash(name: str, user_id: int) -> str:
    """Hash of the archived account's row for this table, re-keyed for the player (the client's defaults)."""
    path = os.path.join(TEMPLATE_DIR, f"{TABLE_NAMES[name]}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return table_hash(name, canonical(name, _rekey(json.load(f), user_id)))
    except (OSError, ValueError, KeyError):
        return ""


def pull(user_id: int, body: dict) -> dict:
    """user/pull {"hash": {name: client_md5}} -> tables whose hash differs from ours."""
    data = load(user_id)
    if user_id and "seeded_at" not in data and not data["tables"].get("pc"):
        with _lock:
            if config.NEW_ACCOUNT == "fresh":
                seed_fresh(data, user_id)
            else:
                seed_from_template(data, user_id)
            db.save_user(user_id, data)
    client = body.get("hash") or {}
    replace, hashes = {}, {}
    for name in data["hash"]:
        if name not in client or name not in data["tables"]:
            continue  # the client may pull only some tables (e.g. {"gift": ...} after mission/receive)
        # hash of what we hold, not the stored one: older server versions stored hashes of rows in the
        # pushed (alphabetical) field order, which the client never matches -- recomputing heals them
        server_hash = table_hash(name, data["tables"][name])
        if (name in CLIENT_OWNED and client[name] and client[name] != server_hash
                and client[name] != default_hash(name, user_id)):
            continue
        if client[name] != server_hash:
            replace[name] = canonical(name, data["tables"][name])
            hashes[name] = server_hash
    out = {"hash": hashes} if hashes else {}
    if replace:
        out["table"] = {"replace": replace}
    return out


def set_tables(user_id: int, changes: dict) -> dict:
    """Server-side table changes {name: rows}; stored and returned as the response's hash/table.replace."""
    with _lock:
        data = load(user_id)
        hashes = {}
        changes = {name: canonical(name, rows) for name, rows in changes.items()}
        for name, rows in changes.items():
            data["tables"][name] = rows
            hashes[name] = data["hash"][name] = table_hash(name, rows)
        db.save_user(user_id, data)
    return {"hash": hashes, "table": {"replace": changes}}


def area_rows_with(user_id: int, area_ids) -> list | None:
    """UserArea must contain every area the player enters, otherwise the field renders black.

    Returns the updated UserArea rows, or None when nothing was missing. New areas are fully revealed.
    """
    rows = list(table(user_id, "area"))
    have = {r.get("areaId") for r in rows}
    missing = [a for a in dict.fromkeys(area_ids) if a and a not in have]
    if not missing:
        return None
    for a in missing:
        rows.append({"userId": user_id, "areaId": a, "lineBlockDwords": [0xFFFFFFFF] * 8, "openupLayers": []})
    return rows


def quest_areas(user_id: int, add=None, clear=False):
    """Remember which UserArea rows the server added for the running quest (to remove them afterwards)."""
    with _lock:
        data = load(user_id)
        current = data.get("quest_areas", [])
        if clear:
            data["quest_areas"] = []
        if add:
            data["quest_areas"] = sorted(set(current) | set(add))
        db.save_user(user_id, data)
    return current


def idle_quest_status(user_id: int, save: dict | None = None) -> dict:
    """The questStatus row to restore when no quest is running.

    questStatus is a single-row table: the client ignores a replace with no row and keeps believing the quest
    is running (it then boots into a black, empty quest field). The archived account shows the idle shape:
    the last finished quest with type 4. quest/start saves that row, quest/retire puts it back.
    """
    with _lock:
        data = load(user_id)
        if save is not None:
            data["idle_quest_status"] = save
            db.save_user(user_id, data)
        row = data.get("idle_quest_status")
    if row is None:
        with open(os.path.join(TEMPLATE_DIR, "UserQuestStatus.json"), encoding="utf-8") as f:
            row = _rekey(json.load(f), user_id)
    return row


def gems(user_id: int) -> int:
    """Free diamond cubes. The client reads the balance from GREE payment/balance, not from a user table."""
    return int(load(user_id).get("free_gems", config.START_FREE_GEMS))


def add_gems(user_id: int, amount: int) -> int:
    with _lock:
        data = load(user_id)
        data["free_gems"] = int(data.get("free_gems", config.START_FREE_GEMS)) + amount
        db.save_user(user_id, data)
        return data["free_gems"]


def counter(user_id: int, key: str, default: int = 0) -> int:
    return int(load(user_id).get(key, default))


def set_counter(user_id: int, key: str, value: int):
    with _lock:
        data = load(user_id)
        data[key] = int(value)
        db.save_user(user_id, data)


def add_counter(user_id: int, key: str, amount: int) -> int:
    """Server-side counters with no known client table yet (e.g. season points)."""
    with _lock:
        data = load(user_id)
        data[key] = int(data.get(key, 0)) + amount
        db.save_user(user_id, data)
        return data[key]



TIME_KEYS = ("At", "Time", "startTime")


def clamp_times(user_id: int) -> int:
    """Bring timestamps written with the real clock back to the game clock (config.now()): anything later than
    the game's "now" (e.g. 2026 values once the game clock runs in 2022) would freeze AP regen, daily limits..."""
    limit = int(config.now())
    changed = 0

    def fix(obj):
        nonlocal changed
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, int) and not isinstance(v, bool) and k.endswith(TIME_KEYS) and v > limit:
                    obj[k] = limit
                    changed += 1
                else:
                    fix(v)
        elif isinstance(obj, list):
            for v in obj:
                fix(v)

    with _lock:
        data = load(user_id)
        for name, rows in data["tables"].items():
            before = changed
            fix(rows)
            if changed != before:
                data["hash"][name] = table_hash(name, rows)
        db.save_user(user_id, data)
    return changed


def table(user_id: int, name: str) -> list:
    return load(user_id)["tables"].get(name, [])


def dump(user_id: int) -> str:
    return json.dumps(load(user_id), ensure_ascii=False, indent=1)
