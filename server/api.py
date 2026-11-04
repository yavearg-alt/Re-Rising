"""Game API. For now: log + decrypt every request, answer with a generic success envelope.

Endpoints get real implementations as the protocol is reverse engineered (see docs/PROTOCOL.md).
"""
import hmac
import json
import os
import random
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from saoub_crypto import decrypt, encrypt  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import master  # noqa: E402
import userdata  # noqa: E402
import guild  # noqa: E402
import missions  # noqa: E402
import raid  # noqa: E402


DEV_TOKEN = os.environ.get("SAOUB_DEV_TOKEN", "")  # tools and the Unity port (no device id): SAOAL-DEV-TOKEN


def _ad_id(headers: dict) -> str:
    ad = headers.get(P + "AD-ID", "") or ""
    return "" if not ad.replace("0", "").replace("-", "") else ad  # all zeros = ad tracking off


def authorize(uid: int, headers: dict) -> str | None:
    """None if this request may act on account `uid`, else the reason. SAOAL-USER-ID alone is sequential and
    trivially forged, so each account is bound to two ids of the device that plays it: SAOAL-PLATFORM-USER-ID
    (changes when the game is reinstalled) and SAOAL-AD-ID (survives a reinstall). One of them must match; the
    binding then follows the other, so a reinstall keeps the account. A new phone needs the admin panel's "link"."""
    if not uid:
        return None
    token = headers.get(P + "DEV-TOKEN", "")
    if DEV_TOKEN and token and hmac.compare_digest(token, DEV_TOKEN):
        return None
    platform, ad = headers.get(P + "PLATFORM-USER-ID", "") or "", _ad_id(headers)
    if not platform and not ad:
        return "no device id"
    bound = db.binding(uid)
    if bound is None:
        db.set_binding(uid, platform, ad)
        return None
    same_platform = bool(platform and bound[0]) and hmac.compare_digest(platform, bound[0])
    same_ad = bool(ad and bound[1]) and hmac.compare_digest(ad, bound[1])
    if not (same_platform or same_ad):
        return f"account bound to another device ({platform[:6]}.../{ad[:8]})"
    if (platform or bound[0], ad or bound[1]) != bound:
        db.set_binding(uid, platform or bound[0], ad or bound[1])
    return None


def seed_platforms_from_logs(log):
    """Bind each existing account to the device ids found in the request logs (so nobody can claim an account
    before its owner plays again), and complete older bindings that have no advertising id yet."""
    missing = dict(db.bindings_without_ad())
    if db.platform_count() and not missing:
        return
    seen = {}
    for name in ("api_requests.jsonl.1", "api_requests.jsonl"):
        path = os.path.join(config.LOG_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    h = json.loads(line).get("headers") or {}
                except ValueError:
                    continue
                uid, platform = h.get(P + "USER-ID"), h.get(P + "PLATFORM-USER-ID")
                if uid and platform and uid.isdigit():
                    key = (platform, _ad_id(h))
                    seen.setdefault(int(uid), {}).setdefault(key, 0)
                    seen[int(uid)][key] += 1
    done = 0
    for uid, pairs in seen.items():
        bound = db.binding(uid)
        if bound is None:
            platform, ad = max(pairs, key=pairs.get)  # most used device
        elif not bound[1]:
            ads = {a: n for (p, a), n in pairs.items() if p == bound[0] and a}
            if not ads:
                continue
            platform, ad = bound[0], max(ads, key=ads.get)
        else:
            continue
        db.set_binding(uid, platform, ad)
        done += 1
    log(f"device binding: {done} account(s) bound or completed from the request logs")


def _user_id(headers: dict) -> int:
    try:
        return int(headers.get(P + "USER-ID") or 0)
    except ValueError:
        return 0


def ep_user_push(uid, req):
    return userdata.push(uid, req or {})


def ep_user_pull(uid, req):
    # guild tables are shared: rebuild them before the client compares hashes (new chat, members...)
    if uid and any(name.startswith("guild") for name in ((req or {}).get("hash") or {})):
        guild.refresh_silently(uid)
    return userdata.pull(uid, req or {})


def tower_status(uid: int, event_id: int):
    """The UserTowerEventStatus row when `event_id` is the tower event the client initialized, else None."""
    row = (userdata.table(uid, "towerEventStatus") or [None])[0]
    return dict(row) if row and event_id and row.get("eventId") == event_id else None


TOWER_INITIAL, TOWER_PLAYING, TOWER_CLEARED = 0, 1, 2  # Enum_UserTowerEventState


def tower_previous(floors: list, status: dict) -> dict:
    """State fields for "the floor before the running one is the last cleared" (or the level's start)."""
    same = [fid for fid, lv, _ in floors if lv == status.get("towerLevel")]
    floor_id = status.get("towerEventQuestId", 0)
    i = same.index(floor_id) if floor_id in same else 0
    return {"state": TOWER_CLEARED, "towerEventQuestId": same[i - 1]} if i > 0 \
        else {"state": TOWER_INITIAL, "towerEventQuestId": 0}


def tower_repair(uid: int):
    """Accounts that cleared floors before 2026-10-17 got towerLevel 1 + floors cleared, state 0 (see the clear
    branch of tower_changes): give them back NORMAL with that many floors cleared. Once per account."""
    row = (userdata.table(uid, "towerEventStatus") or [None])[0]
    if not row or userdata.counter(uid, "tower_repaired") or row.get("state") != TOWER_INITIAL \
            or (row.get("towerLevel") or 0) <= 1:
        return
    userdata.add_counter(uid, "tower_repaired", 1)
    normal = [fid for fid, lv, _ in master.TOWER_FLOORS.get(str(row.get("eventId")), []) if lv == 1]
    cleared = min(row["towerLevel"] - 1, len(normal))
    if cleared:
        userdata.set_tables(uid, {"towerEventStatus": [dict(row, towerLevel=1, state=TOWER_CLEARED,
                                                            towerEventQuestId=normal[cleared - 1])]})


def tower_floor_rewards_backfill(uid: int):
    """The Cathedral floors cleared before their first-clear reward was given (2026-10-27): to the gift box, once."""
    cleared = {r.get("questLevelId") for r in userdata.table(uid, "questProgress") if r.get("clearCount")}
    data = userdata.load(uid)
    gifts = None
    for quest_id, group in master.TOWER_FLOOR_REWARDS.items():
        ql = master.QUEST_LEVELS.get(f"{quest_id}:1", {})
        if ql.get("id") in cleared and not data.get(f"tower_reward_{quest_id}"):
            userdata.set_counter(uid, f"tower_reward_{quest_id}", 1)
            gifts = add_gifts(uid, gifts if gifts is not None else list(userdata.table(uid, "gift")), group)
    if gifts is not None:
        userdata.set_tables(uid, {"gift": gifts})


def tower_changes(uid: int, req: dict, phase: str) -> dict:
    """Cathedral (tower event) bookkeeping around a floor quest. Tables (client serializers):
    UserTowerEventStatus (eventId, towerLevel, state, towerEventQuestId, ...), UserTowerEvent (eventId, maxFloor),
    UserPCTowerStatus (pcId, hp, mp, dispatchedCount) -- hp/mp carry over between floors."""
    setting = (userdata.table(uid, "questSetting") or [{}])[0]
    event_id = int(req.get("eventId") or setting.get("eventId") or 0)
    status = tower_status(uid, event_id)
    if status is None:
        return {}
    changes = {}
    party_id = int(req.get("partyId") or setting.get("partyId") or 0)
    party = next((p for p in userdata.table(uid, "party") if p.get("id") == party_id), {"slots": []})
    members = [sl["pcId"] for sl in party.get("slots", []) if sl.get("pcId")]
    pcs = {p["pcId"]: p for p in userdata.table(uid, "pc")}
    tower_pcs = {r["pcId"]: dict(r) for r in userdata.table(uid, "pcTowerStatus")}
    floors = master.TOWER_FLOORS.get(str(event_id), [])
    if phase == "start":
        quest_id = int(req.get("questId") or 0)
        # the TowerEventQuest id of the floor (601600003001), not the quest id (8014033001): the field looks the
        # floor up by it, found none and stayed in an empty black area (Cathedral, 2026-10-17)
        floor_id = master.TOWER_EVENT_QUESTS.get(f"{event_id}:{quest_id}", quest_id)
        level = next((lv for fid, lv, _ in floors if fid == floor_id), status.get("towerLevel") or 1)
        status.update(towerEventQuestId=floor_id, towerLevel=level, state=TOWER_PLAYING)
        for pc_id in members:
            if pc_id not in tower_pcs:
                hp, mp = master.PC_STATS.get(str(pc_id), [1000, 140])
                tower_pcs[pc_id] = {"userId": uid, "pcId": pc_id, "hp": pcs.get(pc_id, {}).get("hp", hp),
                                    "mp": pcs.get(pc_id, {}).get("mp", mp), "dispatchedCount": 0}
    elif phase == "clear":
        # QUEST_CLEAR keeps the cleared floor: the client then unlocks the next floor of the same towerLevel
        # (FUN_0132310e -> FUN_013232c8). towerLevel is the difficulty (Enum_TowerLevel: 1 normal, 2 hard...), not
        # the floor: towerLevel + 1 with state 0 pointed at the first HARD floor and floor 2 stayed locked
        # (tester report, 2026-10-17)
        floor_id = status.get("towerEventQuestId", 0)
        top = next((top for fid, _, top in floors if fid == floor_id), 0)
        status.update(state=TOWER_CLEARED)
        events = {r["eventId"]: dict(r) for r in userdata.table(uid, "towerEvent")}
        ev = events.setdefault(event_id, {"userId": uid, "eventId": event_id, "maxFloor": 0})
        ev["maxFloor"] = max(ev["maxFloor"], top)
        changes["towerEvent"] = list(events.values())
        for pc_id in members:
            if pc_id in tower_pcs:
                tower_pcs[pc_id]["dispatchedCount"] += 1
    else:  # retire: back to the floor cleared before this one (state 0 = the level's first floor)
        status.update(lostCount=status.get("lostCount", 0) + 1, **tower_previous(floors, status))
    changes["towerEventStatus"] = [status]
    changes["pcTowerStatus"] = list(tower_pcs.values())
    return changes


def stage_filter_type(setting: dict) -> int:
    """UserStageStatus.filterType of the quest's stages (Enum_UserStageFilterType: 0 normal, 1 tower, 2 story event
    boss, 3 rush). The client builds the running quest's stage list from the stageStatus rows whose filterType is
    the quest's category (FUN_0125e830, category FUN_01275b78(questSetting): event type 9 -> 3, 3 -> 1, a raid
    event with a raid boss -> 0, an event boss quest -> 2, else 0). With 0 a Cathedral floor (type 3) had no stage:
    no boss encounter, empty black field (2026-10-17)."""
    event_type = master.EVENT_TYPES.get(str(setting.get("eventId") or 0), 0)
    if event_type in (9, 3):
        return {9: 3, 3: 1}[event_type]
    # event boss quests (event.bin sub 2 field 2 = 2): FUN_012757e8 -> 2. They got 0, so the boss battle started
    # with no stage, like the Cathedral floors: black screen (tester report, 2026-10-17)
    if f"{setting.get('eventId') or 0}:{setting.get('questId') or 0}" in master.EVENT_BOSS_QUESTS:
        return 2
    return 0


# AP (client FUN_0122eb1c): current = baseAp + (now - baseApUpdatedAt) / 180 s while baseAp <= the rank's max AP
# (base.bin sub 11 756001006 = 180), capped at that max; above it (recovery items, up to 999) it does not grow.
AP_SECONDS = 180


def current_ap(info: dict, now: int) -> tuple:
    """(current AP, baseApUpdatedAt to keep the partial recovery) at game time `now`."""
    base, updated = int(info.get("baseAp") or 0), int(info.get("baseApUpdatedAt") or 0)
    top = master.PLAYER_MAX_AP.get(str(master.player_rank(info.get("totalExp", 0))), 0)
    if base >= top:
        return base, now
    gained = max(0, (now - updated) // AP_SECONDS)  # the game clock loops: never negative
    if base + gained >= top:
        return top, now
    return base + gained, updated + gained * AP_SECONDS


def spend_ap(uid: int, quest_id: int, level: int) -> list:
    """Take the quest level's AP cost (quest.bin sub 3 field 5) at quest start: the server never took it, so
    quests were free and AP stayed at its max (a tester: "my AP remains constant at 98", 2026-10-18). Never below 0
    and never refused: the client checks the AP itself before starting. Returns the info row to send, or []."""
    cost = master.QUEST_LEVELS.get(f"{quest_id}:{level}", {}).get("ap", 0)
    info = dict((userdata.table(uid, "info") or [{}])[0])
    if not cost or not info:
        return []
    now = int(config.now())
    ap, updated = current_ap(info, now)
    info.update(baseAp=max(0, ap - cost), baseApUpdatedAt=updated)
    return [info]


def ep_quest_start(uid, req):
    """Client reads UserQuestStatus right after (FUN_0126099c); without it, it goes back home."""
    req = req or {}
    quest_id = int(req.get("questId", 0))
    stages = master.QUEST_STAGES.get(str(quest_id), [])
    event_id = int(req.get("eventId", 0))
    now = int(config.now())
    status = {
        "userId": uid, "questId": quest_id, "eventId": event_id, "level": int(req.get("level", 1)),
        "type": 1, "elapsedTime": 0.0, "stageId": stages[0] if stages else 0, "continueCount": 0,
        "battleCount": 0, "startTime": now, "activeLinkCount": 0,
    }
    # boost items (x2 EXP/Col/drops) are used up, one per quest started; they were never taken away. One the
    # player does not own is dropped (no free boost).
    boosts = Grant(uid)
    boost_ids = []
    for bid in req.get("boostItemIds") or []:
        bid = int(bid)
        if bid not in boost_ids and boosts.items.get(bid, {}).get("amount", 0) > 0:
            boosts.item(bid, -1)
            boost_ids.append(bid)
    # Ticket Boss quests cost their boss tickets (event.bin sub 2 fields 5 / 6): never taken, the count never went
    # down (tester report, 2026-10-21). The client checks it has enough before starting; never below 0.
    ticket = master.EVENT_BOSS_TICKETS.get(f"{status['eventId']}:{quest_id}")
    ticket_id = master.EXCHANGE["labels"].get(ticket[0]) if ticket else None
    if ticket_id:
        boosts.item(ticket_id, -min(int(ticket[1]), boosts.items.get(ticket_id, {}).get("amount", 0)))
    setting = {
        "userId": uid, "questId": quest_id, "level": status["level"], "partyId": int(req.get("partyId", 0)),
        "supporterUserId": int(req.get("supporterUserId", 0)), "supporterPcId": int(req.get("supporterPcId", 0)),
        "boostItemIds": boost_ids, "eventId": status["eventId"], "raidEventBossId": 0,
        "bonusCampaignId": [],
    }
    # UserStageStatus holds the running quest's stages, one row per stage (a leftover row from another quest sent
    # the client to that quest's stage). Without rows for the later stages the field's exit door does nothing.
    stage_status = []
    # several stages can share an area: its chests belong to one of them, the first one the player explores. A
    # story stage of the area (no field setup: a scene, no exploration) can't be it -- the chests given to it could
    # never be opened and the "open every chest" mission stayed undone (event quest 8013001002, stages 201/205).
    chest_stage = {}
    for sid in stages:
        area = master.STAGE_AREA.get(str(sid))
        explorable = bool(master.STAGE_ENCOUNTS.get(f"{sid}:{status['level']}")
                          or master.STAGE_ENCOUNTS.get(f"{sid}:1"))
        if area not in chest_stage or (explorable and not chest_stage[area][1]):
            chest_stage[area] = (sid, explorable)
    filter_type = stage_filter_type(dict(setting, eventId=event_id))
    for sid in stages:
        normal, boss = stage_encounts(sid, status["level"])
        area = master.STAGE_AREA.get(str(sid))
        treasures = stage_treasures(sid, area, status["level"]) if chest_stage[area][0] == sid else []
        stage_status.append({
            "userId": uid, "stageId": sid, "level": status["level"], "encountCount": 0, "clear": False,
            "elapsedTime": 0.0, "treasureStatus": treasures, "encountStatus": normal, "bossEncountStatus": boss,
            "areaEnemyEncountStatus": [], "filterType": filter_type, "questId": quest_id,
            "eventId": status["eventId"],
        })
    boost_enemies(stage_status, boost_ids)
    current = (userdata.table(uid, "questStatus") or [None])[0]
    if current and current.get("type") != 1:
        # a cleared row (type 2) the client never pushed back as 4: retire must not restore "cleared"
        userdata.idle_quest_status(uid, save=dict(current, type=4) if current.get("type") == 2 else current)
    changes = {"questStatus": [status], "questSetting": [setting], "stageStatus": stage_status,
               "questSupporterSlot": [supporter_slot(uid, req)], **tower_changes(uid, req, "start"),
               **boosts.changes()}
    healed = healed_pcs(uid)
    if healed:
        changes["pc"] = healed
    gauge = rested_gauge(uid)
    if gauge:
        changes["anotherForce"] = gauge
    info = spend_ap(uid, quest_id, status["level"])
    if info:
        changes["info"] = info
    stage_areas = [master.STAGE_AREA.get(str(s)) for s in stages]
    have = {r.get("areaId") for r in userdata.table(uid, "area")}
    areas = userdata.area_rows_with(uid, stage_areas)
    if areas is not None:
        changes["area"] = areas
        userdata.quest_areas(uid, add=[a for a in stage_areas if a and a not in have])
    return userdata.set_tables(uid, changes)


# Quest boost items (item.bin sub 12, field 4 = kind): x2 Col, x2 EXP, x2 items (not the chests), x2 event items,
# battle skip (quest/skip). The client takes each enemy's experience / gold / drops from stageStatus as the server
# wrote them and applies no boost itself (VPS log, 2026-10-17: the same +28 character EXP with and without x2 EXP),
# so the server doubles them, and the quest clear gold and drops at quest/clear.
BOOST_GOLD, BOOST_EXP, BOOST_ITEM, BOOST_EVENT = 249000001, 249000002, 249000003, 249000004


def boost_enemies(stage_status: list, boost_ids: list):
    for row in stage_status:
        for enc in row["encountStatus"] + row["bossEncountStatus"]:
            for enemy in enc.get("enemyStatus", []):
                if BOOST_EXP in boost_ids:
                    enemy["experience"] *= 2
                if BOOST_GOLD in boost_ids:
                    enemy["gold"] *= 2
                if BOOST_ITEM in boost_ids:
                    enemy["rewardGroupIds"] = enemy["rewardGroupIds"] * 2


def healed_pcs(uid: int) -> list | None:
    """UserPC with every character back to full hp/mp, or None if nobody was hurt. The client keeps the damage
    taken in a quest (hp/mp pushed after each battle) and never heals between quests itself: a new quest started
    with what was left (Kirito at 297/1028). The archived account rests at full hp. Tower runs use pcTowerStatus."""
    rows, changed = [], False
    for p in userdata.table(uid, "pc"):
        hp, mp = master.pc_max_hp_mp(p["pcId"], p.get("totalExp", 0))
        if p.get("hp", hp) < hp or p.get("mp", mp) < mp:
            p = dict(p, hp=max(p.get("hp", 0), hp), mp=max(p.get("mp", 0), mp))
            changed = True
        rows.append(p)
    return rows if changed else None


def enemy_drops(tier: int, quest_level: int, base_id: int) -> list:
    """Reward groups this enemy will drop: for each drop kind of the stage tier (seed, seed, exp item), the row of
    the enemy's element (last digit of its enemyBaseId, 1 none .. 7 dark) rolls its own chance."""
    table = master.ENEMY_DROPS
    tiers = sorted({int(k.split(":")[0]) for k in table})
    tier = max([t for t in tiers if t <= tier], default=tiers[0])
    kinds = table.get(f"{tier}:{quest_level}") or table.get(f"{tier}:1") or {}
    element = base_id % 10 if 1 <= base_id % 10 <= 7 else 1
    drops = []
    for rows in kinds.values():
        chance, group = rows[min(element, len(rows)) - 1]
        if group and random.random() < chance / 10000:
            drops.append(group)
    return drops


def encount(index: int, party_id: int, level: int, move: int = 0, tier: int = 0, quest_level: int = 1,
            boss: bool = False) -> dict:
    """One UserStageEncountStatus (19 fields, order from the client's serializer FUN_014d007c).
    The server decided the enemies and their rewards in advance; the client fights them locally."""
    party = master.ENEMY_PARTIES.get(str(party_id), {})
    bases = party.get("bases") or []
    # enemy levels come from the party itself; the stage's level fields (10/10 in the tutorial, 800 in an event
    # stage) gave rows the client never used, so hp bars overflowed. `level` still drives drops (tier).
    levels = party.get("levels") or [level] * len(bases)
    n = max(len(bases), 1)
    enemies = [{
        "enemyIndex": i, "enemyBaseId": b, "level": levels[i], "hp": master.enemy_hp(b, levels[i]),
        "rewardGroupIds": enemy_drops(tier or level, quest_level, b),
        "experience": party.get("exp", 0) // n, "gold": party.get("gold", 0) // n, "dropped": False, "getAt": 0,
    } for i, b in enumerate(bases)]
    return {
        "encountIndex": index, "encountMove": move, "encountStatusType": 0, "enemyPartyId": party_id,
        "enemyStatus": enemies, "turnCount": 0, "healCount": 0, "deathCount": 0, "battleWinRank": 0,
        "incarnateCount": 0, "continueCount": 0, "bossLastAttackPcId": 0, "encountTime": 0, "isRewarded": False,
        "frontPcSlotNumbers": [], "pastTotalTurnCount": 0, "enemyDamagePoint": 0, "playerDamagePoint": 0,
        "localTurnCount": 0,
    }


def stage_encounts(stage_id: int, quest_level: int):
    """(encountStatus, bossEncountStatus) for a stage: random parties from its group + its boss party."""
    setup = master.STAGE_ENCOUNTS.get(f"{stage_id}:{quest_level}") or master.STAGE_ENCOUNTS.get(f"{stage_id}:1")
    if not setup:
        return [], []
    lo, hi = setup["level"]
    normal, boss = [], []
    parties = master.PARTIES_BY_GROUP.get(setup["group"], [])
    if parties:
        weights = [master.ENEMY_PARTIES[str(p)]["weight"] for p in parties]
        for i in range(setup["count"]):
            normal.append(encount(i, random.choices(parties, weights)[0], random.randint(lo, hi),
                                  random.randint(*setup["move"]), setup.get("tier", 0), quest_level))
    if setup["label"] in master.PARTY_BY_LABEL:
        boss.append(encount(0, master.PARTY_BY_LABEL[setup["label"]], hi, 0, setup.get("tier", 0), quest_level,
                            True))
    return normal, boss


S_RANK = 1  # battleWinRank / bossClearRank: 1 = S (the archived account: bossClearRank 1 with every mission done)


def quest_battles(uid: int, quest_id: int) -> tuple:
    """(normal battles, boss battles) fought in the quest, from the stageStatus rows the client pushed."""
    normal, boss = [], []
    for st in userdata.table(uid, "stageStatus"):
        if st.get("questId") != quest_id:
            continue
        normal += [e for e in st.get("encountStatus") or [] if e.get("battleWinRank") or e.get("isRewarded")]
        boss += [e for e in st.get("bossEncountStatus") or [] if e.get("battleWinRank") or e.get("isRewarded")]
    return normal, boss


def bonus_mission_results(uid: int, quest_id: int, mission_ids: list, setting: dict) -> list:
    """1/0 per bonus mission of the quest level (quest.bin sub 7 types), judged from the quest's battles."""
    normal, boss = quest_battles(uid, quest_id)
    fought = normal + boss
    party = next((p for p in userdata.table(uid, "party") if p.get("id") == setting.get("partyId")), {})
    pcs = {p["pcId"]: p for p in userdata.table(uid, "pc")}
    members = [sl["pcId"] for sl in party.get("slots") or [] if sl.get("pcId") in pcs]
    characters = {master.PC_CHARACTER.get(str(pc), 0) for pc in members}
    hp = sum(max(0, int(pcs[pc].get("hp") or 0)) for pc in members)
    max_hp = sum(master.pc_max_hp_mp(pc, pcs[pc].get("totalExp", 0))[0] for pc in members) or 1
    # chests stay open once opened (the client keeps them in UserStage.treasure, keyed by stageLevelId, and shows them
    # open on the next runs): a chest counts if opened in this run or in an earlier one
    opened_before = {}
    for row in userdata.table(uid, "stage"):
        opened_before[row.get("stageLevelId")] = {t.get("position") for t in row.get("treasure") or []
                                                  if t.get("dropCount")}
    treasures = []
    for st in userdata.table(uid, "stageStatus"):
        if st.get("questId") != quest_id:
            continue
        level_id = master.STAGE_LEVELS.get(f"{st.get('stageId')}:{st.get('level') or 1}")
        before = opened_before.get(level_id, set())
        treasures += [dict(t, dropped=t.get("dropped") or t.get("position") in before)
                      for t in st.get("treasureStatus") or []]

    def total(key, battles=fought):
        return sum(int(e.get(key) or 0) for e in battles)

    def ok(mission_id) -> bool:
        m = master.BONUS_MISSIONS.get(str(mission_id))
        if not m or not fought:
            return False
        kind, param = m["type"], m["param"]
        if kind == 0:   # party total HP >= param / 10000 at the end
            return hp * 10000 >= param * max_hp
        if kind == 4:   # nobody died
            return total("deathCount") == 0
        if kind == 12:  # no continue
            return total("continueCount") == 0
        if kind == 14:  # Incarnation (心意) skill used at least param times
            return total("incarnateCount") >= max(param, 1)
        if kind == 18:  # boss beaten within param turns
            return bool(boss) and min(int(e.get("turnCount") or 0) for e in boss) <= param
        if kind == 19:  # boss beaten with rank S or better
            return any(e.get("battleWinRank") == S_RANK for e in boss)
        if kind == 20:  # every battle rank S or better
            return all(e.get("battleWinRank") == S_RANK for e in fought)
        if kind == 21:  # no supporter
            return not setting.get("supporterUserId") and not setting.get("supporterPcId")
        if kind == 22:  # every treasure chest of the field opened
            return all(t.get("dropped") for t in treasures)
        if kind == 103:  # boss battle without healing
            return bool(boss) and total("healCount", boss) == 0
        if kind == 104:  # no healing at all
            return total("healCount") == 0
        if kind in (105, 106):  # this character in the party
            return param in characters
        if kind == 113:  # this character dealt the boss's last attack
            return any(master.PC_CHARACTER.get(str(e.get("bossLastAttackPcId")), 0) == param for e in boss)
        return False

    return [1 if ok(m) else 0 for m in mission_ids]


def quest_clear_drops(table: int) -> list:
    """Reward groups dropped at a quest clear: each slot of the drop table rolls one row by weight (out of 10000)."""
    groups = []
    for rows in master.QUEST_DROPS.get(str(table), {}).values():
        roll = random.randrange(10000)
        for weight, group in rows:
            if roll < weight:
                if group:
                    groups.append(group)
                break
            roll -= weight
    return groups


def ep_quest_clear(uid, req):
    """End of quest (empty body). Battle rewards (pc exp, items, ...) were already pushed by the client; the server
    writes the result screen (UserQuestResult, shape of the archived account), progress and the idle quest status."""
    status = (userdata.table(uid, "questStatus") or [{}])[0]
    setting = dict((userdata.table(uid, "questSetting") or [{}])[0])
    quest_id, level = status.get("questId", 0), status.get("level", 1)
    ql = master.QUEST_LEVELS.get(f"{quest_id}:{level}", {})
    info = dict((userdata.table(uid, "info") or [{}])[0])
    before_exp = info.get("totalExp", 0)
    info["totalExp"] = before_exp + ql.get("exp", 0)
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserQuestResult.json"), encoding="utf-8") as f:
        result = userdata._rekey(json.load(f), uid)
    result.update({
        "userId": uid, "eventId": status.get("eventId", 0), "questId": quest_id, "level": level,
        "gold": ql.get("gold", 0), "readState": 0,
        "beforeParams": {"userTotalExp": before_exp, "bonusMission": [0, 0, 0]},
        "afterParams": {"userTotalExp": info["totalExp"], "bonusMission": [0, 0, 0]},
    })
    now = int(config.now())
    progress = list(userdata.table(uid, "questProgress"))
    row = next((r for r in progress if r.get("questLevelId") == ql.get("id")), None)
    first_clear = row is None or not row.get("clearCount")
    if row is None and ql:
        row = {"userId": uid, "questLevelId": ql["id"], "level": level, "playCount": 0, "clearCount": 0,
               "clearRank": 0, "bonusMission": [0, 0, 0], "playCountOfDay": 0, "lastPlayedAt": 0, "readState": 0,
               "bossClearRank": 0, "bossClearTurn": 0, "clearTurn": 0}
        progress.append(row)
    bonus_ids = [m for m in ql.get("missions", []) if m]
    before_flags = list((row or {}).get("bonusMission") or [0] * len(bonus_ids))[:len(bonus_ids)]
    before_flags += [0] * (len(bonus_ids) - len(before_flags))
    achieved = bonus_mission_results(uid, quest_id, bonus_ids, setting) if bonus_ids else []
    after_flags = [max(a, b) for a, b in zip(before_flags, achieved)]
    if row is not None:
        # runs of the day start again each game day (04:00 JST); the count only ever grew (34 runs "today")
        today = row["playCountOfDay"] if game_day(row.get("lastPlayedAt") or 0) == game_day(now) else 0
        row.update(playCount=row["playCount"] + 1, clearCount=row["clearCount"] + 1,
                   playCountOfDay=today + 1, lastPlayedAt=now)
        if bonus_ids:
            row["bonusMission"] = after_flags
        # best boss rank (1 = S, lower is better) / turns, like the archived account
        normal, boss = quest_battles(uid, quest_id)
        ranks = [e["battleWinRank"] for e in boss if e.get("battleWinRank")]
        if ranks:
            row["bossClearRank"] = min([r for r in (row.get("bossClearRank"), *ranks) if r])
            turns = min(int(e.get("turnCount") or 0) for e in boss)
            row["bossClearTurn"] = min([t for t in (row.get("bossClearTurn"), turns) if t] or [turns])
        clear_turn = sum(int(e.get("turnCount") or 0) for e in normal + boss)
        if clear_turn:
            row["clearTurn"] = min([t for t in (row.get("clearTurn"), clear_turn) if t])
    if bonus_ids:
        result["beforeParams"]["bonusMission"] = before_flags
        result["afterParams"]["bonusMission"] = after_flags
    # quest gold and the clear reward group (battle gold/drops are added by the client itself)
    boost_ids = (setting.get("boostItemIds") or []) if setting.get("questId") == quest_id else []
    gold = ql.get("gold", 0) * (2 if BOOST_GOLD in boost_ids else 1)
    result["gold"] = gold
    grant = Grant(uid)
    if gold:
        grant.item(GOLD_ITEM, gold)
    # clear drops (seeds, EXP Water... of the quest level's drop table), then the first clear reward (diamond
    # cubes) only on the first clear: it used to be given on every clear
    reward_groups = quest_clear_drops(ql.get("drops", 0)) * (2 if BOOST_ITEM in boost_ids else 1)
    # event quests also roll their event's own table (event items, armors of the equipment events): it was never
    # rolled, so those quests only gave seeds ("no armor drops", Discord 2026-10-15)
    reward_groups += quest_clear_drops(master.EVENT_QUEST_DROPS.get(str(quest_id), 0)) * (
        2 if BOOST_EVENT in boost_ids else 1)
    first_group = ql.get("clear_reward", 0) if first_clear else 0
    # a Cathedral floor's first-clear reward is on the floor row (event.bin sub 3 field 13), not on the quest level:
    # it was never given ("reward from cleared cathedral doesn't receive any fragments", tester report, 2026-10-21)
    floor_group = master.TOWER_FLOOR_REWARDS.get(str(quest_id), 0)
    if floor_group and not first_group and not userdata.counter(uid, f"tower_reward_{quest_id}"):
        userdata.set_counter(uid, f"tower_reward_{quest_id}", 1)
        first_group = floor_group
    for group in reward_groups + ([first_group] if first_group else []):
        for it in master.REWARDS.get(str(group), {}).get("items", []):
            grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
    result["questRewardGroupIds"] = reward_groups
    result["firstClearRewardGroupId"] = first_group
    # season points: they were never earned, the Season Pass only moved with diamond cubes (2026-10-17)
    season = season_points(uid, ql.get("ap", 0) * SEASON_POINTS_PER_AP)
    result.update(season)
    if season["seasonPoint"]:
        grant.item(season["seasonPointItemId"], season["seasonPoint"])
    # the event's own point season (its event Battle Pass): never earned, the "Unleash Blading" season points stayed
    # at 0 (tester report, Discord 2026-10-27). Same base and boost as the main season points.
    event_season = event_season_points(uid, setting.get("eventId") or status.get("eventId", 0),
                                       ql.get("ap", 0) * SEASON_POINTS_PER_AP)
    result.update(event_season)
    if event_season["eventSeasonPoint"]:
        grant.item(event_season["eventSeasonPointItemId"], event_season["eventSeasonPoint"])
    # friend points for the supporter: never given (Discord 2026-10-18). The game says "You earned 30 Friend Points
    # for battling alongside your friend": 30 with a player you follow or who follows you, 10 with anyone else
    supporter = setting.get("supporterUserId", 0) if setting.get("questId") == quest_id else 0
    if supporter:
        friends = {r.get("followUserId") for r in userdata.table(uid, "follow")} | {
            r.get("followerUserId") for r in userdata.table(uid, "follower")}
        points = FRIEND_POINTS_FRIEND if supporter in friends else FRIEND_POINTS_OTHER
        result.update(friendPoint=points, supporterUserId=supporter)
        grant.item(FRIEND_POINT_ITEM, points)
    tower = tower_changes(uid, {"eventId": status.get("eventId", 0)}, "clear")
    idle = dict(status, type=4)
    userdata.idle_quest_status(uid, save=idle)
    # type 2 = cleared (WorldExplorerState::update: 1 running, 2 cleared, 3 failed, 4 idle). On 2 the field opens
    # the result screen and, once it closes, FUN_01264ab8 goes back to the quest list / area; the client then sets
    # 4 itself and pushes it. Type 4 here made it take the idle branch: straight home after every quest.
    cleared = dict(status, type=2)
    # questSetting keeps the quest just played (quest, level, party, supporter, boosts): the client replays it from
    # there when several battles in a row were asked; the idle shape (questId 0) sent players home after each one
    kept = setting if setting.get("questId") else idle_quest_setting(uid)
    changes = {**tower, "questResult": [result], "questStatus": [cleared], "questSetting": [kept],
               "questProgress": progress, "info": [info], **grant.changes()}
    played = event_play(uid, setting.get("eventId") or status.get("eventId", 0), now)
    if played:
        changes["eventProgress"] = played
    # bonus missions: each one achieved for the first time gives its reward (1 Diamond Cube), and all of them done
    # gives the quest level's bonus reward, once; both go to the gift box like in the original game
    gifts = None
    for mission_id, before, after in zip(bonus_ids, before_flags, after_flags):
        reward = master.BONUS_MISSIONS.get(str(mission_id), {}).get("reward")
        if after and not before and reward:
            gifts = add_gifts(uid, gifts if gifts is not None else list(userdata.table(uid, "gift")), reward)
    flag = f"bonus_rewarded_{ql.get('id')}"
    if (row is not None and ql.get("bonus_reward") and bonus_ids and all(after_flags)
            and not userdata.counter(uid, flag)):
        gifts = add_gifts(uid, gifts if gifts is not None else list(userdata.table(uid, "gift")), ql["bonus_reward"])
        userdata.add_counter(uid, flag, 1)
    if gifts is not None:
        changes["gift"] = gifts
    added = set(userdata.quest_areas(uid, clear=True))
    if added:
        changes["area"] = [r for r in userdata.table(uid, "area") if r.get("areaId") not in added]
    response = rest_party(uid, userdata.set_tables(uid, changes))
    # raid area quest: the boss lottery (a new boss shows up in the area)
    raid_tables = raid.on_quest_clear(uid, quest_id, status.get("eventId", 0))
    if raid_tables:
        response = raid._merge(response, userdata.set_tables(uid, raid_tables))
    # server missions: quest/chapter clears, bonus missions, daily and weekly quest counts, character missions
    party = next((p for p in userdata.table(uid, "party") if p.get("id") == setting.get("partyId")), {})
    normal, boss = quest_battles(uid, quest_id)
    missions.on_quest_clear(uid, quest_id, status.get("eventId", 0),
                            [sl.get("pcId") for sl in party.get("slots") or [] if sl.get("pcId")],
                            incarnates=sum(int(e.get("incarnateCount") or 0) for e in normal + boss),
                            boost_ids=boost_ids)
    return raid._merge(response, missions.update(uid))


def event_play(uid: int, event_id: int, now: int) -> list | None:
    """UserEventProgress after one more run of a daily-limited event (master.EVENT_PLAY_LIMITS), or None. The client
    shows "runs left = limit - playCount" for the current game day; the server never counted, so "Vigil Key
    Collection" stayed at 5/5 runs left and its first free run was shown on every run (tester reports, Discord 2026-10-28). The count starts again each game day (04:00 JST)."""
    if not event_id or str(event_id) not in master.EVENT_PLAY_LIMITS:
        return None
    rows = [dict(r) for r in userdata.table(uid, "eventProgress")]
    row = next((r for r in rows if r.get("eventId") == event_id), None)
    if row is None:
        row = {"userId": uid, "eventId": event_id, "playCount": 0, "lastPlayedAt": 0, "readState": 1}
        rows.append(row)
    today = row.get("playCount", 0) if game_day(row.get("lastPlayedAt") or 0) == game_day(now) else 0
    row.update(playCount=today + 1, lastPlayedAt=now)
    return rows


def ep_mission_receive(uid, req):
    """{"missionIds": [...]}: completed missions (state 3) become received (4); their rewards go to the gift box,
    except AP (rewardType 6, login time bonus) and season points (10): applied directly, they crash the box."""
    wanted = set((req or {}).get("missionIds") or [])
    missions = [dict(r) for r in userdata.table(uid, "mission")]
    gifts = list(userdata.table(uid, "gift"))
    ap = season = 0
    for m in missions:
        if m.get("missionId") not in wanted or m.get("state") != 3:
            continue
        m["state"] = 4
        group_id = master.MISSION_REWARD.get(str(m["missionId"]), 0)
        items = master.REWARDS.get(str(group_id), {}).get("items", [])
        ap += sum(i["amount"] for i in items if i["rewardType"] == 6)
        season += sum(i["amount"] for i in items if i["rewardType"] == 10)
        add_gifts(uid, gifts, group_id, skip=(6, 10))
    changes = {"mission": missions, "gift": gifts}
    if ap:
        # on top of the AP recovered since baseApUpdatedAt: adding to the stored baseAp lost that recovery
        # ("+50 AP" took 87 AP to 104, tester report, Discord 2026-10-28)
        info = dict((userdata.table(uid, "info") or [{}])[0])
        current, updated = current_ap(info, int(config.now()))
        info.update(baseAp=current + ap, baseApUpdatedAt=updated)
        changes["info"] = [info]
    if season:
        userdata.add_counter(uid, "season_points", season)
    return userdata.set_tables(uid, changes)


GOLD_ITEM = 205000001  # "Col": gold is an item (reward.bin rewardType 4 targets it)


def new_pc(uid: int, pc_id: int) -> dict:
    """A freshly obtained character, shaped like the archived account's scouted ones (level 1, no weapon,
    hp/mp = the character's base values from pc.bin)."""
    hp, mp = master.PC_STATS.get(str(pc_id), [1200, 150])
    return {"userId": uid, "pcId": pc_id, "totalExp": 0, "hp": hp, "mp": mp, "weaponEquipmentStockId": 0,
            "armorEquipmentStockId": 0, "badgeEquipmentStockId": 0, "slotSkills": [], "joiningState": 1,
            "limitBreakCount": 0, "partyEmblemId": 0, "pvpPassiveSkillIds": [], "imaginationSlots": [],
            "lastImaginationUnlockCount": 0, "levelUpperCount": 0}


def dup_extra(pc_id: int) -> tuple[int, int]:
    """(itemId, amount) a duplicate also gives besides its crystals: Custom Material R2/R3 x10, R4 x25
    (lottery.bin sub 7, field 7 = 1). It was never given."""
    item, amount = master.PC_DUP_EXTRA.get(str(pc_id)) or [0, 0]
    return int(item), int(amount)


def dup_item(pc_id: int) -> tuple[int, int]:
    """(crystal itemId, amount) a duplicate character turns into: 100 element crystals for a 4*, the game's rule
    (lottery.bin sub 7). It used to be a single crystal for every rarity."""
    item, amount = master.PC_DUP_ITEM.get(str(pc_id)) or [182000010, 1]
    return int(item), int(amount)


class Grant:
    """Accumulates rewards for one request and turns them into table changes."""

    def __init__(self, uid: int):
        self.uid = uid
        self.items = {r["itemId"]: dict(r) for r in userdata.table(uid, "item")}
        self.pcs = list(userdata.table(uid, "pc"))
        self.owned_pcs = {r.get("pcId") for r in self.pcs}
        self.stock = list(userdata.table(uid, "equipmentStock"))
        self.species = list(userdata.table(uid, "equipmentSpecie"))
        self.known = {r.get("equipmentSpecieId") for r in self.species}
        self.next_stock = max((r.get("id", 0) for r in self.stock), default=0) + 1
        self.imaginations = list(userdata.table(uid, "imaginationStock"))
        self.next_imagination = max([r.get("id", 0) for r in self.imaginations] + [
            (userdata.table(uid, "info") or [{}])[0].get("lastImaginationStockId", 0)]) + 1
        self.gems = 0
        self.changed = set()

    def item(self, item_id: int, amount: int):
        row = self.items.setdefault(item_id, {"userId": self.uid, "itemId": item_id, "amount": 0,
                                              "reservedAmount": 0, "readState": 0})
        row["amount"] += amount
        self.changed.add("item")

    def equipment(self, equipment_id: int, amount: int = 1, level: int = 1):
        for _ in range(max(amount, 1)):
            self.stock.append({"userId": self.uid, "id": self.next_stock, "equipmentId": equipment_id,
                               "limitBreakCount": 0, "refiningLevel": max(level, 1), "isFavorite": False,
                               "abilitySlots": []})
            self.next_stock += 1
        if equipment_id not in self.known:
            self.species.append({"userId": self.uid, "equipmentSpecieId": equipment_id})
            self.known.add(equipment_id)
        self.changed.update(("equipmentStock", "equipmentSpecie"))

    def imagination(self, imagination_id: int, amount: int = 1):
        """UserImaginationStock {userId, id, imaginationId, isFavorite, readState} (client FUN_014b21cc), one row
        per Imaginarium (item.20, 380xxxxxx)."""
        for _ in range(max(amount, 1)):
            self.imaginations.append({"userId": self.uid, "id": self.next_imagination, "imaginationId": imagination_id,
                                      "isFavorite": False, "readState": 0})
            self.next_imagination += 1
        self.changed.add("imaginationStock")

    def pc(self, pc_id: int) -> bool:
        """True if the character is new; a duplicate gives limit-break crystals instead (see dup_item)."""
        if pc_id in self.owned_pcs:
            return False
        self.pcs.append(new_pc(self.uid, pc_id))
        self.owned_pcs.add(pc_id)
        self.changed.add("pc")
        return True

    def reward(self, kind: int, target: int, amount: int, level: int = 1) -> bool:
        """Apply one reward (reward.bin rewardType); False if this type is not handled."""
        if kind in (1, 4):  # 4 = gold ("Col"), itself an item
            self.item(target or GOLD_ITEM, amount)
        elif kind == 7:
            # Imaginarium (rewardType 7): Battle Pass tiers, login bonuses, events. Not handled, so they were
            # never given ("the Imaginarium I received via the Battle Pass is nowhere to be found", 2026-10-20)
            self.imagination(target, amount)
        elif kind == 9:
            # "Guild mission pt" (221003716) of the guild mission rewards: the points already went to the guild when
            # the mission was completed (guild.py). Not handled, the gift stayed unopened and could be collected
            # again and again as an empty icon (tester report, 2026-10-22): accepted, nothing more to give.
            pass
        elif kind == 8:
            # equipment ability items (item.bin equipment_ability_item, 4029xxxxx): Training Room drops. Not
            # handled, so those quests gave none ("training room doesn't drop any single ability", 2026-10-20)
            self.item(target, amount)
        elif kind == 2:
            self.equipment(target, amount, level)
        elif kind == 3:
            self.gems += amount
        elif kind == 5:
            if not self.pc(target):
                self.item(*dup_item(target))
                extra, extra_amount = dup_extra(target)
                if extra:
                    self.item(extra, extra_amount)
        else:
            return False
        return True

    def changes(self) -> dict:
        if self.gems:
            userdata.add_gems(self.uid, self.gems)
        tables = {"item": list(self.items.values()), "pc": self.pcs, "equipmentStock": self.stock,
                  "equipmentSpecie": self.species, "imaginationStock": self.imaginations}
        out = {k: v for k, v in tables.items() if k in self.changed}
        # the client numbers the equipment / Imaginarium rows it creates from info.lastEquipmentStockId /
        # lastImaginationStockId: keep them past ours
        info = dict((userdata.table(self.uid, "info") or [{}])[0])
        for table, field, rows in (("equipmentStock", "lastEquipmentStockId", self.stock),
                                   ("imaginationStock", "lastImaginationStockId", self.imaginations)):
            top = max((r.get("id", 0) for r in rows), default=0)
            if table in self.changed and info and info.get(field, 0) < top:
                info[field] = top
                out["info"] = [info]
        return out


def add_gifts(uid: int, gifts: list, reward_group: int, skip=()) -> list:
    """Append a reward group's items to the gift box rows. Gift ids are never reused (received gifts used to be
    dropped from the table), so a per-user high-water mark is kept."""
    group = master.REWARDS.get(str(reward_group), {})
    next_id = max(max((g.get("id", 0) for g in gifts), default=0), userdata.counter(uid, "last_gift_id"))
    now = int(config.now())
    for item in group.get("items", []):
        if item["rewardType"] in skip:
            continue
        next_id += 1
        gifts.append({"userId": uid, "id": next_id, "senderType": 1, "senderId": 0, **item,
                      "reasonId": group.get("reason", 0), "state": 0, "startAt": 0, "expireAt": 0,
                      "createdAt": now})
    userdata.set_counter(uid, "last_gift_id", next_id)
    return gifts


def season_point_items() -> set:
    """The season point items (item.bin seasonPointItem, 405xxxxxx), one per season."""
    return {int(v) for v in master.SEASON.get("pointItems", {}).values() if v}


def unique_items() -> set:
    """Items one owns or not (guild stamps 401xxxxxx, guild icons 390xxxxxx): never more than 1."""
    import idspage  # noqa: PLC0415
    return {r[0] for r in idspage.data()["items"] if r[2] in ("guild_stamp", "guild_icon")}


def giftable_items() -> set:
    """Item ids the gift box can hold: the real items of item.bin (idspage.data(), without the exchange shop
    sheets), minus the item / material category headers ("Season pt" 200000046) and the season points. Anything
    else crashed the client when the gift box was opened (Discord bot claims, 2026-10-20)."""
    import idspage  # noqa: PLC0415
    items = {r[0] for r in idspage.data()["items"] if r[2] not in ("item_type", "material_type")}
    return items - season_point_items()


def add_gift(uid: int, reward_type: int, target: int, amount: int, level: int = 1, reason: int = 0) -> dict:
    """One gift in the box (admin panel): rewardType 1 item, 2 equipment, 3 diamond cubes, 4 gold, 5 character;
    reason = reward.bin sub 2 id (text shown in the box). Received with gift/receive like any other gift."""
    if int(reward_type) == 1 and is_imagination(target):
        reward_type = 7  # an Imaginarium is not an item: as type 1 it landed in UserItem, invisible in game
    if int(reward_type) == 1 and int(target) not in giftable_items():
        # season points, category headers or exchange-shop rows in the gift box crash the client when it opens
        raise ValueError(f"item {target} cannot go to the gift box (not a giftable item: the game crashes on it)")
    gifts = list(userdata.table(uid, "gift"))
    next_id = max(max((g.get("id", 0) for g in gifts), default=0), userdata.counter(uid, "last_gift_id")) + 1
    # every field must be a number: a reasonId sent as text made the client fail on the whole gift box ("error 200",
    # 4 accounts after the 2026-10-11 ticket refund)
    gifts.append({"userId": uid, "id": next_id, "senderType": 1, "senderId": 0, "rewardType": int(reward_type),
                  "itemId": int(target), "level": int(level), "amount": int(amount), "reasonId": int(reason), "state": 0,
                  "startAt": 0, "expireAt": 0, "createdAt": int(config.now())})
    userdata.set_counter(uid, "last_gift_id", next_id)
    return userdata.set_tables(uid, {"gift": gifts})


def _gift_ids(req) -> set:
    """The request body names the gifts to open; accept any list of ints under any key (format not confirmed)."""
    ids = set()
    for v in (req or {}).values():
        if isinstance(v, list):
            ids.update(x for x in v if isinstance(x, int))
        elif isinstance(v, int) and not isinstance(v, bool):
            ids.add(v)
    return ids


def ep_gift_receive(uid, req):
    """{"giftIds": [...]}: open gift-box entries; opened gifts stay in the table with state 1."""
    wanted = _gift_ids(req)
    gifts = [dict(g) for g in userdata.table(uid, "gift")]
    grant = Grant(uid)
    for g in gifts:
        if g.get("id") in wanted and g.get("state") == 0 and grant.reward(
                g.get("rewardType"), g.get("itemId"), g.get("amount", 0), g.get("level", 1)):
            g["state"] = 1
            grant.changed.add("gift")
    changes = grant.changes()
    if "gift" in grant.changed:
        changes["gift"] = gifts
    return userdata.set_tables(uid, changes)


def lottery_pick(lottery: str, top: bool = False):
    """One draw from a lottery: rarity by its rate table (or its highest one if `top`), then an item of that
    rarity by weight."""
    rates = master.LOTTERY["rates"].get(lottery, [])
    pool = master.LOTTERY["items"].get(lottery, [])
    rarities = [(r, w) for r, w in rates if w and any(x["rarity"] == r and x["weight"] for x in pool)]
    if top and rarities:
        rarities = [max(rarities)]
    rarity = random.choices([r for r, _ in rarities], [w for _, w in rarities])[0] if rarities else None
    choices = [x for x in pool if x["rarity"] == rarity and x["weight"]] or [x for x in pool if x["weight"]]
    return random.choices(choices, [x["weight"] for x in choices])[0] if choices else None


MAX_TICKET_DRAWS = 100  # guard against a forged ticketAmount


def ep_lottery_draw(uid, req):
    """{"lotteryPriceId", "step", "gemAmount", "ticketId", "ticketAmount"}: pay, draw by rarity then by item weight,
    grant the results and write UserLotteryResult (shape of the archived account)."""
    req = req or {}
    price_id = int(req.get("lotteryPriceId", 0))
    price = master.LOTTERY["prices"].get(str(price_id))
    if not price:
        return {}
    lottery = str(price["lottery"])
    count = 11 if price["type"] == 2 else 1
    gem_cost = int(req.get("gemAmount", 0))
    ticket_id, ticket_cost = int(req.get("ticketId", 0)), int(req.get("ticketAmount", 0))
    if ticket_id and ticket_cost and price["type"] != 2 and price.get("cost"):
        # ticket scouts can use several tickets at once (the client sends ticketAmount = N x the per-draw cost):
        # one draw per ticket cost. Before, N tickets were consumed for a single draw (issue #5).
        count = max(1, min(ticket_cost // price["cost"], MAX_TICKET_DRAWS))
        ticket_cost = count * price["cost"]
    drawn = next((r.get("drawCount", 0) for r in userdata.table(uid, "lotteryPrice")
                  if r.get("lotteryPriceId") == price_id), 0)
    if not ticket_id and not gem_cost and price["type"] != 2 and not price.get("cost") and ticket_cost > 1:
        # free scouts (cost 0, e.g. the 1000 free draws of release.202210_lottery_free): the number of draws the
        # player picked comes as ticketAmount with no ticket; it always drew 1 (tester report, Discord 2026-11-03)
        count = min(ticket_cost, MAX_TICKET_DRAWS, max(1, price["limit"] - drawn) if price["limit"] else MAX_TICKET_DRAWS)
        ticket_cost = 0
    if price["limit"] and drawn + (count if price["type"] != 2 else 1) > price["limit"]:
        return {}
    grant = Grant(uid)
    if ticket_id and ticket_cost:
        have = grant.items.get(ticket_id, {}).get("amount", 0)
        if have < ticket_cost:
            return {}
        grant.item(ticket_id, -ticket_cost)
    if gem_cost:
        if userdata.gems(uid) < gem_cost:
            return {}
        grant.gems -= gem_cost
    # step-up banners: the current step can boost every draw's rates or guarantee the last draw (11th)
    steps = master.LOTTERY["steps"].get(lottery, {})
    step = int(req.get("step") or 0)
    bonus = steps.get(str(step), {}) if price["type"] == 2 and config.LOTTERY_STEP_BONUS else {}
    results = []
    for i in range(count):
        source = lottery
        if bonus.get("alt") and (bonus["bonus"] == 3 or (bonus["bonus"] == 4 and i == count - 1)):
            source = str(bonus["alt"])
        # debugging aid: a per-user counter forces the highest rarity of the drawn lottery (last draw only)
        top = i == count - 1 and userdata.counter(uid, "debug_force_top") > 0
        if top:
            userdata.add_counter(uid, "debug_force_top", -1)
        pick = lottery_pick(source, top) or lottery_pick(lottery)
        if not pick:
            break
        is_new = pick["rewardType"] == 5 and pick["target"] not in grant.owned_pcs
        grant.reward(pick["rewardType"], pick["target"], pick["amount"])
        dup = not is_new and pick["rewardType"] == 5
        replace, replace_amount = dup_item(pick["target"]) if dup else (0, 0)
        extra, extra_amount = dup_extra(pick["target"]) if dup else (0, 0)
        results.append({"index": i, "lotteryItemId": pick["id"], "replaceItemId": replace,
                        "replaceItemAmount": replace_amount, "imaginationId": 0, "isNew": is_new,
                        "extraReplaceItemId": extra, "extraReplaceItemAmount": extra_amount,
                        "pickupSelectionLotteryItemId": 0})
    point = master.LOTTERY.get("points", {}).get(lottery)
    if point and results:
        grant.item(point[0], point[1] * len(results))  # scout points for the banner's exchange
    now = int(config.now())
    prices = [dict(r) for r in userdata.table(uid, "lotteryPrice")]
    row = next((r for r in prices if r.get("lotteryPriceId") == price_id), None)
    if row is None:
        row = {"userId": uid, "lotteryPriceId": price_id, "drawCount": 0, "dailyDrawCount": 0, "currentDrawCount": 0,
               "currentStep": 0, "lastDrawTime": 0, "currentAdditionalRateStep": 0, "canDrawAdditional": False,
               "selectedPickupLotteryItemId": 0}
        prices.append(row)
    draws = 1 if price["type"] == 2 else max(1, len(results))  # an 11-draw counts once, ticket draws one each
    row.update(drawCount=row["drawCount"] + draws, dailyDrawCount=row["dailyDrawCount"] + draws, lastDrawTime=now)
    if steps and price["type"] == 2:
        row["currentStep"] = bonus.get("next") or step + 1
    changes = grant.changes()
    changes["lotteryPrice"] = prices
    changes["lotteryResult"] = [{"userId": uid, "lotteryPriceId": price_id, "resultItems": results,
                                 "displayed": False, "upperLimitLotteryPointValue": 0}]
    response = userdata.set_tables(uid, changes)
    # scout missions: a weapon banner gives equipment, a character banner characters
    kinds = [master.LOTTERY["itemIndex"].get(str(r["lotteryItemId"]), {}).get("rewardType") for r in results]
    weapons = sum(1 for k in kinds if k == 2)
    missions.on_scout(uid, characters=len(results) - weapons if weapons * 2 < len(results) else 0,
                      weapons=len(results) if weapons * 2 >= len(results) else 0)
    return raid._merge(response, missions.update(uid))


def stage_treasures(stage_id: int, area_id, quest_level: int) -> list:
    """UserStageTreasureStatus for the chests of the stage's area (position, treasureId, rewardGroupIds,
    dropped, getAt; order from the client's serializer). Contents drawn from treasure.bin by tier/type/level."""
    setup = master.STAGE_ENCOUNTS.get(f"{stage_id}:{quest_level}") or master.STAGE_ENCOUNTS.get(f"{stage_id}:1") or {}
    tier = setup.get("tier") or 10
    table = master.TREASURES["table"]
    tiers = sorted({int(k.split(":")[0]) for k in table})
    tier = max([t for t in tiers if t <= tier], default=tiers[0])
    out = []
    for position, kind in master.TREASURES["chests"].get(str(area_id), []):
        rows = table.get(f"{tier}:{kind}:{quest_level}") or table.get(f"{tier}:{kind}:1") or []
        rows = [r for r in rows if r[1]]
        if not rows:
            continue
        treasure_id, _, reward = random.choices(rows, [r[1] for r in rows])[0]
        out.append({"position": position, "treasureId": treasure_id, "rewardGroupIds": [reward] if reward else [],
                    "dropped": False, "getAt": 0})
    return out


# "Skip tutorial" (TutorialManager::skipTutorialFirst/Second): the client keeps asking on every boot until the
# account shows that part as done. Progress below = the archived account, which is exactly "tutorial finished".
TUTORIAL_SKIPS = {
    "first": {"tutorials": [100000003], "quests": [8110000011, 8110000021], "flags": []},
    # 14-18 (equipment scout, equipment, enhance, skill slot, event) and 44 run right after 1-2-3; done in the
    # archived account, and the equipment one softlocks when Kirito's sword is the one it points at
    "second": {"tutorials": list(range(100000004, 100000011)) + list(range(100000014, 100000019)) + [100000044],
               # 8140090011: the Equipment Creation Event Tutorial (Beginner) quest, which tutorial 100000018 stands
               # for: the event showed COMPLETE but its mission stayed 0/1 and "Challenge" replayed the tutorial
               # (tester report, Discord 2026-11-02; 74 accounts on one server)
               "quests": [8110100011, 8130280011, 8110110011, 8110110021, 8110110031, 8140090011],
               "flags": [91210001, 91210002, 91210003, 91210005, 91210006, 91210007, 91210008, 91210010,
                         91210011, 91210012, 91210013, 91210017]},
}


def tutorial_quests_repair(uid: int) -> list | None:
    """UserQuestProgress with the quests of the tutorial skips whose tutorials are done (accounts that skipped before
    a quest was added to TUTORIAL_SKIPS), or None when nothing is missing. Run at login."""
    done = {r.get("tutorialId") for r in userdata.table(uid, "tutorial")}
    progress = list(userdata.table(uid, "questProgress"))
    cleared = {r.get("questLevelId") for r in progress if r.get("clearCount")}
    missing = [ql for skip in TUTORIAL_SKIPS.values() if set(skip["tutorials"]) <= done
               for ql in skip["quests"] if ql not in cleared]
    if not missing:
        return None
    now = int(config.now())
    progress = [r for r in progress if r.get("questLevelId") not in missing]
    progress += [{"userId": uid, "questLevelId": ql, "level": 1, "playCount": 1, "clearCount": 1, "clearRank": 4,
                  "bonusMission": [0, 0, 0], "playCountOfDay": 0, "lastPlayedAt": now, "readState": 0,
                  "bossClearRank": 0, "bossClearTurn": 0, "clearTurn": 0} for ql in dict.fromkeys(missing)]
    return progress


def tutorial_skip(uid: int, part: str) -> dict:
    skip = TUTORIAL_SKIPS[part]
    now = int(config.now())
    tutorials = list(userdata.table(uid, "tutorial"))
    done = {r.get("tutorialId") for r in tutorials}
    tutorials += [{"userId": uid, "tutorialId": t} for t in skip["tutorials"] if t not in done]
    progress = list(userdata.table(uid, "questProgress"))
    have = {r.get("questLevelId") for r in progress}
    for ql in skip["quests"]:
        if ql not in have:
            progress.append({"userId": uid, "questLevelId": ql, "level": 1, "playCount": 1, "clearCount": 1,
                             "clearRank": 4, "bonusMission": [0, 0, 0], "playCountOfDay": 0, "lastPlayedAt": now,
                             "readState": 0, "bossClearRank": 0, "bossClearTurn": 0, "clearTurn": 0})
    flags = list(userdata.table(uid, "systemFlag"))
    on = {r.get("systemFlagId") for r in flags}
    flags += [{"userId": uid, "systemFlagId": f, "flagValue": 1} for f in skip["flags"] if f not in on]
    return userdata.set_tables(uid, {"tutorial": tutorials, "questProgress": progress, "systemFlag": flags})


def ep_tutorial_skip_first(uid, req):
    return tutorial_skip(uid, "first")


def ep_tutorial_skip_second(uid, req):
    return tutorial_skip(uid, "second")


# Supporters ("helpers"). The original server listed other players; we make up a fixed set of guest players.
# UserGuest (FUN_014abd2c): userId, guestUserId, info, pc[], equipmentStock[], supporterPcSlot[], imaginationStock[],
# profileTrackers[]. An empty list soft-locks battle tutorials that rotate the supporter in.
GUEST_BASE_ID = 900000000000
SUPPORTERS = [  # (pcId, player name); several have a Charge skill (pc.bin sub 2 field 104 = 156000201)
    (130114003, "Kazuto"), (130324001, "Asuna_fan"), (130124024, "Integrity"), (130144001, "Zekken"),
    (130114002, "BlueRose"), (130624003, "Hecate"), (130454007, "Yui_chan"), (130124016, "Terraria"),
]
SUPPORTER_EXP = 15950  # level 30 (pc.bin sub 1)


def guest_rows(uid: int) -> list:
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserInfo.json"), encoding="utf-8") as f:
        info_template = json.load(f)
    rows = []
    for i, (pc_id, name) in enumerate(SUPPORTERS, 1):
        gid = GUEST_BASE_ID + i
        pc = dict(new_pc(gid, pc_id), totalExp=SUPPORTER_EXP)
        info = dict(info_template, userId=gid, name=name, profilePcId=pc_id, homePcId=pc_id, totalExp=2000 + 500 * i,
                    lastLoginedAt=int(config.now()), comment="")
        slot = {"userId": gid, "id": 1, "elementType": 0, "pcId": pc_id, "weaponEquipmentStockId": 0,
                "armorEquipmentStockId": 0, "badgeEquipmentStockId": 0}
        rows.append({"userId": uid, "guestUserId": gid, "info": info, "pc": [pc], "equipmentStock": [],
                     "supporterPcSlot": [slot], "imaginationStock": [], "profileTrackers": []})
    return rows


SUPPORTER_COUNT = 10


def player_supporter(uid: int, other: int):
    """A real player as a supporter: their profile card with at least one supporter slot (their profile character
    when they never set one: an empty supporterPcSlot shows no cell)."""
    row = guild.guest_row(uid, other)
    if not row or not row["pc"]:
        return None
    slots = [s for s in row["supporterPcSlot"] if s.get("pcId") in {p.get("pcId") for p in row["pc"]}]
    if not slots:
        pc = next((p for p in row["pc"] if p.get("pcId") == row["info"].get("profilePcId")), row["pc"][0])
        slots = [{"userId": other, "id": 1, "elementType": 0, "pcId": pc["pcId"],
                  "weaponEquipmentStockId": pc.get("weaponEquipmentStockId", 0),
                  "armorEquipmentStockId": pc.get("armorEquipmentStockId", 0),
                  "badgeEquipmentStockId": pc.get("badgeEquipmentStockId", 0)}]
    return dict(row, supporterPcSlot=slots)


def quest_guest_rows(uid: int, quest_id: int) -> list:
    """The imposed supporter of a lesson / tutorial quest (quest.bin sub 9) as the only guest: the Beginner's
    Residence lessons restrict the party to Kirito, and the real players offered since 2026-10-17 (or the made-up
    Kirito before) made the supporter a second Kirito, so the quest could not start."""
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserInfo.json"), encoding="utf-8") as f:
        info_template = json.load(f)
    rows = []
    for i, (_, pc_id, level, name) in enumerate(master.QUEST_GUESTS.get(str(quest_id), []), 1):
        # an id below 1000 makes it an NPC for the client (FUN_0128a7ec); a player id (900000000101) made it an
        # ordinary supporter, whose incarnate is only allowed for mutual friends: the lesson's INC button did
        # nothing (soft lock, 2026-10-17)
        gid = i
        exp = max([e for lv, e in master.PC_LEVEL_EXP if lv <= level], default=0)
        pc = dict(new_pc(gid, pc_id), totalExp=exp,
                  **dict(zip(("hp", "mp"), master.pc_max_hp_mp(pc_id, exp))))
        info = dict(info_template, userId=gid, name=name, profilePcId=pc_id, homePcId=pc_id,
                    lastLoginedAt=int(config.now()), comment="")
        slot = {"userId": gid, "id": 1, "elementType": 0, "pcId": pc_id, "weaponEquipmentStockId": 0,
                "armorEquipmentStockId": 0, "badgeEquipmentStockId": 0}
        rows.append({"userId": uid, "guestUserId": gid, "info": info, "pc": [pc], "equipmentStock": [],
                     "supporterPcSlot": [slot], "imaginationStock": [], "profileTrackers": []})
    return rows


def ep_quest_supporter(uid, req):
    """{"questId", "level"} -> the guest list the client builds its supporter cells from: followed players first,
    then the other players who logged in last, padded with the made-up ones. Only the made-up ones were sent, so
    real players never showed up (Discord, 2026-10-17)."""
    imposed = quest_guest_rows(uid, int((req or {}).get("questId") or 0))
    # the rotation tutorial 1-2-2 (8011011002) imposes Eugeo 130111002, whom every new account already has in its
    # party: the client then leaves the sub member slot "EMPTY" and the tutorial waits forever on it (seen 2026-10-10,
    # back on 2026-10-18 with the imposed supporters). An imposed character already in the party is not imposed.
    info = (userdata.table(uid, "info") or [{}])[0]
    party = next((p for p in userdata.table(uid, "party") if p.get("id") == info.get("activePartyId", 1)), {})
    members = {sl.get("pcId") for sl in party.get("slots", [])}
    imposed = [g for g in imposed if g["pc"][0]["pcId"] not in members]
    if imposed:
        return userdata.set_tables(uid, {"guest": imposed})
    import db  # noqa: PLC0415
    follows = [r.get("followUserId") for r in userdata.table(uid, "follow")]
    others = []
    for (other,) in db._conn.execute("SELECT user_id FROM users WHERE user_id != ?", (uid,)).fetchall():
        info = (userdata.table(other, "info") or [{}])[0]
        others.append((-(info.get("lastLoginedAt") or 0), other))
    order = list(dict.fromkeys([u for u in follows if u] + [u for _, u in sorted(others)]))
    rows = []
    for other in order:
        if len(rows) == SUPPORTER_COUNT:
            break
        row = player_supporter(uid, int(other))
        if row:
            rows.append(row)
    rows += guest_rows(uid)[:max(0, SUPPORTER_COUNT - len(rows))]
    return userdata.set_tables(uid, {"guest": rows})


def supporter_slot(uid: int, req: dict) -> dict:
    """UserQuestSupporterSlot for the supporter chosen in quest/start (empty shape when none)."""
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserQuestSupporterSlot.json"), encoding="utf-8") as f:
        slot = userdata._rekey(json.load(f), uid)
    gid, pc_id = int(req.get("supporterUserId") or 0), int(req.get("supporterPcId") or 0)
    guest = next((g for g in userdata.table(uid, "guest") if g.get("guestUserId") == gid), None)
    pc = next((p for p in (guest or {}).get("pc", []) if p.get("pcId") == pc_id), None)
    if pc:
        slot.update(slotId=int(req.get("supporterPcSlotId") or 0), supporterUserId=gid, pc=pc,
                    equipmentStock=guest.get("equipmentStock", []))
    return slot


def ep_quest_skip(uid, req):
    """Quest skip (same body as quest/start): start + clear at once; the battles that are not played still drop
    their loot (rolled per enemy at start), granted here."""
    start = ep_quest_start(uid, req)
    drops = [g for row in (start.get("table", {}).get("replace", {}).get("stageStatus") or [])
             for enc in row.get("encountStatus", []) + row.get("bossEncountStatus", [])
             for enemy in enc.get("enemyStatus", []) for g in enemy.get("rewardGroupIds", [])]
    gold = sum(enemy.get("gold", 0) for row in (start.get("table", {}).get("replace", {}).get("stageStatus") or [])
               for enc in row.get("encountStatus", []) + row.get("bossEncountStatus", [])
               for enemy in enc.get("enemyStatus", []))
    grant = Grant(uid)
    for group in drops:
        for it in master.REWARDS.get(str(group), {}).get("items", []):
            grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
    if gold:
        grant.item(GOLD_ITEM, gold)
    userdata.set_tables(uid, grant.changes())
    clear = ep_quest_clear(uid, {})
    hashes = dict(start.get("hash", {}), **clear.get("hash", {}))
    tables = dict(start.get("table", {}).get("replace", {}), **clear.get("table", {}).get("replace", {}))
    # items: take the final state (drops + quest gold)
    tables["item"] = userdata.table(uid, "item")
    hashes["item"] = userdata.load(uid)["hash"].get("item", "")
    return {"hash": hashes, "table": {"replace": tables}}


def ep_quest_continue(uid, req):
    """Continue after a defeat (free on this server): count it on the running quest."""
    status = dict((userdata.table(uid, "questStatus") or [{}])[0])
    status["continueCount"] = status.get("continueCount", 0) + 1
    return userdata.set_tables(uid, {"questStatus": [status]})


def ep_equipment_expand(uid, req):
    """{"addCount", "consumeGem"}: bigger equipment inventory, paid in diamond cubes."""
    req = req or {}
    add, cost = int(req.get("addCount", 0)), int(req.get("consumeGem", 0))
    if cost and userdata.gems(uid) < cost:
        return {}
    if cost:
        userdata.add_gems(uid, -cost)
    info = dict((userdata.table(uid, "info") or [{}])[0])
    info["equipmentStockAddCount"] = info.get("equipmentStockAddCount", 0) + add
    out = userdata.set_tables(uid, {"info": [info]})
    missions.bump(uid, equipment_expand=1)  # mission 503 "expand the equipment inventory"
    return raid._merge(out, missions.update(uid))


def ep_guest_search(uid, req):
    """{"guestUserIds": [...]}: profile of other players (only our made-up supporters exist)."""
    wanted = set((req or {}).get("guestUserIds") or [])
    rows = [g for g in guest_rows(uid) if not wanted or g["guestUserId"] in wanted]
    known = {g["guestUserId"]: g for g in userdata.table(uid, "guest")}
    known.update({g["guestUserId"]: g for g in rows})
    for other in wanted - set(known) - {uid}:  # real players (guild members, PvP opponents...)
        row = guild.guest_row(uid, int(other))
        if row:
            known[int(other)] = row
    return userdata.set_tables(uid, {"guest": list(known.values())})


def ep_guest(uid, req):
    """/guest {"guestUserIds": [...]}: same as guest/search, but the client also asks for its own profile card."""
    wanted = {int(u) for u in (req or {}).get("guestUserIds") or []}
    known = {g["guestUserId"]: g for g in userdata.table(uid, "guest")}
    for other in wanted:
        row = guild.guest_row(uid, other) if other < GUEST_BASE_ID else None
        if row:
            known[other] = row
    return userdata.set_tables(uid, {"guest": list(known.values())})


# Friends (FriendManager): UserFollow {userId, followUserId} (FUN_01507408), UserFollower {userId, followerUserId,
# readState} (FUN_01507cb8). The routes were missing, so Follow did nothing (tester report, 2026-10-17).
def _friend_target(req: dict, *keys) -> int:
    for key in keys:
        if (req or {}).get(key):
            return int(req[key])
    return 0


def _follow_tables(uid: int, follows: list, followers: list) -> dict:
    """The viewer's follow lists plus the profile cards the friend list shows."""
    others = {r["followUserId"] for r in follows} | {r["followerUserId"] for r in followers}
    return {"follow": follows, "follower": followers, "guest": guild.with_guests(uid, others)}


def ep_friend_follow_request(uid, req):
    """{"userId", "followUserId"}: follow a player; they get a follower row (readState 0 = new)."""
    other = _friend_target(req, "followUserId")
    follows = [dict(r) for r in userdata.table(uid, "follow")]
    if other and other != uid and other < GUEST_BASE_ID and userdata.table(other, "info") \
            and all(r.get("followUserId") != other for r in follows):
        follows.append({"userId": uid, "followUserId": other})
        theirs = [dict(r) for r in userdata.table(other, "follower") if r.get("followerUserId") != uid]
        userdata.set_tables(other, {"follower": theirs + [{"userId": other, "followerUserId": uid, "readState": 0}]})
    out = userdata.set_tables(uid, _follow_tables(uid, follows, list(userdata.table(uid, "follower"))))
    return raid._merge(out, missions.update(uid))  # mission 502 "follow N players"


def ep_friend_follow_remove(uid, req):
    """{"userId", "followUserId"}: stop following (and leave their follower list)."""
    other = _friend_target(req, "followUserId", "targetUserId")
    follows = [dict(r) for r in userdata.table(uid, "follow") if r.get("followUserId") != other]
    if other:
        theirs = [dict(r) for r in userdata.table(other, "follower") if r.get("followerUserId") != uid]
        userdata.set_tables(other, {"follower": theirs})
    return userdata.set_tables(uid, _follow_tables(uid, follows, list(userdata.table(uid, "follower"))))


def ep_friend_follower_remove(uid, req):
    """{"userId", "followerUserId"}: remove a follower (they stop following this player)."""
    other = _friend_target(req, "followerUserId", "followUserId", "targetUserId")
    followers = [dict(r) for r in userdata.table(uid, "follower") if r.get("followerUserId") != other]
    if other:
        theirs = [dict(r) for r in userdata.table(other, "follow") if r.get("followUserId") != uid]
        userdata.set_tables(other, {"follow": theirs})
    return userdata.set_tables(uid, _follow_tables(uid, list(userdata.table(uid, "follow")), followers))


def ep_item_exchange(uid, req):
    """{"exchangeOutputItemList": [{"id", "count", "inputGroupItemId"}]}: pay the input group (items, gold or
    diamond cubes), grant the output's reward group, count it in UserExchangedItem (userId, exchangeOutputItemId,
    count, lastUpdatedAt, readState)."""
    ex = master.EXCHANGE
    grant = Grant(uid)
    done = {r["exchangeOutputItemId"]: dict(r) for r in userdata.table(uid, "exchangedItem")}
    now = int(config.now())
    for entry in (req or {}).get("exchangeOutputItemList") or []:
        out = ex["out"].get(str(entry.get("id")))
        count = max(int(entry.get("count", 1)), 1)
        if not out:
            return {}
        row = done.setdefault(entry["id"], {"userId": uid, "exchangeOutputItemId": entry["id"], "count": 0,
                                            "lastUpdatedAt": 0, "readState": 0})
        if out.get("resets") and game_day(row["lastUpdatedAt"]) != game_day(now):
            # a limit that starts again (item.bin sub 9 field 11): the count never went back to 0, so after 10
            # Battle Skips / Event Rewards x2 the cube shop refused them for good while the client still offered
            # them (tester report, 2026-10-25). The client keeps its own period; the server starts again each game day.
            row["count"] = 0
        if out["max"] and row["count"] + count > out["max"]:
            return {}
        for label, amount in ex["in"].get(str(entry.get("inputGroupItemId") or out["input"]), []):
            cost = amount * count
            if label == "currency.gem":
                if userdata.gems(uid) + grant.gems < cost:
                    return {}
                grant.gems -= cost
            else:
                item_id = ex["labels"].get(label)
                if item_id is None or grant.items.get(item_id, {}).get("amount", 0) < cost:
                    return {}
                grant.item(item_id, -cost)
        for _ in range(count):
            for it in master.REWARDS.get(str(out["reward"]), {}).get("items", []):
                grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
        row.update(count=row["count"] + count, lastUpdatedAt=now)
    changes = grant.changes()
    changes["exchangedItem"] = list(done.values())
    return userdata.set_tables(uid, changes)


# Login bonuses running when the service closed (the archived account's UserLoginBonus rows).
ACTIVE_LOGIN_BONUSES = [40010000002, 40010000030, 40010000084, 40010000188, 40010000190, 40010000191]
DAY_RESET = 4 * 3600 - 9 * 3600  # daily reset 04:00 JST, as an offset from UTC midnight


def game_day(ts: float) -> int:
    return int((ts - DAY_RESET) // 86400)


def login_bonus_loops(bonus_id: int, days: dict) -> bool:
    """False for a one-time campaign bonus (master.LOGIN_BONUS_ONCE) that gives something one owns once (a home
    illustration 97xxxxxx, a character): it started a new lap every day, so the "Unleash Blading" illustration
    (40010000191, one day) came back at every login (tester report, Discord 2026-10-27)."""
    if bonus_id not in master.LOGIN_BONUS_ONCE:
        return True
    for group in days.values():
        for it in master.REWARDS.get(str(group), {}).get("items", []):
            if it["rewardType"] == 5 or (it["rewardType"] == 1 and 97000000 <= it["itemId"] < 98000000):
                return False
    return True


def ep_login(uid, req):
    """Called at every boot: advance each active login bonus once per real day and grant that day's reward;
    readState 0 makes the client show the login bonus screen."""
    pvp_status_seen(uid)
    tower_repair(uid)
    tower_floor_rewards_backfill(uid)
    repaired = tutorial_quests_repair(uid)
    now = int(config.now())
    rows = {r["loginBonusId"]: dict(r) for r in userdata.table(uid, "loginBonus")}
    grant = Grant(uid)
    changed = False
    # operator gifts (pre-registration, social media contest): done on the archived account, but new accounts
    # got them in progress (0/1) and nothing ever completed them
    gift_missions = set(master.GIFT_MISSIONS)
    gift_rows = [dict(m) for m in userdata.table(uid, "mission")]
    done = False
    for m in gift_rows:
        if m.get("missionId") in gift_missions and m.get("state", 0) < 3:
            m.update(progress=1, state=3, completedAt=now, progressUpdatedAt=now, isNotifiable=True)
            done = True
    for bonus_id in ACTIVE_LOGIN_BONUSES:
        days = master.LOGIN_BONUS.get(str(bonus_id))
        if not days:
            continue
        row = rows.setdefault(bonus_id, {"userId": uid, "loginBonusId": bonus_id, "laps": 0, "dayCount": 0,
                                         "receivedAt": 0, "readState": 1, "loginBonusLotteryRewardId": 0})
        # one reward per real day: the game clock loops (and over 4 days in raid mode), so a day counted on it
        # went backwards at each loop and players got no login bonus for days ("daily login doesn't work")
        if userdata.counter(uid, f"login_day_{bonus_id}") >= game_day(time.time()):
            continue
        userdata.set_counter(uid, f"login_day_{bonus_id}", game_day(time.time()))
        day = row["dayCount"] + 1
        if str(day) not in days:  # last day passed: next lap
            if not login_bonus_loops(bonus_id, days):
                continue
            day, row["laps"] = 1, row["laps"] + 1
        row.update(dayCount=day, receivedAt=now, readState=0)
        # rewards go straight to the inventory (the archived account holds its day-1 rewards as items, not gifts)
        for it in master.REWARDS.get(str(days.get(str(day), 0)), {}).get("items", []):
            grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
        changed = True
    bundled = pass_bundles(uid, grant)  # bundles bought before they were given
    bundled = imagination_backfill(uid, grant) or bundled
    response = {}
    if changed or done or bundled:
        changes = grant.changes()
        if changed:
            changes["loginBonus"] = list(rows.values())
        if done:
            changes["mission"] = gift_rows
        response = userdata.set_tables(uid, changes)
        if changed:
            # readState 0 shows today's bonus screen once; the client never sends it back as seen, so the screen
            # came back at every boot (tester report, 2026-10-17). Stored as seen right away: the next user/pull sends it.
            userdata.set_tables(uid, {"loginBonus": [dict(r, readState=1) for r in rows.values()]})
    elif any(r.get("readState") == 0 for r in rows.values()):
        # bonus already given today and shown: stored as seen (accounts of before the fix above)
        userdata.set_tables(uid, {"loginBonus": [dict(r, readState=1) for r in rows.values()]})
    if repaired:
        response = raid._merge(response, userdata.set_tables(uid, {"questProgress": repaired}))
    missions.on_login(uid)  # login days, time bonus windows, daily/weekly resets
    return raid._merge(response, missions.update(uid)) if response else missions.update(uid)


# Ordinal Battle (PvP). Opponents are the other accounts on this server (their active party), padded with the
# made-up supporter players. Formats (client serializers): UserPvpNpcParty FUN_014ffeec, UserPvp FUN_014fbaf0.
PVP_OPPONENTS = 3
PVP_WIN_POINT = 30
PVP_MAX_BP = 5


def pvp_row(uid: int, pvp_id: int) -> tuple[list, dict]:
    rows = [dict(r) for r in userdata.table(uid, "pvp")]
    row = next((r for r in rows if r.get("pvpId") == pvp_id), None)
    if row is None:
        info = (userdata.table(uid, "info") or [{}])[0]
        row = {"userId": uid, "pvpId": pvp_id, "activePartyId": info.get("activePartyId", 1), "point": 0,
               "maxReachingPoint": 0, "bp": PVP_MAX_BP, "rewardReceivedAt": 0, "bpUpdatedAt": int(config.now()),
               "consecutiveWinCount": 0}
        rows.append(row)
    return rows, row


def pvp_rank(pvp_id: int, point: int) -> int:
    """The Ordinal rank reached with `point` in this season (pvp.bin sub 1)."""
    return max([r for r, need in master.PVP_RANKS.get(str(pvp_id), []) if need <= point], default=1)


PVP_HARDER, PVP_SAME, PVP_EASIER = 2, 1, 0  # UserPvpNpcParty.rank


def pvp_opponent_ranks(pvp_id: int, my_point: int) -> list:
    """(difficulty, point) of the three opponents: one rank above, the same rank, one below. UserPvpNpcParty.rank
    is that difficulty, not the opponent's Ordinal rank (the client shows it from the points): the client picks the
    point rate from it (FUN_012eb7a0: 2 -> pvp.bin sub 1 field 16 x2, 0 -> field 18 x0.8, else field 17 x1). The
    Ordinal ranks sent before only looked right at rank 1 (2/1/0); from rank D up all three showed x1 (tester report,
    2026-10-17)."""
    ranks = dict(master.PVP_RANKS.get(str(pvp_id), []))
    mine = pvp_rank(pvp_id, my_point)
    out = []
    for rank in (mine + 1, mine, mine - 1):
        if rank not in ranks:
            rank = mine if rank > mine else max(rank, 0)
        low = ranks.get(rank, 0)
        high = min([need for r, need in ranks.items() if r > rank] or [low + 100]) - 1
        point = my_point if rank == mine else random.randint(low, max(low, high))
        difficulty = PVP_HARDER if rank > mine else PVP_SAME if rank == mine else PVP_EASIER
        out.append((difficulty, max(0, point)))
    return out


def pvp_opponent(uid: int, index: int, data: dict, point: int, rank: int = 0) -> dict:
    """A UserPvpNpcParty row from another account's tables (info, active party, its pcs and weapons)."""
    tables = data["tables"]
    info = (tables.get("info") or [{}])[0]
    party_id = info.get("activePartyId", 1)
    party = next((p for p in tables.get("party", []) if p.get("id") == party_id),
                 (tables.get("party") or [{"id": 1, "slots": []}])[0])
    members = {sl.get("pcId") for sl in party.get("slots", []) if sl.get("pcId")}
    # full hp/mp: stored values are what was left after the owner's last quest
    pcs = [dict(p, **dict(zip(("hp", "mp"), master.pc_max_hp_mp(p["pcId"], p.get("totalExp", 0)))))
           for p in tables.get("pc", []) if p.get("pcId") in members]
    weapons = {p.get("weaponEquipmentStockId") for p in pcs} | {p.get("armorEquipmentStockId") for p in pcs}
    stock = [e for e in tables.get("equipmentStock", []) if e.get("id") in weapons]
    return {"userId": uid, "partyIndex": index, "enemyUserId": info.get("userId", 0), "rank": rank, "point": point,
            "info": info, "party": party, "pc": pcs, "equipmentStock": stock, "imaginationStock": []}


def ep_pvp_matching(uid, req):
    """{"pvpId", "reloadFlag", "expectedConsumingBp"}: three opponents, real accounts first."""
    req = req or {}
    pvp_id = int(req.get("pvpId", 0))
    rows, me = pvp_row(uid, pvp_id)
    # "change opponents" (reloadFlag) costs expectedConsumingBp (base.bin PVP_MATCHING_CHANGE_BP = 1 before
    # PVP_MATCHING_CHANGE_TIME): it was free, so refreshing never used BP (tester report, 2026-10-17)
    cost = int(req.get("expectedConsumingBp") or 0) if req.get("reloadFlag") else 0
    if cost:
        if me["bp"] < cost:
            return {}
        me.update(bp=me["bp"] - cost, bpUpdatedAt=int(config.now()))
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserPvpStatus.json"), encoding="utf-8") as f:
        status = userdata._rekey(json.load(f), uid)
    status.update(userId=uid, pvpId=pvp_id, status=1, matchingUpdatedAt=int(config.now()), npcPartyIndex=0,
                  consumingBp=0)
    return userdata.set_tables(uid, {"pvp": rows, "pvpNpcParty": pvp_opponents(uid, me["point"], pvp_id),
                                     "pvpStatus": [status]})


def pvp_opponents(uid: int, my_point: int, pvp_id: int = 0) -> list:
    import db  # noqa: PLC0415
    slots = pvp_opponent_ranks(pvp_id, my_point)
    others = [u for (u,) in db._conn.execute("SELECT user_id FROM users WHERE user_id != ?", (uid,)).fetchall()]
    random.shuffle(others)
    opponents = []
    for other in others:
        data = userdata.load(other)
        if data["tables"].get("pc") and data["tables"].get("info"):
            rank, point = slots[len(opponents)]
            opponents.append(pvp_opponent(uid, len(opponents), data, point, rank))
        if len(opponents) == PVP_OPPONENTS:
            break
    for guest in guest_rows(uid):
        if len(opponents) == PVP_OPPONENTS:
            break
        pc = guest["pc"][0]
        party = {"userId": guest["guestUserId"], "id": 1, "emblemId": 0, "name": "", "partyIconType": 0,
                 "slots": [{"slotNumber": 1, "pcId": pc["pcId"], "assistingPcId": 0, "hp": 0, "mp": 0}]
                 + [{"slotNumber": n, "pcId": 0, "assistingPcId": 0, "hp": 0, "mp": 0} for n in range(2, 7)]}
        fake = {"tables": {"info": [guest["info"]], "party": [party], "pc": guest["pc"], "equipmentStock": []}}
        rank, point = slots[len(opponents)]
        opponents.append(pvp_opponent(uid, len(opponents), fake, point, rank))
    return opponents


def ep_pvp_start(uid, req):
    """{"pvpId", "activePartyId", "npcPartyIndex", "consumingBp"}: pay BP, remember the chosen opponent."""
    req = req or {}
    pvp_id = int(req.get("pvpId", 0))
    rows, me = pvp_row(uid, pvp_id)
    cost = int(req.get("consumingBp") or 0)
    spent = min(cost, me["bp"])
    me.update(bp=me["bp"] - spent, activePartyId=int(req.get("activePartyId") or me["activePartyId"]),
              bpUpdatedAt=int(config.now()))
    missions.on_pvp_bp(uid, spent)  # guild mission "consume N BPs in Ordinal Battle"
    status = dict((userdata.table(uid, "pvpStatus") or [{}])[0])
    status.update(userId=uid, pvpId=pvp_id, status=2, npcPartyIndex=int(req.get("npcPartyIndex") or 0),
                  consumingBp=cost)
    # both sides start at full hp/mp (the stored values are what the last quest left)
    party = next((p for p in userdata.table(uid, "party") if p.get("id") == me["activePartyId"]), {"slots": []})
    members = {sl.get("pcId") for sl in party.get("slots", []) if sl.get("pcId")}
    pcs = [dict(p, **dict(zip(("hp", "mp"), master.pc_max_hp_mp(p["pcId"], p.get("totalExp", 0)))))
           if p.get("pcId") in members else p for p in userdata.table(uid, "pc")]
    changes = {"pvp": rows, "pvpStatus": [status], "pc": pcs}
    gauge = rested_gauge(uid)
    if gauge:
        changes["anotherForce"] = gauge
    return userdata.set_tables(uid, changes)


PVP_STATUS_IDLE, PVP_STATUS_CLEARED = 1, 3


def pvp_status_seen(uid):
    """After an Ordinal battle the result was shown (the client goes home and logs in after it): back to idle, or
    every later return to the field would open the result screen again. The next user/pull sends the row."""
    rows = userdata.table(uid, "pvpStatus")
    if rows and rows[0].get("status") == PVP_STATUS_CLEARED:
        # fresh opponents (otherwise the next fight replays the beaten one and ends at once)
        pvp_id = rows[0].get("pvpId", 0)
        point = next((r.get("point", 0) for r in userdata.table(uid, "pvp") if r.get("pvpId") == pvp_id), 0)
        userdata.set_tables(uid, {"pvpStatus": [dict(rows[0], status=PVP_STATUS_IDLE, consumingBp=0,
                                                     npcPartyIndex=0)],
                                  "pvpNpcParty": pvp_opponents(uid, point, pvp_id)})


def pvp_turn_bonus(row: dict, turns: int) -> int:
    """Turn bonus x10000 from the rank row: the fewer turns, the higher (x1.10 up to 25 turns, ... x0.95)."""
    return next((ratio for limit, ratio in row["turns"] if turns <= limit), row["slowTurn"])


def ep_pvp_clear(uid, req):
    """{"pvpId", "winFlag", "elapsedTurn"}: points, rewards and the result screen (UserPvpResult).
    The screen (PvpResultPointNode::doNextAnimation) shows in turn: rankPoint (the rank's base points, pvp.bin sub
    1 field 4), consecutiveWinBonus, turnBonus and enemyBonus as ratios x10000 (11000 = x1.10), the BP spent
    (pvpStatus.consumingBp) and totalPoint; the Reward box lists rewardGroupIds then bpBonusRewardGroupIds
    (PvpResultRewardsNode, FUN_012e9360 / FUN_012e954c). All of them were 0 or empty: "x0.00", "x0", no reward
    (Discord, 2026-10-17). Points = (rankPoint x turn x opponent + streak) x BP."""
    req = req or {}
    pvp_id = int(req.get("pvpId", 0))
    rows, me = pvp_row(uid, pvp_id)
    win = bool(req.get("winFlag"))
    status = dict((userdata.table(uid, "pvpStatus") or [{}])[0])
    chosen = next((o for o in userdata.table(uid, "pvpNpcParty")
                   if o.get("partyIndex") == status.get("npcPartyIndex")), {})
    mine = pvp_rank(pvp_id, me["point"])
    rank_row = master.PVP_RANK_ROWS.get(f"{pvp_id}:{mine}") or {
        "point": PVP_WIN_POINT, "turns": [], "slowTurn": 10000, "enemy": [20000, 10000, 8000],
        "win": 0, "bp": 0, "first": 0, "streakFrom": 0, "streak": 0}
    # the chosen opponent's difficulty: x2 rank above, x1 same rank, x0.8 rank below (pvp_opponent_ranks)
    difficulty = chosen.get("rank", PVP_SAME)
    if difficulty not in (PVP_HARDER, PVP_SAME, PVP_EASIER):  # rows drawn before 2026-10-17: an Ordinal rank
        difficulty = PVP_HARDER if difficulty > mine else PVP_SAME if difficulty == mine else PVP_EASIER
    enemy_bonus = rank_row["enemy"][{PVP_HARDER: 0, PVP_SAME: 1, PVP_EASIER: 2}[difficulty]]
    turn_bonus = pvp_turn_bonus(rank_row, int(req.get("elapsedTurn") or 0))
    # BP spent on the battle (1 to 5) multiplies the points: 5 BP gave the same as 1 (Discord, 2026-10-17)
    bp_used = max(1, int(status.get("consumingBp") or 1))
    # consecutive-win bonus: the rank's points (pvp.bin sub 1 field 6, 30 at rank 1 ... 160 at rank 9) from the
    # field-5-th win in a row (2). It was a made-up min(streak, 5) x 2: "160 -> 10 -> 4" (tester report, 2026-10-28)
    streak = me["consecutiveWinCount"] + 1
    streak_bonus = rank_row.get("streak", 0) if win and rank_row.get("streak") and streak >= max(
        1, rank_row.get("streakFrom", 0)) else 0
    gained = (rank_row["point"] * turn_bonus * enemy_bonus // 10 ** 8 + streak_bonus) * bp_used if win else 0
    best_before = pvp_rank(pvp_id, me["maxReachingPoint"])
    me.update(point=me["point"] + gained, consecutiveWinCount=me["consecutiveWinCount"] + 1 if win else 0)
    me["maxReachingPoint"] = max(me["maxReachingPoint"], me["point"])
    after = pvp_rank(pvp_id, me["point"])
    # rewards: the rank's win reward, one BP bonus reward per BP above 1, and the new rank's reward the first time
    # it is reached; granted at once (the screen only shows them)
    rewards = [rank_row["win"]] if win and rank_row["win"] else []
    bp_rewards = [rank_row["bp"]] * (bp_used - 1) if win and rank_row["bp"] else []
    first = [g for g in (master.PVP_RANK_ROWS.get(f"{pvp_id}:{r}", {}).get("first", 0)
                         for r in range(best_before + 1, after + 1)) if g]
    grant = Grant(uid)
    for group in rewards + bp_rewards + first:
        for it in master.REWARDS.get(str(group), {}).get("items", []):
            grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
    season = season_points(uid, bp_used * SEASON_POINTS_PER_BP if win else 0)
    if season["seasonPoint"]:
        grant.item(season["seasonPointItemId"], season["seasonPoint"])
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserPvpResult.json"), encoding="utf-8") as f:
        result = userdata._rekey(json.load(f), uid)
    result.update(userId=uid, rankPoint=rank_row["point"] if win else 0, consecutiveWinBonus=streak_bonus,
                  turnBonus=turn_bonus if win else 0, enemyBonus=enemy_bonus if win else 0, totalPoint=gained,
                  rewardGroupIds=rewards, bpBonusRewardGroupIds=bp_rewards, firstRewardGroupIds=first,
                  rankUp=after != mine, **season)
    # status 3 = battle over: the field (WorldExplorerState, FUN_01123798) opens PvpResultUIState only on 3 (2 =
    # fighting, 1 = idle). 1 here skipped the victory animation and stats: straight home (tester report, 2026-10-17).
    # consumingBp (the screen's "BP Bonus x5"), npcPartyIndex and pvpNpcParty (the beaten opponent shown on the
    # screen) stay until the next login (pvp_status_seen), which also draws fresh opponents.
    status.update(status=PVP_STATUS_CLEARED, matchingUpdatedAt=int(config.now()))
    # the party screen showed the hp / mp / INC left by the Ordinal battle (tester report, 2026-10-28): rested like a quest
    return rest_party(uid, userdata.set_tables(uid, {"pvp": rows, "pvpResult": [result], "pvpStatus": [status],
                                                     **grant.changes()}))



def ep_pvp_last_reward_receive(uid, req):
    """{"userId", "pvpId"}: an ended Ordinal season was seen. PvpUIState::init sends it for the first UserPvp row
    whose season is over and whose rewardReceivedAt is 0 (FUN_012ec780 / FUN_012eaa18), then shows "The event has
    ended" with the final rank. The route had no handler (generic code 0): the row never changed, so the notice came
    back each time Ordinal opened and after every battle (tester report, 2026-10-23/24). The rank rewards were
    already granted during the season (ep_pvp_clear); this marks the season as received."""
    pvp_id = int((req or {}).get("pvpId") or 0)
    rows = [dict(r) for r in userdata.table(uid, "pvp")]
    now = int(config.now())
    changed = False
    for r in rows:
        if r.get("pvpId") == pvp_id and not r.get("rewardReceivedAt"):
            r["rewardReceivedAt"] = now
            changed = True
    return userdata.set_tables(uid, {"pvp": rows}) if changed else {}

# Season pass. Tables (client serializers): UserSeasonReward {userId, seasonRewardId, acquiredCount, lastAcquiredAt},
# UserSeasonPointBoost {userId, seasonPointBoostId, purchasedCount, purchasedAt, expiredNoticeReadState},
# UserPremiumPass {userId, premiumPassId, purchasedCount, purchasedAt}. The client checks the season points itself.
# Season points earned by playing. No rule was found in the master (quest/pvp results only carry seasonPoint,
# seasonPointItemId, seasonPointBoostRatio), so: 1 point per AP a cleared quest costs (quest.bin sub 3 field 5),
# 20 per BP of an Ordinal win; x2 while a bought point boost lasts (season.bin sub 2: 30 days, ratio 20000).
SEASON_POINTS_PER_AP = 1
FRIEND_POINT_ITEM = 205000007  # currency.friend_point
FRIEND_POINTS_FRIEND, FRIEND_POINTS_OTHER = 30, 10
SEASON_POINTS_PER_BP = 20


def current_season() -> str | None:
    """The season whose release window is open at the game clock (Season 9 is served extended)."""
    import missions  # noqa: PLC0415
    open_ = [sid for sid, label in master.SEASON.get("labels", {}).items()
             if label and master.SEASON["pointItems"].get(sid) and missions.is_open(label)
             and master.RELEASES.get(label)]
    return max(open_) if open_ else None


def season_points(uid: int, base: int) -> dict:
    """seasonPoint / seasonPointItemId / seasonPointBoostRatio of a result for `base` points (0 when no season)."""
    season = current_season()
    if not season or base <= 0:
        return {"seasonPoint": 0, "seasonPointItemId": 0, "seasonPointBoostRatio": 0}
    now = int(config.now())
    ratio = 10000
    for row in userdata.table(uid, "seasonPointBoost"):
        info = master.SEASON.get("boostInfo", {}).get(str(row.get("seasonPointBoostId")), {})
        if info and row.get("purchasedAt", 0) + info["days"] * 86400 > now:
            ratio = max(ratio, info["ratio"])
    return {"seasonPoint": base * ratio // 10000, "seasonPointItemId": master.SEASON["pointItems"][season],
            "seasonPointBoostRatio": ratio}


def event_season_points(uid: int, event_id: int, base: int) -> dict:
    """eventSeasonPoint / eventSeasonPointItemId / eventSeasonPointBoostRatio of a quest result of an event that has
    its own point season (master.EVENT_SEASONS), else zeros."""
    season = master.EVENT_SEASONS.get(str(event_id or 0))
    if not season or base <= 0:
        return {"eventSeasonPoint": 0, "eventSeasonPointItemId": 0, "eventSeasonPointBoostRatio": 0}
    ratio = season_points(uid, base).get("seasonPointBoostRatio") or 10000
    return {"eventSeasonPoint": base * ratio // 10000, "eventSeasonPointItemId": season[1],
            "eventSeasonPointBoostRatio": ratio}


def ep_season_reward_receive(uid, req):
    """{"seasonRewardIds": [...]}: grant each tier's reward group (once), count it in UserSeasonReward."""
    got = {r["seasonRewardId"]: dict(r) for r in userdata.table(uid, "seasonReward")}
    grant = Grant(uid)
    now = int(config.now())
    for rid in (req or {}).get("seasonRewardIds") or []:
        tier = master.SEASON["rewards"].get(str(rid))
        # the "extra" tier after level 100 (tier 101) can be received again every 500 points, up to season.bin sub 1
        # field 6 times (30; stored as "cost"); it was refused after the first time (シンキラ, 2026-10-21)
        limit = max(1, tier.get("cost", 0)) if tier else 0
        count = got.get(rid, {}).get("acquiredCount", 0)
        if not tier or count >= limit:
            continue
        for it in master.REWARDS.get(str(tier["reward"]), {}).get("items", []):
            if it["rewardType"] == 6:
                continue
            grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
        got[rid] = {"userId": uid, "seasonRewardId": rid, "acquiredCount": count + 1, "lastAcquiredAt": now}
    changes = grant.changes()
    changes["seasonReward"] = list(got.values())
    return userdata.set_tables(uid, changes)


def ep_season_reward_unlock(uid, req):
    """{"seasonRewardId", "cost"}: "Unlock Season Level" up to that tier with diamond cubes. It buys season
    points (the season's point item, season.bin sub 0 field 2) up to the tier's requirement (sub 1 field 4); the
    client counts them and the player then receives the tiers. It used to grant that one tier only, so buying
    level 1 -> 100 gave nothing on screen (tester report, 2026-10-17)."""
    req = req or {}
    cost = int(req.get("cost") or 0)
    tier = master.SEASON["rewards"].get(str(int(req.get("seasonRewardId") or 0)))
    point_item = master.SEASON.get("pointItems", {}).get(str((tier or {}).get("season", 0)))
    if not tier or not point_item or (cost and userdata.gems(uid) < cost):
        return {}
    if cost:
        userdata.add_gems(uid, -cost)
    grant = Grant(uid)
    have = grant.items.get(point_item, {}).get("amount", 0)
    if tier["points"] > have:
        grant.item(point_item, tier["points"] - have)
    return userdata.set_tables(uid, grant.changes())


def _purchase(uid: int, table: str, key: str, item_id: int, cost: int, extra: dict) -> dict:
    if cost and userdata.gems(uid) < cost:
        return {}
    if cost:
        userdata.add_gems(uid, -cost)
    rows = {r[key]: dict(r) for r in userdata.table(uid, table)}
    row = rows.setdefault(item_id, {"userId": uid, key: item_id, "purchasedCount": 0, "purchasedAt": 0, **extra})
    row.update(purchasedCount=row["purchasedCount"] + 1, purchasedAt=int(config.now()))
    return userdata.set_tables(uid, {table: list(rows.values())})


def ep_season_point_boost_buy(uid, req):
    bid = int((req or {}).get("seasonPointBoostId") or 0)
    return _purchase(uid, "seasonPointBoost", "seasonPointBoostId", bid, master.SEASON["boosts"].get(str(bid), 0),
                     {"expiredNoticeReadState": 0})


def pass_bundles(uid: int, grant) -> bool:
    """Give the content of the "Bundle Set" passes bought (season points, home and list themes: season.bin sub 3
    field 7) straight to the inventory, once per purchase. They were never given (Battle Pass bundles without their
    EXP, Discord 2026-10-18). Never through the gift box: a season point item there crashes the client. Also run at
    login for the bundles bought before this fix."""
    given = False
    for row in userdata.table(uid, "premiumPass"):
        pid = row.get("premiumPassId")
        group = master.SEASON["passes"].get(str(pid), {}).get("bundle")
        done = userdata.counter(uid, f"pass_bundle_{pid}")
        if not group or done >= row.get("purchasedCount", 0):
            continue
        for _ in range(row.get("purchasedCount", 0) - done):
            for it in master.REWARDS.get(str(group), {}).get("items", []):
                grant.reward(it["rewardType"], it["itemId"], it["amount"], it["level"])
        userdata.set_counter(uid, f"pass_bundle_{pid}", row.get("purchasedCount", 0))
        given = True
    return given


def is_imagination(item_id: int) -> bool:
    """Imaginarium ids (item.20 / imagination.*): 380xxxxxx, and 381xxxxxx for the Iskahn and Tales of ARISE Kirito
    ones. The 381 ones were missed, so they went to UserItem and never showed (tester report, Discord 2026-10-28)."""
    return 380000000 <= int(item_id) < 382000000


def imagination_backfill(uid: int, grant) -> bool:
    """The Imaginarium lost before rewardType 7 was handled. Those claimed or gifted as plain items sit in UserItem
    where the game never looks: moved to UserImaginationStock (at every login: the 381xxxxxx ones were only
    recognised later). Once per account, the Battle Pass tiers already received give theirs. (Login bonus ones
    cannot be counted back.)"""
    given = False
    for item_id, row in list(grant.items.items()):
        if is_imagination(item_id) and row.get("amount", 0) > 0:
            grant.imagination(item_id, row["amount"])
            grant.item(item_id, -row["amount"])
            given = True
    if userdata.counter(uid, "imagination_backfill"):
        return given
    userdata.set_counter(uid, "imagination_backfill", 1)
    for r in userdata.table(uid, "seasonReward"):
        tier = master.SEASON["rewards"].get(str(r.get("seasonRewardId")))
        if not tier or not r.get("acquiredCount"):
            continue
        for it in master.REWARDS.get(str(tier["reward"]), {}).get("items", []):
            if it["rewardType"] == 7:
                grant.imagination(it["itemId"], it["amount"])
                given = True
    return given


def ep_premium_pass_buy(uid, req):
    pid = int((req or {}).get("premiumPassId") or 0)
    cost = master.SEASON["passes"].get(str(pid), {}).get("cost", 0)
    out = _purchase(uid, "premiumPass", "premiumPassId", pid, cost, {})
    grant = Grant(uid)
    if out and pass_bundles(uid, grant):
        out = raid._merge(out, userdata.set_tables(uid, grant.changes()))
    return out


def _lottery_result(uid: int, price_id: int, lottery_item_ids: list, grant) -> dict:
    """Grant lottery items picked by the player and build the UserLotteryResult row."""
    results = []
    for i, item_id in enumerate(lottery_item_ids):
        it = master.LOTTERY["itemIndex"].get(str(item_id))
        if not it:
            continue
        is_new = it["rewardType"] == 5 and it["target"] not in grant.owned_pcs
        grant.reward(it["rewardType"], it["target"], it["amount"])
        # an owned character turns into crystals: the result must say so (replaceItem*), like lottery/draw. Without
        # it the client crashed on a point exchange of an owned character (Discord, 300-point free character)
        dup = it["rewardType"] == 5 and not is_new
        replace, replace_amount = dup_item(it["target"]) if dup else (0, 0)
        extra, extra_amount = dup_extra(it["target"]) if dup else (0, 0)
        results.append({"index": i, "lotteryItemId": item_id, "replaceItemId": replace,
                        "replaceItemAmount": replace_amount, "imaginationId": 0, "isNew": is_new,
                        "extraReplaceItemId": extra, "extraReplaceItemAmount": extra_amount,
                        "pickupSelectionLotteryItemId": 0})
    return {"userId": uid, "lotteryPriceId": price_id, "resultItems": results, "displayed": False,
            "upperLimitLotteryPointValue": 0}


def ep_lottery_point_exchange(uid, req):
    """Scout point exchange {"lotteryItemId", ...}: pay the item's point cost (lottery.bin sub 6 field 19) in the
    exchange lottery's point item, grant the item."""
    req = req or {}
    item_id = int(req.get("lotteryItemId") or 0)
    it = master.LOTTERY["itemIndex"].get(str(item_id))
    if not it:
        return {}
    point_item = master.LOTTERY["exchangeLottery"].get(str(it["lottery"]))
    grant = Grant(uid)
    cost = it["pointCost"] * max(int(req.get("count") or 1), 1)
    if point_item and cost:
        if grant.items.get(point_item, {}).get("amount", 0) < cost:
            return {}
        grant.item(point_item, -cost)
    # the result screen opened after the exchange looks the price up: 0 crashed the client right after the
    # exchange (null pointer, [闇を制する者] Eydis 31227910001, 2026-10-17); echo the request's price
    result = _lottery_result(uid, int(req.get("lotteryPriceId") or 0), [item_id], grant)
    changes = grant.changes()
    changes["lotteryResult"] = [result]
    return userdata.set_tables(uid, changes)


def ep_lottery_select(uid, req):
    """Selection ticket ("pick your 4-star") {"lotteryPriceId", "lotteryItemId", ...}: pay the ticket, grant it."""
    req = req or {}
    price_id = int(req.get("lotteryPriceId") or 0)
    price = master.LOTTERY["prices"].get(str(price_id), {})
    grant = Grant(uid)
    ticket = int(req.get("ticketId") or price.get("ticket") or 0)
    if ticket:
        if grant.items.get(ticket, {}).get("amount", 0) < 1:
            return {}
        grant.item(ticket, -1)
    result = _lottery_result(uid, price_id, [int(req.get("lotteryItemId") or 0)], grant)
    changes = grant.changes()
    changes["lotteryResult"] = [result]
    return userdata.set_tables(uid, changes)


def ep_lottery_pickup_select(uid, req):
    """{"lotteryPriceId"?, "lotteryItemId"}: remember the chosen pick-up on the banner's price rows."""
    req = req or {}
    item_id = int(req.get("lotteryItemId") or 0)
    lottery = (master.LOTTERY["itemIndex"].get(str(item_id)) or {}).get("lottery")
    price_id = int(req.get("lotteryPriceId") or 0)
    rows = [dict(r) for r in userdata.table(uid, "lotteryPrice")]
    targets = {pid for pid, p in master.LOTTERY["prices"].items() if p["lottery"] == lottery}
    if price_id:
        targets.add(str(price_id))
    have = {str(r["lotteryPriceId"]) for r in rows}
    for pid in targets - have:
        # a step-up banner's 11-draw starts at its first step: a row at step 0 made the client show the full price
        # (125 Diamond Cubes for step 1 became 250 once a pick-up was chosen, tester report, Discord 2026-11-03)
        p = master.LOTTERY["prices"].get(pid, {})
        steps = master.LOTTERY["steps"].get(str(p.get("lottery")), {})
        first = min((int(k) for k in steps if int(k) > 0), default=0) if p.get("type") == 2 else 0
        rows.append({"userId": uid, "lotteryPriceId": int(pid), "drawCount": 0, "dailyDrawCount": 0,
                     "currentDrawCount": 0, "currentStep": first, "lastDrawTime": 0, "currentAdditionalRateStep": 0,
                     "canDrawAdditional": False, "selectedPickupLotteryItemId": 0})
    for r in rows:
        if str(r["lotteryPriceId"]) in targets:
            r["selectedPickupLotteryItemId"] = item_id
    return userdata.set_tables(uid, {"lotteryPrice": rows})


CUSTOM_MATERIALS = (411000002, 411000003, 411000004)  # custom_material.r2 .. r4


def ep_item_custom_exchange(uid, req):
    """{"exchangeItemId"}: unlock a customization (bgm/theme/frame from custom.bin) for one custom material
    (the lowest one owned); the unlocked customization is kept as an item."""
    item_id = int((req or {}).get("exchangeItemId") or 0)
    if not item_id:
        return {}
    grant = Grant(uid)
    for mat in CUSTOM_MATERIALS:
        if grant.items.get(mat, {}).get("amount", 0) > 0:
            grant.item(mat, -1)
            break
    grant.item(item_id, 1)
    return userdata.set_tables(uid, grant.changes())


def ep_quest_boost_item_purchase(uid, req):
    """{"boostItemIds": [...]}: buy one of each quest boost item with gold (item.bin field 5). Without enough gold
    on the server the purchase stops there, but the item table is still sent: an empty answer left the client
    waiting for the boost it thought it bought, the quest screen frozen ("Freeze with skip ticket and Shia boost",
    tester report, Discord 2026-10-30)."""
    grant = Grant(uid)
    for bid in (req or {}).get("boostItemIds") or []:
        cost = master.BOOST_ITEMS.get(str(bid), 0)
        if grant.items.get(GOLD_ITEM, {}).get("amount", 0) < cost:
            print(f"BOOST purchase refused uid={uid} boost={bid}: not enough gold", flush=True)
            break
        grant.item(GOLD_ITEM, -cost)
        grant.item(int(bid), 1)
    changes = grant.changes()
    changes.setdefault("item", list(userdata.table(uid, "item")))
    return userdata.set_tables(uid, changes)


def ep_quest_retire(uid, req):
    """Abandon the running quest: put back the idle UserQuestStatus (last finished quest, type 4) and reset
    UserQuestSetting to its idle shape (questId 0, as in the archived account) -- otherwise the client still
    believes a quest is running. An empty questStatus is ignored by the client (single-row table)."""
    running = (userdata.table(uid, "questStatus") or [{}])[0]
    tower = tower_changes(uid, {"eventId": running.get("eventId", 0)}, "retire") if running.get("type") == 1 else {}
    changes = {**tower, "questStatus": [userdata.idle_quest_status(uid)],
               "questSetting": [idle_quest_setting(uid)], "stageStatus": []}
    added = set(userdata.quest_areas(uid, clear=True))
    if added:  # an area row for the current position makes the client boot into the field instead of home
        changes["area"] = [r for r in userdata.table(uid, "area") if r.get("areaId") not in added]
    return rest_party(uid, userdata.set_tables(uid, changes))


def rested_gauge(uid: int) -> list | None:
    """UserAnotherForce with the party's Incarnation (INC) gauge back to 0, or None when it already is. The client
    pushes the gauge after each battle (gauge 0..20000 = 0..200 %) and starts the next battle from it; the server
    never emptied it, so a quest or an Ordinal battle started with the last one's INC, up to 200 % (tester reports,
    Discord 2026-10-28 / 11-02). The archived account rests at 0 between quests."""
    rows = userdata.table(uid, "anotherForce")
    if not any(r.get("gauge") for r in rows):
        return None
    return [dict(r, gauge=0) for r in rows]


def rest_party(uid: int, response: dict) -> dict:
    """Back to full hp/mp and an empty INC gauge when a quest or an Ordinal battle ends (clear or retire), like the
    archived account at rest. Only quest/start healed, but the party screen showed the hp left by the last battle
    and a character knocked out in a lost lesson stayed at 0 hp (Kirito 0/987, 2026-10-17)."""
    changes = {}
    healed = healed_pcs(uid)
    if healed:
        changes["pc"] = healed
    gauge = rested_gauge(uid)
    if gauge:
        changes["anotherForce"] = gauge
    return raid._merge(response, userdata.set_tables(uid, changes)) if changes else response


def idle_quest_setting(uid):
    return {"userId": uid, "questId": 0, "level": 1, "partyId": 0, "supporterUserId": 0, "supporterPcId": 0,
            "boostItemIds": [], "eventId": 0, "raidEventBossId": 0, "bonusCampaignId": []}


STUCK_BOOTS = 2  # game boots in a row with the same quest still running before the server abandons it


def ep_start(uid, req):
    """Game boot. A running quest is resumed by the client; when it can't ("This battle cannot be resumed", e.g. it
    crashed entering the quest) it never abandons it either, and the player was stuck at every boot (tester report,
    2026-10-13). The second boot in a row with the same quest still running abandons it like quest/retire; the
    following user/pull brings the idle quest status."""
    if not uid or not db.get_user(uid):
        return {}  # first boot of a new install: no account yet
    status = (userdata.table(uid, "questStatus") or [{}])[0]
    if status.get("type") != 1:
        return {}
    key = f"boot_in_quest_{status.get('questId', 0)}_{status.get('startTime', 0)}"
    if userdata.counter(uid, key) + 1 < STUCK_BOOTS:
        userdata.set_counter(uid, key, userdata.counter(uid, key) + 1)
        return {}
    userdata.set_counter(uid, key, 0)
    ep_quest_retire(uid, {"isUserRetire": True})
    return {}


def ep_client_log(uid, req):
    with open(config.log_file("client_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "user": uid, "log": req}, ensure_ascii=False, default=str) + "\n")
    return {}


def ep_ok(uid, req):
    return {}


# path (without the /api prefix) -> handler(user_id, request_body) returning extra top-level response fields
ENDPOINTS = {
    "/user/push": ep_user_push,
    "/user/pull": ep_user_pull,
    "/quest/start": ep_quest_start,
    "/quest/retire": ep_quest_retire,
    "/quest/clear": ep_quest_clear,
    "/quest/supporter": ep_quest_supporter,
    "/quest/skip": ep_quest_skip,
    "/quest/continue": ep_quest_continue,
    "/equipment/expand": ep_equipment_expand,
    "/guest/search": ep_guest_search,
    "/guest": ep_guest,
    "/friend/follow/request": ep_friend_follow_request,
    "/friend/follow/remove": ep_friend_follow_remove,
    "/friend/follower/remove": ep_friend_follower_remove,
    "/item/exchange": ep_item_exchange,
    "/start": ep_start,
    "/login": ep_login,
    "/pvp/matching": ep_pvp_matching,
    "/pvp/start": ep_pvp_start,
    "/pvp/clear": ep_pvp_clear,
    "/pvp/last-reward/receive": ep_pvp_last_reward_receive,
    "/season-reward/receive": ep_season_reward_receive,
    "/season-reward/unlock": ep_season_reward_unlock,
    "/season-point-boost/buy": ep_season_point_boost_buy,
    "/premium-pass/buy": ep_premium_pass_buy,
    "/lottery/point/exchange": ep_lottery_point_exchange,
    "/lottery/select": ep_lottery_select,
    "/lottery/pickup/select": ep_lottery_pickup_select,
    "/item/custom/exchange": ep_item_custom_exchange,
    "/quest/boost-item/purchase": ep_quest_boost_item_purchase,
    "/mission/receive": ep_mission_receive,
    "/gift/receive": ep_gift_receive,
    "/lottery/draw": ep_lottery_draw,
    "/tutorial/skip/first": ep_tutorial_skip_first,
    "/tutorial/skip/second": ep_tutorial_skip_second,
    "/client/log": ep_client_log,
    **guild.ENDPOINTS,
    **raid.ENDPOINTS,
}

# Request headers the client compares against the response (0x1049b54); echoed back verbatim.
ECHO_HEADERS = ("REQUEST-ID", "REQUEST-SEQUENCE", "REQUEST-BODY-HASH", "ONE-TIME-TOKEN")
# every custom header is prefixed with the app code "SAOAL" (config object at 0xa266d4)
P = "SAOAL-"


def _decode_body(headers: dict, body: bytes):
    if not body:
        return None
    raw = body
    if headers.get("SAOAL-ENCRYPTION") == "1":
        try:
            raw = decrypt(body)
        except (ValueError, zlib.error):
            return {"_undecryptable": body[:64].hex()}
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        import msgpack
        return msgpack.unpackb(raw, raw=False, strict_map_key=False)
    except Exception:
        return {"_raw": raw[:256].hex()}


def handle_api(method: str, path: str, headers: dict, body: bytes, log):
    headers = {k.upper(): v for k, v in headers.items()}
    req = _decode_body(headers, body)
    logged = {k: ("***" if k == P + "DEV-TOKEN" else v) for k, v in headers.items()}  # no secret in the logs
    entry = {"t": time.time(), "method": method, "path": path, "headers": logged, "body": req}
    with open(config.log_file("api_requests.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    log(f"API {method} {path} body={json.dumps(req, ensure_ascii=False, default=str)[:300]}")

    now = int(config.now())
    payload = {"code": 0, "serverTime": now, "localeServerTime": now, "requestEncrypt": False,
               # client default is 250 (config +0x64); 0 makes it ship all its internal logs to client/log
               "remoteLogLevel": config.REMOTE_LOG_LEVEL}
    handler = ENDPOINTS.get(path[len("/api"):] if path.startswith("/api/") else path)
    refused = authorize(_user_id(headers), headers) if handler is not None else None
    if refused:
        log(f"AUTH refused {path} user={_user_id(headers)}: {refused}")
        return 403, b'{"code": 403}', "application/json", {}
    if handler is not None:
        extra = handler(_user_id(headers), req)
        payload.update(extra)
        log(f"    -> {json.dumps(extra, ensure_ascii=False)[:300]}")
    out = json.dumps(payload).encode()
    resp_headers = {P + h: headers[P + h] for h in ECHO_HEADERS if P + h in headers}
    resp_headers.update({
        P + "SERVER-TIMESTAMP": str(now),
        P + "SERVER-VERSION": "1",
        P + "SERVER-RESPONSE-CODE": str(payload.get("code", 0)),
        P + "ENCRYPTION": "0",
    })
    if headers.get(P + "ENCRYPTION") == "1":
        out = encrypt(out)
        resp_headers[P + "ENCRYPTION"] = "1"
    return 200, out, "application/json", resp_headers
