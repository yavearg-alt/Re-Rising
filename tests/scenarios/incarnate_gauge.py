"""The party's Incarnation (INC) gauge (UserAnotherForce.gauge, 0..20000 = 0..200 %) is pushed by the client after
each battle and the next battle starts from it. Quests and Ordinal battles must start and end with it empty
(Discord 2026-10-28 "stats and charges not reset between battles", 2026-11-02 "INC is kept between fight")."""
PVP = 603100000038
QUEST = {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8014044003, "supporterPcId": 0,
         "supporterPcSlotId": 0, "supporterUserId": 0}


def _charge(c, gauge=20000):
    """What the game pushes after a battle won with a full gauge."""
    c.push({"anotherForce": [dict(c.row("anotherForce"), isUnlocked=True, gauge=gauge)]})


def test_inc_gauge_empty_at_quest_start_and_end(h):
    c = h.new_player()
    _charge(c)
    h.expect(c.server_table("anotherForce")[0]["gauge"] == 20000, "le push doit garder la jauge du combat")
    c.call("quest/start", QUEST)
    h.expect(c.row("anotherForce").get("gauge") == 0, f"jauge au lancement : {c.row('anotherForce')}")
    h.expect(c.row("anotherForce").get("isUnlocked") is True, "isUnlocked doit rester tel quel")
    _charge(c, 13500)  # within the quest, the client keeps its gauge between waves: pushes are stored
    h.expect(c.server_table("anotherForce")[0]["gauge"] == 13500, "jauge en cours de quête écrasée")
    c.call("quest/clear")
    h.expect(c.row("anotherForce").get("gauge") == 0, f"jauge après quest/clear : {c.row('anotherForce')}")
    _charge(c)
    c.call("quest/start", QUEST)
    _charge(c)
    c.call("quest/retire")
    h.expect(c.server_table("anotherForce")[0]["gauge"] == 0, "jauge après quest/retire")


def test_inc_gauge_and_hp_rested_around_ordinal(h):
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP, "reloadFlag": False})
    _charge(c)
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 1, "npcPartyIndex": 1, "pvpId": PVP})
    h.expect(c.row("anotherForce").get("gauge") == 0, f"jauge au début du combat Ordinal : {c.row('anotherForce')}")
    # the battle: INC charged, the first character hurt
    _charge(c, 17270)
    pc = dict(c.t("pc")[0], hp=1, mp=0)
    c.push({"pc": [pc]})
    c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": True})
    h.expect(c.row("anotherForce").get("gauge") == 0, f"jauge après le combat Ordinal : {c.row('anotherForce')}")
    after = next(p for p in c.t("pc") if p["pcId"] == pc["pcId"])
    h.expect(after["hp"] > 1 and after["mp"] > 0, f"pv/pm non restaurés après Ordinal : {after['hp']}/{after['mp']}")
