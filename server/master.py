"""Server-side views of the master data (decoded with tools/flatmaster.py, cached as JSON).

Field meanings were identified by matching values (see docs/PROTOCOL.md "Master data"):
  quest.bin sub-table 2  (Quest):      0 questId, 1 chapterId, 4 name, 8 label ("1-1-1")
  quest.bin sub-table 3  (QuestLevel): 0 questLevelId, 1 questId, 2 level, 3 user exp, 4 gold (guessed),
                                       14 all-bonus-missions reward group, 20 clear reward group
  enemy.bin sub-table 7  (EnemyDrop):  1 tier, 3 kind (0 seed, 1 seed, 2 exp item), 5 quest level, 6 rewardGroupId,
                                       7 chance (/10000); one row per element in each kind
  quest.bin sub-table 10 (Stage):      0 stageId, 1 questId, 2 order, 6 areaId
  quest.bin sub-table 11 (StageEncount): 2 stageId, 4 quest level (absent = 1), 6 random encounter count,
                                         7/8 walking distance before each encounter (min/max), 9 enemy party group (-1 = none),
                                         10 boss enemy_party label, 11/12 enemy level min/max
  mission.bin sub-table 0 (Mission):    0 missionId, 14 rewardGroupId
  reward.bin sub-table 0  (Reward):     1 rewardGroupId, 2 rewardType (1 item, 2 equipment, 3 diamond cubes, 5 character),
                                        3 target id, 4 level, 5 amount
  reward.bin sub-table 1  (RewardGroup): 0 rewardGroupId, 2 reasonId (shown in the gift box)
  item.bin sub-table 16   (PcLimitBreakItem): 0 itemId, 6 pcId
  pc.bin sub-table 0      (Character):  0 pcId, 1 name, 15 base hp, 16 base mp  (pc.bin was missing from the first
                                        extraction: master/divided/pc.enc)
  treasure.bin sub-table 0 (TreasureType): 0 id, 1 label (treasure_type.chest.modern / .mining)
  treasure.bin sub-table 1 (Treasure):  0 treasureId, 1 tier (10, 20, ... like the stage level), 3 treasure type,
                                        4 quest level, 5 rewardGroupId, 6 weight
  map.bin sub-table 6 (AreaObject):     2 areaId, 6 position (line block), 10 treasure type label (chests only)
  lottery.bin sub-table 2 (LotteryRate):  1 lotteryId, 2 rarity, 3 weight (/10000)
  lottery.bin sub-table 3 (LotteryPrice): 0 lotteryPriceId, 1 lotteryId, 2 draw type (1 single, 2 eleven), 3 currency
                                          (1 diamond cubes, 2 ticket), 4 ticket itemId, 5 cost, 7 draw limit
  lottery.bin sub-table 4 (LotteryStep):  1 lotteryId, 2 step, 3 label, 4 bonus (1 none, 2 discount, 3 boosted rates
                                          from lottery 6 for every draw, 4 last draw from lottery 6), 5 discounted cost,
                                          6 alternative lotteryId, 7 next step (absent = step + 1)
  lottery.bin sub-table 6 (LotteryItem):  0 lotteryItemId, 1 lotteryId, 2 rarity, 3 weight, 4 rewardType, 5 target id,
                                          6 amount
  enemy.bin sub-table 2  (EnemyParty):   0 enemyPartyId, 1 group, 3 weight, 5 label, 37 experience, 38 gold,
                                         41..45 enemyBaseIds
  enemy.bin sub-table 4  (EnemyParam):   2 enemyBaseId, 6 level, 9 hp
"""
import json
import os
import sys

import config

sys.path.insert(0, os.path.join(config.ROOT, "tools"))
from flatmaster import Master  # noqa: E402

MASTER_DIR = os.path.join(config.DATA_DIR, "extracted", "master_final")
CACHE_DIR = os.path.join(config.DATA_DIR, "server_cache")


def _cached(name: str, build):
    path = os.path.join(CACHE_DIR, f"master_{name}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    value = build()
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    return value


def _build_quest_stages():
    m = Master(open(os.path.join(MASTER_DIR, "quest.bin"), "rb").read())
    stages = {}
    for r in m.rows(10):
        stages.setdefault(str(r.get(1)), []).append((r.get(2) or 0, r.get(0)))
    return {q: [sid for _, sid in sorted(v)] for q, v in stages.items()}


def _build_stage_areas():
    m = Master(open(os.path.join(MASTER_DIR, "quest.bin"), "rb").read())
    return {str(r.get(0)): r.get(6) for r in m.rows(10) if r.get(6)}


def _build_quest_levels():
    m = Master(open(os.path.join(MASTER_DIR, "quest.bin"), "rb").read())
    return {f"{r.get(1)}:{r.get(2) or 1}": {"id": r.get(0), "exp": r.get(3) or 0, "gold": r.get(4) or 0,
                                            "bonus_reward": r.get(14) or 0, "clear_reward": r.get(20) or 0,
                                            "drops": r.get(10) or 0, "ap": r.get(5) or 0,
                                            "missions": [r.get(k) or 0 for k in (11, 12, 13)]}
            for r in m.rows(3)}


def _build_event_quest_drops():
    """questId -> drop table of the event quest (event.bin sub 2 field 8): event items, equipment generation armors...
    rolled at clear on top of the quest level's own table (quest.bin sub 3 field 10, seeds and buds)."""
    return {str(r.get(3)): r.get(8) for r in _json_rows("event", 2) if r.get(3) and r.get(8)}


def _build_event_boss_quests():
    """["eventId:questId"] of the event boss quests (event.bin sub 2 field 2 = 2, Enum_EventQuestType BOSS; absent
    = 1 EVENT). The client files their stages under filterType 2 (FUN_012757e8 -> FUN_0140b838)."""
    return sorted(f"{r.get(1)}:{r.get(3)}" for r in _json_rows("event", 2) if r.get(2) == 2 and r.get(1) and r.get(3))


def _build_event_boss_tickets():
    """"eventId:questId" -> [ticket label, tickets per run] of the Ticket Boss quests (event.bin sub 2: field 5 the
    quest_ticket.* item label, field 6 the cost: 10 / 20 / 40)."""
    return {f"{r.get(1)}:{r.get(3)}": [r.get(5), r.get(6) or 1] for r in _json_rows("event", 2)
            if r.get(1) and r.get(3) and str(r.get(5) or "").startswith("quest_ticket.")}


def _build_pc_imaginations():
    """pcId -> the character's own Imaginarium (item.bin sub 20 field 8 = pcId, label *_4lb: earned at limit break 4)."""
    return {str(r[8]): r[0] for r in _json_rows("item", 20) if r.get(8)}


def _build_exchangeable_imaginations():
    """Imaginarium one can get from the Imaginarium exchange (item.bin sub 20 field 10 = 1: the characters' own)."""
    return sorted(r[0] for r in _json_rows("item", 20) if r.get(10) == 1)


def _build_event_seasons():
    """eventId -> [season id, point item] of the event point seasons (season.bin sub 0 rows with an eventId in field
    1: the event's own Battle Pass, e.g. "Unleash Blading" 601101000202 -> 5010220906 / 405220906). Their points are
    the results' eventSeasonPoint."""
    return {str(r[1]): [r[0], r.get(2) or 0] for r in _json_rows("season", 0) if r.get(1) and r.get(2)}


def _build_event_play_limits():
    """eventId -> runs allowed per game day (event.bin sub 0 field 4, with field 5 = 1): the material collection
    events (Vigil Key, Growth Plant, Blessing of EXP: 5 a day). The client shows "limit - UserEventProgress.playCount"
    for the current day."""
    return {str(r.get(0)): r.get(4) for r in _json_rows("event", 0) if r.get(0) and r.get(4) and r.get(5) == 1}


def _build_banners():
    """Scout banners for the admin panel's Banners tab: release label -> [[lotteryId, Japanese name, English
    headline], ...] (lottery.bin sub 1: field 3 name, 12 release label; English from the localization
    description, its first lines without colour tags)."""
    import re  # noqa: PLC0415
    with open(os.path.join(JSON_DIR, "localization.0.json"), encoding="utf-8") as f:
        desc = {r.get("1"): r.get("4") for r in json.load(f) if r.get("3") == "description" and r.get("4")}
    out = {}
    for r in _json_rows("lottery", 1):
        label = r.get(12)
        if not label:
            continue
        text = re.sub(r"\[/?c[^\]]*\]", "", desc.get(str(r.get(0)), "") or "")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
        out.setdefault(label, []).append([r.get(0), r.get(3) or "", " ".join(lines)[:140]])
    return out


def _build_tower_event_quests():
    """Cathedral floors (event.bin sub 3, TowerEventQuest): "eventId:questId" -> towerEventQuestId (field 0; 1 event,
    2 quest, 3 towerEventQuestGroupId, 14 first floor using it). UserTowerEventStatus.towerEventQuestId is this id,
    not the quest id: with the quest id the field found no floor and never started its battle."""
    return {f"{r.get(1)}:{r.get(2)}": r.get(0) for r in _json_rows("event", 3) if r.get(0) and r.get(1) and r.get(2)}


def _build_tower_floor_rewards():
    """Cathedral floor first-clear rewards (event.bin sub 3 field 13): questId -> reward group (Fragments, Diamond
    Cubes, Sacred Arts Manuals, flowers...)."""
    return {str(r.get(2)): r.get(13) for r in _json_rows("event", 3) if r.get(2) and r.get(13)}


def _build_quest_guests():
    """Quests with an imposed supporter (quest.bin sub 9, questSupporter: 1 questId, 4 pcId, 5 level, 6 name):
    questId -> [[row id, pcId, level, name], ...] (tutorials, Beginner's Residence lessons)."""
    out = {}
    for r in _json_rows("quest", 9):
        if r.get(1) and r.get(4):
            out.setdefault(str(r[1]), []).append([r[0], r[4], r.get(5) or 1, r.get(6) or ""])
    return out


def _build_pvp_rank_rows():
    """Ordinal result per season and rank: "pvpId:rank" -> pvp.bin sub 1 fields the result screen uses: 4 rank
    points, turn bonus (x10000) by turns (9 -> 10, 11 -> 12, 13 -> 14, else 15; the client grades it S-C against
    10/12/14), opponent bonus (x10000) 16 rank above / 17 same / 18 below, reward groups 19 win, 20 BP bonus,
    21 first time at this rank, consecutive-win bonus points 6 from the field-5-th win in a row (2: 30 at rank 1 ...
    160 at rank 9)."""
    out = {}
    for r in _json_rows("pvp", 1):
        out[f"{r.get(1)}:{r.get(2)}"] = {
            "point": r.get(4) or 0,
            "turns": [[r.get(9) or 0, r.get(10) or 10000], [r.get(11) or 0, r.get(12) or 10000],
                      [r.get(13) or 0, r.get(14) or 10000]],
            "slowTurn": r.get(15) or 10000,
            "enemy": [r.get(16) or 10000, r.get(17) or 10000, r.get(18) or 10000],
            "win": r.get(19) or 0, "bp": r.get(20) or 0, "first": r.get(21) or 0,
            "streakFrom": r.get(5) or 0, "streak": r.get(6) or 0}
    return out


def _build_tower_floors():
    """Cathedral floors in play order: eventId -> [[towerEventQuestId, towerLevel (field 3: 1 normal, 2 hard...),
    top floor number (field 14)], ...]. The client lists the floors of (eventId, towerLevel) the same way
    (FUN_0131caf4, field 3) and unlocks up to the one after UserTowerEventStatus.towerEventQuestId."""
    out = {}
    for r in sorted(_json_rows("event", 3), key=lambda r: r.get(0) or 0):
        if r.get(0) and r.get(1):
            out.setdefault(str(r[1]), []).append([r[0], r.get(3) or 1, r.get(14) or 0])
    return out


def _build_pvp_ranks():
    """Ordinal Battle ranks per season: pvpId -> [[rank, minimum points], ...] (pvp.bin sub 1: field 1 season,
    2 rank, 3 points needed; rank 1 needs none)."""
    out = {}
    for r in _json_rows("pvp", 1):
        out.setdefault(str(r.get(1)), []).append([r.get(2) or 0, r.get(3) or 0])
    return {k: sorted(v) for k, v in out.items()}


def _build_missions():
    """Every mission: missionId -> [type (4), category (2: 1 normal, 2 daily, 3 event, 4 beginner, 5 weekly,
    6 tower), target (8, default 1), param (9), level (10), extra (11), release label (18), name (1), tracked by the
    server (16 = 1; the others, such as character levels, are counted by the client)]."""
    out = {}
    for r in _json_rows("mission", 0):
        out[str(r[0])] = [r.get(4) or 0, r.get(2) or 0, int(r.get(8) or 1), str(r.get(9) or ""),
                          str(r.get(10) or ""), str(r.get(11) or ""), r.get(18) or "", r.get(1) or "",
                          r.get(16) == 1]
    return out


def _build_releases():
    """Release windows (base.bin sub 15): label -> [start, end] as "YYYY/MM/DD hh:mm:ss" (JST), "" when open."""
    return {r.get(1): [r.get(5) or "", r.get(6) or ""] for r in _json_rows("base", 15) if r.get(1)}


def _build_event_kinds():
    """eventId -> event kind (event.bin sub 0 field 1: 2 = growth events such as EXP or plants, else story/limited)."""
    return {str(r[0]): r.get(1) or 0 for r in _json_rows("event", 0)}


def _build_event_types():
    """eventId -> event type (event.bin sub 0 field 3, Enum_EventType: 1 normal, 3 tower, 6 raid, 7 ranking,
    8 floor ranking, 9 rush). The client tests it with FUN_013f7154(event, type)."""
    return {str(r[0]): r.get(3) or 0 for r in _json_rows("event", 0)}


def _build_raid():
    """War of Underworld raids (raid_event.*; a sheet is identified by its id prefix, see docs/NEXT_RAID.md).
    Exploring an area quest (raidEventQuest) may reveal a boss (raidEventBossLottery: chance /10000, boss quest,
    level group); the boss's level (raidEventBossLevel) rises with the group's defeat count."""
    sheets = {}
    for n in range(19):
        if not os.path.exists(os.path.join(JSON_DIR, f"raid_event.{n}.json")):
            continue
        rows = _json_rows("raid_event", n)
        if rows:
            sheets[str(rows[0][0])[:4]] = rows
    out = {"events": {}, "areas": {}, "quests": {}, "lottery": {}, "levels": {}, "point_rewards": {},
           "guild_point_rewards": {}, "guild_rank_rewards": {}, "member_rank_rewards": {}}
    for r in sheets.get("6041", []):
        out["events"][str(r[1])] = {"id": r[0], "wp_max": r.get(3) or 0, "map": r.get(2) or ""}
    for r in sheets.get("6042", []):
        out["areas"][str(r[0])] = {"event": r.get(1), "chapter": r.get(2)}
    chapter_area = {a["chapter"]: int(k) for k, a in out["areas"].items()}
    quest_chapter = {r[0]: r.get(1) for r in _json_rows("quest", 2)}
    for r in sheets.get("6043", []):
        out["quests"][str(r.get(2))] = {"event": r.get(1), "lottery": r.get(3),
                                        "area": chapter_area.get(quest_chapter.get(r.get(2)), 0)}
    for r in sheets.get("6044", []):
        out["lottery"][str(r.get(1))] = {"chance": r.get(2) or 0, "boss_quest": r.get(3), "group": r.get(5)}
    for r in sheets.get("6045", []):
        out["levels"].setdefault(str(r.get(2)), []).append({
            "id": r[0], "index": r.get(3) or 0, "level": r.get(4) or 1, "party": r.get(5) or "",
            "defeat_point": r.get(7) or 0, "guild_point": r.get(8) or 0, "wp": r.get(9) or 0,
            "reward": r.get(11) or 0, "duration": r.get(13) or 14400,
            "bonus": [r.get(k) for k in (19, 21, 23) if r.get(k)]})
    for v in out["levels"].values():
        v.sort(key=lambda x: x["index"])
    for r in sheets.get("6046", []):
        out["point_rewards"].setdefault(str(r.get(1)), []).append([r.get(2) or 0, r[0], r.get(4) or 0])
    for r in sheets.get("6064", []):
        out["guild_point_rewards"].setdefault(str(r.get(1)), []).append([r.get(2) or 0, r[0], r.get(3) or 0])
    for r in sheets.get("6061", []):  # guild rank reward: [rank from, rank to, id, reward group]
        out["guild_rank_rewards"].setdefault(str(r.get(1)), []).append([r.get(2) or 0, r.get(3) or 0, r[0], r.get(4) or 0])
    for r in sheets.get("6063", []):  # member rank reward inside a guild rank band: [band id, from, to, id, group]
        out["member_rank_rewards"].setdefault(str(r.get(1)), []).append(
            [r.get(2) or 0, r.get(3) or 0, r.get(4) or 0, r[0], r.get(5) or 0])
    for v in (out["point_rewards"], out["guild_point_rewards"]):
        for rows in v.values():
            rows.sort()
    return out


def _build_bonus_missions():
    """Quest bonus missions (quest.bin sub 7): id -> {type (field 1; none = party HP), param (field 2), reward
    group (field 4, one Diamond Cube)}."""
    return {str(r[0]): {"type": r.get(1) or 0, "param": r.get(2) or 0, "reward": r.get(4) or 0}
            for r in _json_rows("quest", 7)}


def _build_pc_characters():
    """pcId -> character id (pc.0 field 8, e.g. 132000001 Kirito): bonus missions about who is in the party."""
    return {str(r[0]): r.get(8) or 0 for r in _json_rows("pc", 0)}


def _build_pc_traits():
    """pcId -> [gender (pc.0 field 5: 1 man, 2 woman), weapon type (field 10, e.g. 202000001 sword)]: the Cathedral
    missions "with only Woman characters" / "with only Sword"."""
    return {str(r[0]): [r.get(5) or 0, r.get(10) or 0] for r in _json_rows("pc", 0)}


def _build_quest_drops():
    """Quest clear drops (quest.bin sub 8, referenced by quest level field 10): table -> {slot: [[weight, group]]}.
    Each slot (field 3) rolls once out of 10000: one row by weight (field 5), nothing in the remainder. Field 2
    (equal to the slot in most rows) is not understood yet and is ignored."""
    out = {}
    for r in _json_rows("quest", 8):
        out.setdefault(str(r.get(1)), {}).setdefault(str(r.get(3) or 1), []).append([r.get(5) or 0, r.get(4) or 0])
    return out


def _build_enemy_drops():
    """ "tier:questLevel" -> {kind: [[chance, rewardGroupId] x 7 elements]}; kind = field 3 (0 seed, 1 second seed,
    2 exp item); within a kind the rows are ordered by element (none, fire, water, earth, wind, light, dark)."""
    out = {}
    for r in sorted(_json_rows("enemy", 7), key=lambda r: r[0]):
        kinds = out.setdefault(f"{r.get(1)}:{r.get(5) or 1}", {})
        kinds.setdefault(str(r.get(3) or 0), []).append([r.get(7) or 0, r.get(6) or 0])
    return out


def _build_rewards():
    m = Master(open(os.path.join(MASTER_DIR, "reward.bin"), "rb").read())
    out = {str(r.get(0)): {"reason": r.get(2) or 0, "items": []} for r in m.rows(1)}
    for r in m.rows(0):
        out.setdefault(str(r.get(1)), {"reason": 0, "items": []})["items"].append(
            {"rewardType": r.get(2) or 0, "itemId": r.get(3) or 0, "level": r.get(4) or 0, "amount": r.get(5) or 0})
    return out


def _build_mission_rewards():
    m = Master(open(os.path.join(MASTER_DIR, "mission.bin"), "rb").read())
    return {str(r.get(0)): r.get(14) or 0 for r in m.rows(0)}


def _build_gift_missions():
    """mission.login_preregistration_*: gifts handed out by the operator (pre-registration, social media
    campaigns), completed on the server's side."""
    m = Master(open(os.path.join(MASTER_DIR, "mission.bin"), "rb").read())
    return sorted(r[0] for r in m.rows(0) if str(r.get(1) or "").startswith("mission.login_preregistration_"))


JSON_DIR = os.path.join(config.DATA_DIR, "extracted", "master_json")  # tools/dump_masters.py output


def _json_rows(table: str, sub: int):
    """Rows from the pre-decoded JSON dump (keys back to ints); falls back to decoding the .bin."""
    path = os.path.join(JSON_DIR, f"{table}.{sub}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [{int(k): v for k, v in r.items()} for r in json.load(f)]
    return list(Master(open(os.path.join(MASTER_DIR, f"{table}.bin"), "rb").read()).rows(sub))


def _build_lottery():
    prices = {str(r[0]): {"lottery": r.get(1), "type": r.get(2) or 1, "currency": r.get(3) or 0,
                          "ticket": r.get(4) or 0, "cost": r.get(5) or 0, "limit": r.get(7) or 0}
              for r in _json_rows("lottery", 3)}
    steps = {}
    for r in _json_rows("lottery", 4):
        steps.setdefault(str(r.get(1)), {})[str(r.get(2) or 0)] = {
            "bonus": r.get(4) or 1, "cost": r.get(5) or 0, "alt": r.get(6) or 0, "next": r.get(7) or 0}
    rates, items = {}, {}
    for r in _json_rows("lottery", 2):
        rates.setdefault(str(r.get(1)), []).append([r.get(2) or 0, r.get(3) or 0])
    for r in _json_rows("lottery", 6):
        items.setdefault(str(r.get(1)), []).append({"id": r[0], "rarity": r.get(2) or 0, "weight": r.get(3) or 0,
                                                   "rewardType": r.get(4) or 0, "target": r.get(5) or 0,
                                                   "amount": r.get(6) or 1})
    # scout points: lottery.bin sub 1 field 20 = point item a banner gives (sub 8: 0 item, 5 exchange lottery,
    # 6 points per draw); point-exchange lotteries cost sub 6 field 19 points per item
    per_draw = {r[0]: r.get(6) or 1 for r in _json_rows("lottery", 8)}
    exchange_lottery = {str(r.get(5)): r[0] for r in _json_rows("lottery", 8) if r.get(5)}
    points = {str(r[0]): [r[20], per_draw.get(r[20], 1)] for r in _json_rows("lottery", 1) if r.get(20)}
    item_index = {}
    for r in _json_rows("lottery", 6):
        item_index[str(r[0])] = {"lottery": r.get(1), "rewardType": r.get(4) or 0, "target": r.get(5) or 0,
                                 "amount": r.get(6) or 1, "pointCost": r.get(19) or 0}
    return {"prices": prices, "rates": rates, "items": items, "steps": steps, "points": points,
            "exchangeLottery": exchange_lottery, "itemIndex": item_index}


def _build_pc_limit_break_items():
    return {str(r[6]): r[0] for r in _json_rows("item", 16) if r.get(6)}


def _build_pc_dup_items():
    """pcId -> [crystal itemId, amount] given for a duplicate character. lottery.bin sub 7 (field 7 = 5): per
    character rows (field 6 = pcId), else by rarity (1) and element mask (2): 1 crystal for 1-2*, 2 for 3*,
    100 for 4*. pc.bin sub 0: field 7 = rarity, field 43 = element mask. A character with its own crystal
    (item.bin sub 16) gets that crystal in the rarity's amount."""
    generic, own_rows = {}, {}
    for r in _json_rows("lottery", 7):
        if r.get(7) != 5:
            continue
        if r.get(6):
            own_rows[str(r[6])] = [r.get(3), r.get(4) or 1]
        else:
            generic[f"{r.get(1)}:{r.get(2)}"] = [r.get(3), r.get(4) or 1]
    own_items = _build_pc_limit_break_items()
    out = {}
    for p in _json_rows("pc", 0):
        pc_id = str(p[0])
        item, amount = generic.get(f"{p.get(7)}:{p.get(43)}") or [182000010, 1]
        out[pc_id] = own_rows.get(pc_id) or [own_items.get(pc_id) or item, amount]
    return out


def _build_pc_dup_extras():
    """pcId -> [itemId, amount] also given for a duplicate: lottery.bin sub 7 rows with field 7 = 1 (item), by
    rarity (field 1): Custom Material R2/R3 x10, R4 x25 (custom.bin sub 5, 411000002-4). The lottery result shows
    it as extraReplaceItem*."""
    by_rarity = {r.get(1): [r.get(3), r.get(4) or 1] for r in _json_rows("lottery", 7) if r.get(7) == 1 and r.get(1)}
    return {str(p[0]): by_rarity[p.get(7)] for p in _json_rows("pc", 0) if p.get(7) in by_rarity}


def _build_treasures():
    m = Master(open(os.path.join(MASTER_DIR, "treasure.bin"), "rb").read())
    types = {r.get(1): r[0] for r in m.rows(0)}
    table = {}
    for r in m.rows(1):
        table.setdefault(f"{r.get(1)}:{r.get(3)}:{r.get(4) or 1}", []).append([r[0], r.get(6) or 0, r.get(5) or 0])
    chests = {}
    for r in Master(open(os.path.join(MASTER_DIR, "map.bin"), "rb").read()).rows(6):
        if r.get(10) in types:
            chests.setdefault(str(r.get(2)), []).append([r.get(6) or 0, types[r[10]]])
    return {"table": table, "chests": chests}


def _build_pc_stats():
    return {str(r[0]): [r.get(15) or 1, r.get(16) or 1] for r in _json_rows("pc", 0)}


def _build_pc_curves():
    """pc.bin sub 0: hp/mp at levels 1 (15/16), 50 (25/26), 80 (33/34), 100 (63/64) -- linear in between
    (checked: Kirito Lv13 = 1485 hp, the archived account's value)."""
    out = {}
    for r in _json_rows("pc", 0):
        pts = [(lv, r.get(h), r.get(m)) for lv, h, m in ((1, 15, 16), (50, 25, 26), (80, 33, 34), (100, 63, 64))
               if r.get(h)]
        out[str(r[0])] = pts
    return out


def _build_pc_level_exp():
    return sorted([r.get(2) or 1, r.get(3) or 0] for r in _json_rows("pc", 1))


def _build_exchange():
    """Item exchange shop: outputs (item.bin sub 9: 1 rewardGroupId, 2 max count (absent = unlimited), 3 input group,
    4 display order), inputs (sub 8: 1 group, 2 item label, 3 amount), and item label -> itemId for every item sheet.
    Field 4 was read as the limit: the cube shop's Item Boost x2 (order 94) stopped selling after 94 (tester report,
    2026-10-18)."""
    # field 11 = 1: a limit that starts again (the cube shop's Battle Skip / Event Rewards x2 / boosts, flowers...)
    out = {str(r[0]): {"reward": r.get(1) or 0, "input": r.get(3) or 0, "max": r.get(2) or 0,
                       "resets": r.get(11) == 1} for r in _json_rows("item", 9)}
    inputs = {}
    for r in _json_rows("item", 8):
        inputs.setdefault(str(r.get(1)), []).append([r.get(2) or "", r.get(3) or 0])
    labels = {}
    for sub in range(0, 40):
        try:
            rows = _json_rows("item", sub)
        except (IndexError, FileNotFoundError, OSError):
            break
        for r in rows:
            if isinstance(r.get(0), int) and isinstance(r.get(1), str) and "." in r[1]:
                labels.setdefault(r[1], r[0])
    return {"out": out, "in": inputs, "labels": labels}


def _build_login_bonus():
    """login_bonus.bin sub 1 (days): 1 loginBonusId, 2 day, 3 rewardGroupId -> {bonusId: {day: rewardGroupId}}."""
    out = {}
    for r in _json_rows("login_bonus", 1):
        out.setdefault(str(r.get(1)), {})[str(r.get(2) or 1)] = r.get(3) or 0
    return out


def _build_login_bonus_once():
    """Campaign login bonuses that run once (login_bonus.bin sub 0 field 4 = 1)."""
    return sorted(r[0] for r in _json_rows("login_bonus", 0) if r.get(4) == 1)


def _build_season():
    """season.bin: sub 1 rewards (0 id, 1 seasonId, 2 track (absent free, 2 premium), 3 tier, 4 points, 5 rewardGroupId,
    6 times the extra tier 101 can be received, kept as "cost"), sub 2 point boosts (4 cost), sub 3 premium passes
    (4 seasonId, 5 cost, 7 bundle reward group)."""
    rewards = {str(r[0]): {"season": r.get(1) or 0, "reward": r.get(5) or 0, "cost": r.get(6) or 0,
                           "premium": (r.get(2) or 1) == 2, "points": r.get(4) or 0} for r in _json_rows("season", 1)}
    boosts = {str(r[0]): r.get(4) or 0 for r in _json_rows("season", 2)}
    # field 7: the "Bundle Set" passes' reward group (season points + themes), given with the pass
    passes = {str(r[0]): {"season": r.get(4) or 0, "cost": r.get(5) or 0, "bundle": r.get(7) or 0}
              for r in _json_rows("season", 3)}
    # sub 0: season id -> its season point item (field 2, item.bin seasonPointItem 405xxxxxx), release label (13)
    point_items = {str(r[0]): r.get(2) or 0 for r in _json_rows("season", 0)}
    # main seasons only: the rows with an eventId (field 1) are event point seasons (eventSeasonPoint)
    labels = {str(r[0]): r.get(13) or "" for r in _json_rows("season", 0) if not r.get(1)}
    # sub 2 point boost: field 6 days, field 7 ratio x10000 (20000 = season and event points x2)
    boost_info = {str(r[0]): {"days": r.get(6) or 0, "ratio": r.get(7) or 10000} for r in _json_rows("season", 2)}
    return {"rewards": rewards, "boosts": boosts, "passes": passes, "pointItems": point_items, "labels": labels,
            "boostInfo": boost_info}


def _build_boost_items():
    """quest boost items (item.bin sheet questBoostItem, ids 249xxxxxx): field 5 = gold price."""
    for sub in range(0, 40):
        try:
            rows = _json_rows("item", sub)
        except (IndexError, FileNotFoundError, OSError):
            break
        if rows and str(rows[0].get(0, "")).startswith("249"):
            return {str(r[0]): r.get(5) or 0 for r in rows}
    return {}


def _build_stage_levels():
    """"stageId:questLevel" -> stageLevelId (quest.11 field 0), the key of the client's UserStage rows."""
    return {f"{r.get(2)}:{r.get(4) or 1}": r[0] for r in _json_rows("quest", 11) if r.get(2)}


def _build_stage_encounts():
    m = Master(open(os.path.join(MASTER_DIR, "quest.bin"), "rb").read())
    out = {}
    for r in m.rows(11):
        group, label = r.get(9, -1), r.get(10) or ""
        if (group is not None and group > 0) or label or r.get(11) or r.get(12):
            out[f"{r.get(2)}:{r.get(4) or 1}"] = {"group": group if group and group > 0 else 0, "count": r.get(6) or 0,
                                  "label": label, "level": [r.get(11) or 1, r.get(12) or r.get(11) or 1],
                                                  "move": [r.get(7) or 0, r.get(8) or r.get(7) or 0],
                                                  "tier": r.get(11) or r.get(12) or 0}
    return out


def _build_enemy_parties():
    m = Master(open(os.path.join(MASTER_DIR, "enemy.bin"), "rb").read())
    return {str(r.get(0)): {"group": r.get(1) or 0, "weight": r.get(3) or 1, "label": r.get(5) or "",
                            "exp": r.get(37) or 0, "gold": r.get(38) or 0,
                            "bases": [r[k] for k in range(41, 46) if r.get(k)],
                            # level of each member (fields 46-50, next to the enemies 41-45): picks the enemy.bin
                            # sub 4 row the client fights with (ch1_1_1_sn_boss 2/2, its hard variant 22/22)
                            "levels": [r.get(k + 5) or 1 for k in range(41, 46) if r.get(k)]} for r in m.rows(2)}


def _build_enemy_hp():
    m = Master(open(os.path.join(MASTER_DIR, "enemy.bin"), "rb").read())
    out = {}
    for r in m.rows(4):
        out.setdefault(str(r.get(2)), {})[str(r.get(6) or 1)] = r.get(9) or 1
    return out


QUEST_STAGES = _cached("quest_stages", _build_quest_stages)  # questId -> [stageId in order]
STAGE_AREA = _cached("stage_areas", _build_stage_areas)  # stageId -> areaId
STAGE_ENCOUNTS = _cached("stage_encounts4", _build_stage_encounts)  # "stageId:questLevel" -> encounter setup
ENEMY_PARTIES = _cached("enemy_parties2", _build_enemy_parties)  # enemyPartyId -> party
ENEMY_HP = _cached("enemy_hp", _build_enemy_hp)  # enemyBaseId -> {level: hp}
PARTIES_BY_GROUP, PARTY_BY_LABEL = {}, {}
for _pid, _p in ENEMY_PARTIES.items():
    PARTIES_BY_GROUP.setdefault(_p["group"], []).append(int(_pid))
    if _p["label"]:
        PARTY_BY_LABEL[_p["label"]] = int(_pid)


def enemy_hp(base_id: int, level: int) -> int:
    """Max HP of an enemy: enemy.bin sub 4 has rows at sample levels (every 10 levels, or a normal and a hard
    variant such as 2 and 22); exact at those levels, linear in between. The level must be the party's own
    (see ENEMY_PARTIES "levels"), not the stage's: the stage level gave hp the client never used."""
    table = sorted((int(k), v) for k, v in ENEMY_HP.get(str(base_id), {}).items())
    if not table:
        return 1
    lo = max((t for t in table if t[0] <= level), default=table[0])
    hi = min((t for t in table if t[0] >= level), default=table[-1])
    if hi[0] == lo[0]:
        return lo[1]
    return int(lo[1] + (hi[1] - lo[1]) * (level - lo[0]) / (hi[0] - lo[0]))
QUEST_LEVELS = _cached("quest_levels5", _build_quest_levels)  # "questId:level" -> {id, exp, gold, rewards, drops, missions}
BONUS_MISSIONS = _cached("bonus_missions", _build_bonus_missions)  # missionId -> {type, param, reward}
PC_CHARACTER = _cached("pc_characters", _build_pc_characters)  # pcId -> character id
PC_TRAITS = _cached("pc_traits", _build_pc_traits)  # pcId -> [gender, weapon type]
RAID = _cached("raid", _build_raid)  # see _build_raid
STAGE_LEVELS = _cached("stage_levels", _build_stage_levels)  # "stageId:level" -> stageLevelId
QUEST_DROPS = _cached("quest_drops", _build_quest_drops)  # drop table -> {slot: [[weight, rewardGroupId]]}
EVENT_QUEST_DROPS = _cached("event_quest_drops", _build_event_quest_drops)  # questId -> event drop table
PVP_RANKS = _cached("pvp_ranks", _build_pvp_ranks)  # pvpId -> [[rank, minimum points]]
EVENT_BOSS_QUESTS = set(_cached("event_boss_quests", _build_event_boss_quests))  # {"eventId:questId"}
EVENT_BOSS_TICKETS = _cached("event_boss_tickets", _build_event_boss_tickets)  # "eventId:questId" -> [label, cost]
PC_IMAGINATIONS = _cached("pc_imaginations", _build_pc_imaginations)  # pcId -> its limit-break-4 Imaginarium
EXCHANGEABLE_IMAGINATIONS = set(_cached("imagination_exchangeable", _build_exchangeable_imaginations))
EVENT_SEASONS = _cached("event_seasons", _build_event_seasons)  # eventId -> [event season id, point item]
EVENT_PLAY_LIMITS = _cached("event_play_limits", _build_event_play_limits)  # eventId -> runs per game day
BANNERS = _cached("banners", _build_banners)  # release label -> [[lotteryId, jp name, en headline]] (admin panel)


def _build_banner_images():
    """lotteryId -> [[path, base64 AES key], ...] of its banner picture (lottery.bin sub 1: field 11 folder, field 6
    picture name), English first then Japanese; the keys are master asset.0's, which the VPS does not have."""
    with open(os.path.join(JSON_DIR, "asset.0.json"), encoding="utf-8") as f:
        keys = {r["0"]: r["1"] for r in json.load(f)}
    out = {}
    for r in _json_rows("lottery", 1):
        folder, name = r.get(11), r.get(6)
        if not folder or not name:
            continue
        paths = [f"localization/en/files/lottery/{folder}/{name}.pngenc", f"files/lottery/{folder}/{name}.pngenc"]
        found = [[p, keys[p]] for p in paths if p in keys]
        if found:
            out[str(r.get(0))] = found
    return out


BANNER_IMAGES = _cached("banner_images", _build_banner_images)  # lotteryId -> [[path, key]] (admin panel)
TOWER_EVENT_QUESTS = _cached("tower_event_quests", _build_tower_event_quests)  # "eventId:questId" -> towerEventQuestId
QUEST_GUESTS = _cached("quest_guests", _build_quest_guests)  # questId -> [[id, pcId, level, name]]
PVP_RANK_ROWS = _cached("pvp_rank_rows2", _build_pvp_rank_rows)  # "pvpId:rank" -> result bonuses and rewards
TOWER_FLOOR_REWARDS = _cached("tower_floor_rewards", _build_tower_floor_rewards)  # questId -> first-clear group
TOWER_FLOORS = _cached("tower_floors", _build_tower_floors)  # eventId -> [[towerEventQuestId, towerLevel, top floor]]
MISSIONS = _cached("missions2", _build_missions)  # missionId -> [type, category, target, ..., server-tracked]
RELEASES = _cached("releases", _build_releases)  # release label -> [start, end] (JST)
EVENT_KINDS = _cached("event_kinds", _build_event_kinds)  # eventId -> event kind (2 = growth)
EVENT_TYPES = _cached("event_types", _build_event_types)  # eventId -> event type (3 = tower, 9 = rush)
REWARDS = _cached("rewards", _build_rewards)  # rewardGroupId -> {reason, items[]}
MISSION_REWARD = _cached("mission_rewards", _build_mission_rewards)  # missionId -> rewardGroupId
GIFT_MISSIONS = _cached("gift_missions", _build_gift_missions)  # [missionId] completed by the server
LOTTERY = _cached("lottery3", _build_lottery)  # {"prices": {priceId}, "rates": {lotteryId}, "items": {lotteryId}}
PC_LIMIT_BREAK_ITEM = _cached("pc_limit_break_items", _build_pc_limit_break_items)  # pcId -> crystal itemId
PC_DUP_EXTRA = _cached("pc_dup_extras", _build_pc_dup_extras)  # pcId -> [custom material, amount] on a duplicate
PC_DUP_ITEM = _cached("pc_dup_items", _build_pc_dup_items)  # pcId -> [crystal itemId, amount] for a duplicate
TREASURES = _cached("treasures", _build_treasures)  # {"table": {"tier:type:level": [[id, weight, reward]]},
#                                                      "chests": {areaId: [[position, type]]}}
ENEMY_DROPS = _cached("enemy_drops2", _build_enemy_drops)  # "tier:questLevel" -> {kind: [[chance, group] per element]}
PC_STATS = _cached("pc_stats", _build_pc_stats)  # pcId -> [base hp, base mp]
EXCHANGE = _cached("exchange3", _build_exchange)  # {"out": {id}, "in": {group}, "labels": {label: itemId}}
LOGIN_BONUS = _cached("login_bonus", _build_login_bonus)  # loginBonusId -> {day: rewardGroupId}
LOGIN_BONUS_ONCE = set(_cached("login_bonus_once", _build_login_bonus_once))  # campaign bonuses that run once
PC_CURVES = _cached("pc_curves", _build_pc_curves)  # pcId -> [[level, hp, mp], ...]
PC_LEVEL_EXP = _cached("pc_level_exp", _build_pc_level_exp)  # [[level, total exp], ...]


def pc_level(total_exp: int) -> int:
    return max([lv for lv, exp in PC_LEVEL_EXP if exp <= total_exp], default=1)


def pc_max_hp_mp(pc_id: int, total_exp: int) -> tuple[int, int]:
    """Full hp/mp of a character at the level its total exp gives."""
    pts = PC_CURVES.get(str(pc_id)) or [[1, 1000, 140]]
    level = pc_level(total_exp)
    lo = max([p for p in pts if p[0] <= level], default=pts[0], key=lambda p: p[0])
    hi = min([p for p in pts if p[0] >= level], default=pts[-1], key=lambda p: p[0])
    if hi[0] == lo[0]:
        return lo[1], lo[2]
    t = (level - lo[0]) / (hi[0] - lo[0])
    # truncated like the client (Kirito level 2: 1028.55 -> 1028 in game; the archived account: 1485 not 1486)
    return int(lo[1] + (hi[1] - lo[1]) * t), int(lo[2] + (hi[2] - lo[2]) * t)
SEASON = _cached("season5", _build_season)  # {"rewards": {id}, "boosts": {id: cost}, "passes": {id}}
BOOST_ITEMS = _cached("boost_items", _build_boost_items)  # itemId -> gold price


def _build_player_levels():
    """player.bin sub 0: field 1 = player level ("Rank"), field 2 = total exp needed."""
    return sorted([r.get(1) or 1, r.get(2) or 0] for r in _json_rows("player", 0))


def _build_player_max_ap():
    """player.bin sub 0: rank (field 1) -> max AP (field 6: 35 at rank 1 ... 150). The client reads it as vtable
    slot 0x10 of the player row (FUN_0122e900, + a bonus that is 0 without a license)."""
    return {str(r.get(1) or 1): r.get(6) or 0 for r in _json_rows("player", 0)}


def _build_guild_missions():
    """guild.bin: sub 1 guildMission (field 2 mission.bin id, 3 group, 4 guild points), sub 3
    guildMissionReceiveCount (field 3 group, 2 missions drawn from it per day), sub 2 weekly point rewards
    (field 2 points, 3 reward group)."""
    missions = {str(r[0]): {"missionId": r.get(2), "group": r.get(3) or 1, "point": r.get(4) or 0}
                for r in _json_rows("guild", 1)}
    counts = {str(r.get(3)): r.get(2) or 1 for r in _json_rows("guild", 3)}
    rewards = sorted([r.get(2) or 0, r.get(3) or 0] for r in _json_rows("guild", 2))
    return {"missions": missions, "counts": counts, "pointRewards": rewards}


def _build_names():
    """Display names for the admin panel: {"items", "equipment", "pcs", "reasons": {id: name}}. English from
    localization.bin ("<label>" -> name) when it exists, else the Japanese name of the master row."""
    def rows(name):
        path = os.path.join(JSON_DIR, name)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    import glob  # noqa: PLC0415
    loc = {r.get("1"): r.get("4") for r in rows("localization.0.json") if r.get("3") == "name" and r.get("4")}
    out = {"items": {}, "equipment": {}, "pcs": {}, "reasons": {}}
    for path in sorted(glob.glob(os.path.join(JSON_DIR, "item.*.json"))) + [os.path.join(JSON_DIR, "lottery.0.json")]:
        for r in rows(os.path.basename(path)):
            if not isinstance(r.get("0"), int):
                continue
            label = next((r[k] for k in ("1", "6", "7") if isinstance(r.get(k), str) and "." in r[k]), "")
            jp = r.get("2") if isinstance(r.get("2"), str) else ""
            name = loc.get(label) or jp or label
            if name and str(r["0"]) not in out["items"]:
                out["items"][str(r["0"])] = name
    for r in rows("equipment.6.json"):
        label = (r.get("6") or "").replace("equipment.", "")
        out["equipment"][str(r["0"])] = f"{r.get('7') or ''} ({label})".strip()
    for r in rows("pc.0.json"):
        label = (r.get("6") or "").replace("character.", "").replace("_", " ")
        out["pcs"][str(r["0"])] = f"{label} — {r.get('1') or ''}"
    for r in rows("reward.2.json"):
        out["reasons"][str(r["0"])] = r.get("1") or ""
    return out


PLAYER_LEVELS = _cached("player_levels", _build_player_levels)  # [[rank, total exp], ...]
PLAYER_MAX_AP = _cached("player_max_ap", _build_player_max_ap)  # "rank" -> max AP
GUILD = _cached("guild", _build_guild_missions)
NAMES = _cached("names", _build_names)  # admin panel: {"items"|"equipment"|"pcs"|"reasons": {id: name}}


def player_rank(total_exp: int) -> int:
    return max([lv for lv, exp in PLAYER_LEVELS if exp <= total_exp], default=1)
