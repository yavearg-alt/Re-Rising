"""War of Underworld (raid) events: solo boss fights and guild points (see docs/NEXT_RAID.md for the research).

Flow (first version, to be checked in game):
  - exploring a raid area quest (normal quest/start + quest/clear) may reveal a boss: raidEventBossLottery chance;
  - raid/info lists the event (raidEvent: points, WP), its areas with their bosses (raidEventArea.bossIds) and the
    bosses (raidEventBossSharedStatus: level, remaining hp in encountStatus, expiry; raidEventBossStatus: this
    player's damage and battle state);
  - raid/boss/start = quest/start of the boss quest with the boss's current encounter; the client fights it and
    pushes its stageStatus; raid/boss/end takes the damage dealt (enemyDamagePoint) off the boss;
  - a beaten boss gives its level's reward, defeat points (raidEvent.defeatPoint, point rewards) and guild defeat
    points (added to the player's guild: raid/guild/ranking/*), and raises the boss group's level.
Bosses are per player for now (rescue / shared bosses come later). WP is not limited yet (always full).
"""
import copy
import json
import os
import random
import threading

import config
import db
import guild
import master
import userdata

STATE_IDLE, STATE_FIGHTING = 0, 1
RESULT_DEFEATED, RESULT_ESCAPED = 1, 2
_seq_lock = threading.Lock()


def now() -> int:
    return int(config.now())


def raid_event(event_id: int):
    return master.RAID["events"].get(str(event_id))


def is_raid_event(event_id) -> bool:
    return bool(event_id) and str(event_id) in master.RAID["events"]


def _next_boss_id() -> int:
    """Boss ids are global (bosses will later be shared with friends and guild members)."""
    with _seq_lock:
        n = int(db.get_setting("raid_boss_seq") or 0) + 1
        db.set_setting("raid_boss_seq", n)
    return 700000000 + n


def _empty_party() -> dict:
    """bestAttackParty: same shape as UserQuestResult.rankingCurrentScore.rankingParty (empty)."""
    with open(os.path.join(userdata.TEMPLATE_DIR, "UserQuestResult.json"), encoding="utf-8") as f:
        party = json.load(f)["rankingCurrentScore"]["rankingParty"]
    return copy.deepcopy(party)


# ---------------------------------------------------------------- per-player rows

def _rows(uid: int, name: str, event_id: int) -> list:
    return [dict(r) for r in userdata.table(uid, name) if r.get("eventId") == event_id]


def _others(uid: int, name: str, event_id: int) -> list:
    return [r for r in userdata.table(uid, name) if r.get("eventId") != event_id]


def event_row(uid: int, event_id: int) -> dict:
    ev = raid_event(event_id) or {}
    row = next(iter(_rows(uid, "raidEvent", event_id)), None)
    g = guild.guild_of(uid)
    if row is None:
        row = {"userId": uid, "eventId": event_id, "defeatPoint": 0, "guildDefeatPoint": 0, "wp": ev.get("wp_max", 0),
               "wpUpdatedAt": now(), "guildId": 0, "lastWorldBossDailyResultReceivedAt": 0,
               "maxReceivedWorldDefeatRewardId": 0, "lastAttackedRaidEventWorldBossLevelId": 0,
               "lastAttackedWorldBossId": 0, "lastResultShownWorldBossId": 0, "maxRaidEventGuildPointRewardId": 0}
    row["guildId"] = g["id"] if g else 0
    # WP is not limited yet: always full (the recovery rate is still unknown)
    row["wp"], row["wpUpdatedAt"] = ev.get("wp_max", 0), now()
    return row


def level_groups(uid: int, event_id: int) -> dict:
    """raidBossLevelGroupId -> UserRaidEventBossLevelGroup row."""
    return {r["raidBossLevelGroupId"]: r for r in _rows(uid, "raidEventBossLevelGroup", event_id)}


def boss_level(uid: int, event_id: int, group: int) -> dict:
    levels = master.RAID["levels"].get(str(group)) or []
    row = level_groups(uid, event_id).get(group)
    beaten = max(0, (row or {}).get("raidBossDefeatCount", 0) - (row or {}).get("reduceDefeatCount", 0))
    return levels[min(beaten, len(levels) - 1)] if levels else {}


def _level_by_id(level_id: int) -> tuple:
    for group, levels in master.RAID["levels"].items():
        for lv in levels:
            if lv["id"] == level_id:
                return int(group), lv
    return 0, {}


def boss_hp(shared: dict) -> int:
    enc = shared.get("encountStatus") or {}
    return sum(max(0, int(e.get("hp") or 0)) for e in enc.get("enemyStatus") or [])


def area_rows(uid: int, event_id: int, shared: list) -> list:
    rows = []
    for area_id, area in master.RAID["areas"].items():
        if area["event"] != event_id:
            continue
        bosses = [s["bossId"] for s in shared if s.get("areaId", _boss_area(s)) == int(area_id)]
        rows.append({"userId": uid, "raidEventAreaId": int(area_id), "eventId": event_id, "bossIds": bosses})
    return rows


def _boss_area(shared: dict) -> int:
    """Area of a boss: the one whose quests share the boss quest's chapter."""
    quest = master.RAID["quests"].get(str(shared.get("questId") - 5)) or {}  # boss quest = explore quest 1 + 5
    if quest:
        return quest["area"]
    for q in master.RAID["quests"].values():
        lot = master.RAID["lottery"].get(str(q["lottery"])) or {}
        if lot.get("boss_quest") == shared.get("questId"):
            return q["area"]
    return 0


def tables(uid: int, event_id: int, extra: dict | None = None) -> dict:
    """Every raid table of this event for the player (after expiring the bosses whose time is up)."""
    shared = _rows(uid, "raidEventBossSharedStatus", event_id)
    status = _rows(uid, "raidEventBossStatus", event_id)
    t = now()
    alive = [s for s in shared if s.get("expiredAt", 0) > t]
    gone = {s["bossId"] for s in shared} - {s["bossId"] for s in alive}
    changes = {
        "raidEvent": _others(uid, "raidEvent", event_id) + [event_row(uid, event_id)],
        "raidEventArea": _others(uid, "raidEventArea", event_id) + area_rows(uid, event_id, alive),
        "raidEventBossSharedStatus": _others(uid, "raidEventBossSharedStatus", event_id) + alive,
        "raidEventBossStatus": _others(uid, "raidEventBossStatus", event_id) + [s for s in status if s["bossId"] not in gone],
        "raidEventBossLevelGroup": _others(uid, "raidEventBossLevelGroup", event_id)
        + list(level_groups(uid, event_id).values()),
    }
    changes.update(extra or {})
    return changes


def _respond(uid: int, event_id: int, extra: dict | None = None) -> dict:
    return userdata.set_tables(uid, tables(uid, event_id, extra))


# ---------------------------------------------------------------- discovery (quest/clear of an area quest)

def spawn_boss(uid: int, event_id: int, group: int, boss_quest: int) -> dict:
    """A new boss for this player, at the current level of its group."""
    import api  # noqa: PLC0415 (api imports raid)
    level = boss_level(uid, event_id, group)
    party_id = master.PARTY_BY_LABEL.get(level.get("party", ""), 0)
    enc = api.encount(0, party_id, level.get("level", 1), 0, 0, 1, True)
    stages = master.QUEST_STAGES.get(str(boss_quest), [])
    t = now()
    boss_id = _next_boss_id()
    shared = {"userId": uid, "bossId": boss_id, "eventId": event_id, "questId": boss_quest,
              "stageId": stages[-1] if stages else 0, "raidEventBossLevelId": level.get("id", 0), "findUserId": uid,
              "bestAttackUserId": 0, "bestAttackDamage": 0, "comboCount": 0, "comboUpdatedAt": 0, "attackerCount": 0,
              "rescueUserIds": [], "encountStatus": enc, "appearedAt": t, "expiredAt": t + level.get("duration", 14400),
              "timeupAt": t + level.get("duration", 14400), "bestAttackParty": _empty_party(), "bestChargerUserId": 0,
              "bestChargerAmount": 0, "takeOverIncarnate": 0, "rescueGuildIds": [],
              "guildCombo": {"guildId": 0, "comboCount": 0, "comboUpdatedAt": 0}, "worldBossId": 0}
    status = {"userId": uid, "bossId": boss_id, "isFriendRescue": False, "damage": 0, "playCount": 0,
              "eventId": event_id, "acquireBonusCharacterIds": [], "escapeReadState": 0, "appearWarning": 0,
              "stageId": shared["stageId"], "findUserId": uid, "bossBattleState": STATE_IDLE,
              "bossStartHp": boss_hp(shared), "bossStartIncarnate": 0, "chargeIncarnateAmount": 0,
              "isSentRescue": False, "isGuildRescue": False, "consumingWp": 0}
    return {"raidEventBossSharedStatus": userdata.table(uid, "raidEventBossSharedStatus") + [shared],
            "raidEventBossStatus": userdata.table(uid, "raidEventBossStatus") + [status]}


# Boss appearance after a raid area quest. Off by default: the first boss that appeared in production
# (2026-10-12, doktorDark) made the client fail at every start ("error 200") -- its rows are not what the
# client expects yet. SAOUB_RAID_BOSSES=1 turns it back on (tests, BlueStacks checks).
SPAWN_BOSSES = os.environ.get("SAOUB_RAID_BOSSES", "0") == "1"


def on_quest_clear(uid: int, quest_id: int, event_id: int) -> dict:
    """quest/clear of a raid area quest: the boss lottery. Returns the raid tables to add to the response."""
    quest = master.RAID["quests"].get(str(quest_id))
    if not quest:
        return {}
    event_id = event_id or quest["event"]
    lot = master.RAID["lottery"].get(str(quest["lottery"])) or {}
    if lot and SPAWN_BOSSES and random.randrange(10000) < lot["chance"]:
        userdata.set_tables(uid, spawn_boss(uid, event_id, lot["group"], lot["boss_quest"]))
    return tables(uid, event_id)


# ---------------------------------------------------------------- endpoints

def _int(req, key) -> int:
    try:
        return int((req or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _boss(uid: int, event_id: int, boss_id: int):
    shared = next((s for s in _rows(uid, "raidEventBossSharedStatus", event_id) if s["bossId"] == boss_id), None)
    status = next((s for s in _rows(uid, "raidEventBossStatus", event_id) if s["bossId"] == boss_id), None)
    return shared, status


def _replace(uid: int, name: str, row: dict) -> list:
    return [row if (r.get("bossId") == row["bossId"] and r.get("eventId") == row["eventId"]) else r
            for r in userdata.table(uid, name)]


def ep_info(uid, req):
    """{"eventId"}"""
    return _respond(uid, _int(req, "eventId"))


def ep_boss_info(uid, req):
    """{"eventId", "bossId"}"""
    return _respond(uid, _int(req, "eventId"))


def ep_boss_start(uid, req):
    """quest/start body + {"bossId", "consumingWp"}: the boss quest, starting from the boss's remaining hp."""
    import api  # noqa: PLC0415
    req = dict(req or {})
    event_id, boss_id = _int(req, "eventId"), _int(req, "bossId")
    shared, status = _boss(uid, event_id, boss_id)
    if not shared or boss_hp(shared) <= 0:
        return {"code": ERR_BOSS_GONE}
    req["questId"] = shared["questId"]
    start = api.ep_quest_start(uid, req)
    stages = []
    for st in userdata.table(uid, "stageStatus"):
        st = dict(st)
        if st.get("stageId") == shared["stageId"] or (len(stages) == 0 and not st.get("bossEncountStatus")):
            st["bossEncountStatus"] = [copy.deepcopy(shared["encountStatus"])]
        stages.append(st)
    status.update(bossBattleState=STATE_FIGHTING, bossStartHp=boss_hp(shared), consumingWp=_int(req, "consumingWp"),
                  playCount=status.get("playCount", 0) + 1)
    extra = {"stageStatus": stages, "raidEventBossStatus": _replace(uid, "raidEventBossStatus", status)}
    raid = userdata.set_tables(uid, tables(uid, event_id, extra))
    return _merge(start, raid)


def _merge(a: dict, b: dict) -> dict:
    out = {"hash": dict(a.get("hash") or {}), "table": {"replace": dict((a.get("table") or {}).get("replace") or {})}}
    out["hash"].update(b.get("hash") or {})
    out["table"]["replace"].update((b.get("table") or {}).get("replace") or {})
    for k, v in a.items():
        if k not in ("hash", "table"):
            out[k] = v
    return out


def _damage_dealt(uid: int, shared: dict) -> int:
    """Damage the player's battle dealt to the boss, from the stageStatus the client pushed."""
    for st in userdata.table(uid, "stageStatus"):
        if st.get("questId") != shared["questId"]:
            continue
        for e in st.get("bossEncountStatus") or []:
            dealt = int(e.get("enemyDamagePoint") or 0)
            if not dealt:  # fallback: hp lost by the enemies during the fight
                start = {x.get("enemyIndex"): int(x.get("hp") or 0)
                         for x in (shared["encountStatus"].get("enemyStatus") or [])}
                dealt = sum(max(0, start.get(x.get("enemyIndex"), 0) - int(x.get("hp") or 0))
                            for x in e.get("enemyStatus") or [])
            return max(0, dealt)
    return 0


def _apply_damage(shared: dict, damage: int):
    left = damage
    for e in shared["encountStatus"].get("enemyStatus") or []:
        hit = min(left, max(0, int(e.get("hp") or 0)))
        e["hp"] = int(e.get("hp") or 0) - hit
        left -= hit


def ep_boss_end(uid, req):
    """{"eventId", "bossId", "isUserRetire"}: end of a boss battle (instead of quest/clear)."""
    import api  # noqa: PLC0415
    event_id, boss_id = _int(req, "eventId"), _int(req, "bossId")
    shared, status = _boss(uid, event_id, boss_id)
    qs = (userdata.table(uid, "questStatus") or [{}])[0]
    idle = dict(qs, type=4)
    userdata.idle_quest_status(uid, save=idle)
    changes = {"questStatus": [idle], "questSetting": [api.idle_quest_setting(uid)]}
    if not shared:
        return userdata.set_tables(uid, tables(uid, event_id, changes))
    damage = 0 if (req or {}).get("isUserRetire") else _damage_dealt(uid, shared)
    _apply_damage(shared, damage)
    status.update(damage=status.get("damage", 0) + damage, bossBattleState=STATE_IDLE)
    if damage > shared.get("bestAttackDamage", 0):
        shared.update(bestAttackDamage=damage, bestAttackUserId=uid)
    shared["attackerCount"] = max(shared.get("attackerCount", 0), 1)
    changes["raidEventBossSharedStatus"] = _replace(uid, "raidEventBossSharedStatus", shared)
    changes["raidEventBossStatus"] = _replace(uid, "raidEventBossStatus", status)
    if boss_hp(shared) <= 0:
        changes.update(_defeated(uid, event_id, shared, status))
    return userdata.set_tables(uid, tables(uid, event_id, changes))


def _defeated(uid: int, event_id: int, shared: dict, status: dict) -> dict:
    """Boss beaten: points, rewards (gift box), guild points, next level for the group; the boss leaves the area."""
    import api  # noqa: PLC0415
    group, level = _level_by_id(shared["raidEventBossLevelId"])
    ev = event_row(uid, event_id)
    find_bonus = level.get("defeat_point", 0) // 10 if shared.get("findUserId") == uid else 0
    total = level.get("defeat_point", 0) + find_bonus
    before = ev.get("defeatPoint", 0)
    ev["defeatPoint"] = before + total
    ev["guildDefeatPoint"] = ev.get("guildDefeatPoint", 0) + level.get("guild_point", 0)
    gifts = list(userdata.table(uid, "gift"))
    reward_groups = [level["reward"]] if level.get("reward") else []
    point_ids = []
    for points, reward_id, group_id in master.RAID["point_rewards"].get(str(event_id), []):
        if before < points <= ev["defeatPoint"]:
            point_ids.append(reward_id)
            reward_groups.append(group_id)
    for g in reward_groups:
        api.add_gifts(uid, gifts, g)
    add_guild_points(uid, event_id, level.get("guild_point", 0))
    groups = level_groups(uid, event_id)
    row = groups.setdefault(group, {"userId": uid, "eventId": event_id, "raidBossLevelGroupId": group,
                                    "raidBossDefeatCount": 0, "reduceDefeatCount": 0})
    row["raidBossDefeatCount"] += 1
    result = {"userId": uid, "bossId": shared["bossId"], "damageDefeatPoint": level.get("defeat_point", 0),
              "damageContributeRate": 10000, "bestAttackBonusDefeatPoint": 0, "findBonusDefeatPoint": find_bonus,
              "friendRescueBonusDefeatPoint": 0, "specialBonusDefeatRate": 0, "totalDefeatPoint": total,
              "rewardGroupIds": reward_groups, "pointRewardIds": point_ids, "eventDropBonus": [], "eventId": event_id,
              "bossResultState": RESULT_DEFEATED, "readState": 0, "shouldShowMvp": False, "finishedAt": now(),
              "guildDefeatPointBonusRate": 0, "guildDefeatPoint": level.get("guild_point", 0), "seasonPoint": 0,
              "seasonPointItemId": 0, "seasonPointBoostRatio": 0, "eventSeasonPoint": 0, "eventSeasonPointItemId": 0,
              "eventSeasonPointBoostRatio": 0}
    keep = lambda name: [r for r in userdata.table(uid, name) if r.get("bossId") != shared["bossId"]]  # noqa: E731
    return {"raidEvent": _others(uid, "raidEvent", event_id) + [ev], "gift": gifts,
            "raidEventBossLevelGroup": _others(uid, "raidEventBossLevelGroup", event_id) + list(groups.values()),
            "raidEventBossResult": userdata.table(uid, "raidEventBossResult") + [result],
            "raidEventBossSharedStatus": keep("raidEventBossSharedStatus"),
            "raidEventBossStatus": keep("raidEventBossStatus")}


def ep_boss_withdraw(uid, req):
    """{"eventId", "bossId"}: the player gives the boss up."""
    event_id, boss_id = _int(req, "eventId"), _int(req, "bossId")
    keep = lambda name: [r for r in userdata.table(uid, name) if r.get("bossId") != boss_id]  # noqa: E731
    return _respond(uid, event_id, {"raidEventBossSharedStatus": keep("raidEventBossSharedStatus"),
                                    "raidEventBossStatus": keep("raidEventBossStatus")})


def ep_boss_status_update(uid, req):
    """{"eventId", "escapeBossIds", "appearWarningBossIds"}: the client saw these escape / appear warnings."""
    event_id = _int(req, "eventId")
    escaped = set((req or {}).get("escapeBossIds") or [])
    warned = set((req or {}).get("appearWarningBossIds") or [])
    rows = []
    for r in userdata.table(uid, "raidEventBossStatus"):
        r = dict(r)
        if r.get("bossId") in escaped:
            r["escapeReadState"] = 1
        if r.get("bossId") in warned:
            r["appearWarning"] = 1
        rows.append(r)
    return _respond(uid, event_id, {"raidEventBossStatus": rows})


def ep_wp_item_recover(uid, req):
    """{"eventId", "items": [{"itemId", "amount"}]}: WP potions (WP is always full for now: just acknowledge)."""
    return _respond(uid, _int(req, "eventId"))


def ep_ack(uid, req):
    """Rescue / MVP: not implemented yet (bosses are not shared); answer with the current raid state."""
    event_id = _int(req, "eventId")
    return _respond(uid, event_id) if event_id else {}


# ---------------------------------------------------------------- guild points and rankings

def add_guild_points(uid: int, event_id: int, points: int):
    g = guild.guild_of(uid)
    if not g or not points:
        return
    raid = g.setdefault("raid", {}).setdefault(str(event_id), {"points": 0, "members": {}})
    raid["points"] += points
    raid["members"][str(uid)] = raid["members"].get(str(uid), 0) + points
    guild.save(g)


def guild_ranking(event_id: int) -> list:
    """[(rank, guild, points)] by points, best first (ties share the order of creation)."""
    scored = [(g.get("raid", {}).get(str(event_id), {}).get("points", 0), g) for g in db.list_guilds()]
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    return [(i + 1, g, pts) for i, (pts, g) in enumerate(scored)]


def ep_guild_ranking_list(uid, req):
    """{"eventId", "guildId", "offset", "limit"}: guilds by raid defeat points."""
    event_id = _int(req, "eventId")
    offset, limit = _int(req, "offset"), _int(req, "limit") or 50
    ranking = guild_ranking(event_id)
    rows = [{"userId": uid, "eventId": event_id, "guildId": g["id"], "rank": rank, "guildDefeatPoint": pts}
            for rank, g, pts in ranking[offset:offset + limit]]
    shown = [g for _, g, _ in ranking[offset:offset + limit]]
    extra = {"raidEventGuildRankingScore": rows}
    extra.update({k: v for k, v in guild.tables(uid, shown).items() if k in ("guild", "guildMember", "info")})
    return _respond(uid, event_id, extra)


def ep_guild_ranking_member_list(uid, req):
    """{"eventId", "guildId"}: members of a guild by their raid defeat points."""
    event_id = _int(req, "eventId")
    g = guild.load(_int(req, "guildId")) or guild.guild_of(uid)
    members = (g or {}).get("raid", {}).get(str(event_id), {}).get("members", {})
    order = sorted((g or {}).get("members", {}), key=lambda u: (-members.get(u, 0), int(u)))
    rows = [{"userId": uid, "guildId": g["id"], "guestId": int(u), "rank": i + 1, "guildDefeatPoint": members.get(u, 0)}
            for i, u in enumerate(order)] if g else []
    extra = {"raidEventGuildRankingMemberScore": rows}
    if g:
        extra["guest"] = guild.with_guests(uid, [int(u) for u in order])
    return _respond(uid, event_id, extra)


ERR_BOSS_GONE = 28122811  # same code as a disbanded guild for now ("no longer exists"); to check in game

ENDPOINTS = {
    "/raid/info": ep_info,
    "/raid/boss/info": ep_boss_info,
    "/raid/boss/start": ep_boss_start,
    "/raid/boss/end": ep_boss_end,
    "/raid/boss/withdraw": ep_boss_withdraw,
    "/raid/boss/status/update": ep_boss_status_update,
    "/raid/wp/item/recover": ep_wp_item_recover,
    "/raid/boss/rescue": ep_ack,
    "/raid/rescue/save": ep_ack,
    "/raid/mvp/elect": ep_ack,
    "/raid/guild/ranking/list": ep_guild_ranking_list,
    "/raid/guild/ranking/member/list": ep_guild_ranking_member_list,
}
