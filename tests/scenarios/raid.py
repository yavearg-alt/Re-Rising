"""War of Underworld 18 (event 601101000200): explore -> boss appears -> fight -> defeat -> points, rewards, guild ranking."""
import raid
from harness import master

raid.SPAWN_BOSSES = True  # off in production until the boss rows are checked in the real client

EVENT = 601101000200
EXPLORE = {"boostItemIds": [], "eventId": EVENT, "level": 1, "partyId": 1, "questId": 8015045005, "supporterPcId": 0,
           "supporterPcSlotId": 0, "supporterUserId": 0}  # lottery chance 10000: the boss always appears


def _find_boss(h, c):
    c.call("quest/start", EXPLORE)
    c.call("quest/clear")
    bosses = [s for s in c.t("raidEventBossSharedStatus") if s.get("eventId") == EVENT]
    h.expect(bosses, "explorer la quête 8015045005 doit faire apparaître un boss")
    return bosses[-1]


def test_raid_boss_flow(h):
    c = h.new_player()
    c.call("guild/form", {"name": "Raiders", "comment": "", "atmosphereType": 0, "iconId": 390000010,
                          "isRequiredApproval": False})
    c.call("raid/info", {"eventId": EVENT})
    ev = c.row("raidEvent")
    h.expect(ev.get("eventId") == EVENT and ev.get("wp") == 8000, f"raidEvent : {ev}")
    h.expect(len([a for a in c.t("raidEventArea") if a.get("eventId") == EVENT]) == 4, "4 zones attendues")

    boss = _find_boss(h, c)
    area = next(a for a in c.t("raidEventArea") if boss["bossId"] in a.get("bossIds", []))
    h.expect(area["raidEventAreaId"] == 604200000045, f"zone du boss : {area['raidEventAreaId']}")
    hp = sum(e["hp"] for e in boss["encountStatus"]["enemyStatus"])
    h.expect(hp > 0, "le boss doit avoir des PV")

    start = dict(EXPLORE, questId=boss["questId"], bossId=boss["bossId"], consumingWp=40)
    c.call("raid/boss/start", start)
    h.expect(c.row("questStatus").get("type") == 1, "le combat de boss démarre une quête")
    st = next(s for s in c.t("stageStatus") if s.get("bossEncountStatus"))
    h.expect(st["bossEncountStatus"][0]["enemyPartyId"] == boss["encountStatus"]["enemyPartyId"],
             "le combat doit opposer le boss découvert")

    # first fight: half the hp
    half = dict(st, bossEncountStatus=[dict(st["bossEncountStatus"][0], enemyDamagePoint=hp // 2)])
    c.push({"stageStatus": [half]})
    c.call("raid/boss/end", {"eventId": EVENT, "bossId": boss["bossId"], "isUserRetire": False})
    left = next(s for s in c.t("raidEventBossSharedStatus") if s["bossId"] == boss["bossId"])
    h.expect(sum(e["hp"] for e in left["encountStatus"]["enemyStatus"]) == hp - hp // 2, "les PV du boss doivent baisser")
    h.expect(c.row("questStatus").get("type") == 4, "après raid/boss/end, la quête est terminée")

    # second fight: the rest -> defeated
    gifts = len(c.t("gift"))
    c.call("raid/boss/start", start)
    st = next(s for s in c.t("stageStatus") if s.get("bossEncountStatus"))
    c.push({"stageStatus": [dict(st, bossEncountStatus=[dict(st["bossEncountStatus"][0], enemyDamagePoint=hp)])]})
    c.call("raid/boss/end", {"eventId": EVENT, "bossId": boss["bossId"], "isUserRetire": False})
    h.expect(not any(s["bossId"] == boss["bossId"] for s in c.t("raidEventBossSharedStatus")), "le boss vaincu disparaît")
    result = next(r for r in c.t("raidEventBossResult") if r["bossId"] == boss["bossId"])
    level = master.RAID["levels"]["5045006"][0]
    h.expect(result["totalDefeatPoint"] >= level["defeat_point"], f"points : {result['totalDefeatPoint']}")
    h.expect(c.row("raidEvent")["defeatPoint"] == result["totalDefeatPoint"], "les points s'ajoutent à raidEvent")
    h.expect(len(c.t("gift")) > gifts, "les récompenses du boss arrivent dans la boîte cadeaux")
    group = next(r for r in c.t("raidEventBossLevelGroup") if r["raidBossLevelGroupId"] == 5045006)
    h.expect(group["raidBossDefeatCount"] == 1, "le groupe de boss monte d'un niveau")

    nxt = _find_boss(h, c)
    h.expect(nxt["raidEventBossLevelId"] == master.RAID["levels"]["5045006"][1]["id"], "le boss suivant est plus fort")

    gid = c.row("info")["guildId"]
    c.call("raid/guild/ranking/list", {"eventId": EVENT, "guildId": gid, "offset": 0, "limit": 20})
    mine = next(r for r in c.t("raidEventGuildRankingScore") if r["guildId"] == gid)
    h.expect(mine["guildDefeatPoint"] == level["guild_point"], f"points de guilde : {mine}")
    c.call("raid/guild/ranking/member/list", {"eventId": EVENT, "guildId": gid})
    me = next(r for r in c.t("raidEventGuildRankingMemberScore") if r["guestId"] == c.uid)
    h.expect(me["rank"] == 1 and me["guildDefeatPoint"] == level["guild_point"], f"classement membre : {me}")


def test_raid_retire_and_withdraw(h):
    c = h.new_player()
    boss = _find_boss(h, c)
    hp = sum(e["hp"] for e in boss["encountStatus"]["enemyStatus"])
    start = dict(EXPLORE, questId=boss["questId"], bossId=boss["bossId"], consumingWp=40)
    c.call("raid/boss/start", start)
    c.call("raid/boss/end", {"eventId": EVENT, "bossId": boss["bossId"], "isUserRetire": True})
    left = next(s for s in c.t("raidEventBossSharedStatus") if s["bossId"] == boss["bossId"])
    h.expect(sum(e["hp"] for e in left["encountStatus"]["enemyStatus"]) == hp, "abandonner ne blesse pas le boss")
    c.call("raid/boss/withdraw", {"eventId": EVENT, "bossId": boss["bossId"]})
    h.expect(not any(s["bossId"] == boss["bossId"] for s in c.t("raidEventBossSharedStatus")), "le boss abandonné disparaît")
