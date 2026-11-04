"""AP is spent at quest start, with the client's own rule (FUN_0122eb1c): +1 AP every 180 s up to the rank's max
AP (player.bin field 6), no growth above it (recovery items), never below 0."""
from harness import config, master, userdata
import api

QUEST = {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8014044003, "supporterPcId": 0,
         "supporterPcSlotId": 0, "supporterUserId": 0}


def _set_ap(c, base, ago):
    info = dict(userdata.table(c.uid, "info")[0], baseAp=base, baseApUpdatedAt=int(config.now()) - ago)
    userdata.set_tables(c.uid, {"info": [info]})
    c.pull()


def test_ap_spent_at_quest_start(h):
    c = h.new_player()
    rank = master.player_rank(c.row("info").get("totalExp", 0))
    top = master.PLAYER_MAX_AP[str(rank)]
    cost = master.QUEST_LEVELS["8014044003:1"]["ap"]
    h.expect(top >= 35 and cost > 0, f"AP max {top}, coût {cost}")
    # full AP for a long time: the stored value is the max, minus the cost
    _set_ap(c, 0, 10 ** 6)
    c.call("quest/start", QUEST)
    info = c.row("info")
    h.expect(info["baseAp"] == top - cost, f"AP après lancement depuis le max : {info['baseAp']} (max {top}, coût {cost})")
    h.expect(abs(info["baseApUpdatedAt"] - int(config.now())) <= 2, "baseApUpdatedAt doit repartir de maintenant")
    c.call("quest/clear")
    # recovering: 10 AP stored 30 min ago = 20 AP now; the partial minute is kept
    _set_ap(c, 10, 30 * 60 + 100)
    c.call("quest/start", QUEST)
    info = c.row("info")
    h.expect(info["baseAp"] == max(0, 20 - cost), f"AP en récupération : {info['baseAp']}")
    h.expect(int(config.now()) - info["baseApUpdatedAt"] in range(99, 103), "la fraction de minute doit être gardée")
    c.call("quest/clear")
    # above the max (recovery items): no growth, straight subtraction
    _set_ap(c, 500, 10 ** 6)
    c.call("quest/start", QUEST)
    h.expect(c.row("info")["baseAp"] == 500 - cost, f"AP au-dessus du max : {c.row('info')['baseAp']}")
    c.call("quest/clear")
    # not enough: 0, never negative, quest still starts
    _set_ap(c, 0, 0)
    c.call("quest/start", QUEST)
    h.expect(c.row("info")["baseAp"] == 0 and c.row("questStatus").get("type") == 1, "AP négative ou quête refusée")


def test_current_ap_formula(h):
    info = {"baseAp": 10, "baseApUpdatedAt": 1000, "totalExp": 0}
    top = master.PLAYER_MAX_AP["1"]
    h.expect(api.current_ap(info, 1000 + 179) == (10, 1000), "moins de 180 s : pas d'AP")
    h.expect(api.current_ap(info, 1000 + 360) == (12, 1360), "360 s : +2 AP")
    h.expect(api.current_ap(info, 1000 + 10 ** 6)[0] == top, "plafonnée au max du rang")
    h.expect(api.current_ap(info, 500)[0] == 10, "horloge qui recule : jamais moins")


def test_ap_reward_adds_to_current_ap(h):
    """A "+50 AP" mission reward (login time bonus, rewardType 6) goes on top of the AP recovered since
    baseApUpdatedAt: it was added to the stored value, so 87 AP became 104 (Discord 2026-10-28)."""
    c = h.new_player()
    mid = 15000050001
    rows = [dict(r, state=3) if r["missionId"] == mid else r for r in userdata.table(c.uid, "mission")]
    h.expect(any(r["missionId"] == mid for r in rows), "mission +50 AP absente du compte neuf")
    userdata.set_tables(c.uid, {"mission": rows})
    _set_ap(c, 10, 30 * 60 + 100)  # 10 stored 30 min ago = 20 AP now
    c.call("mission/receive", {"missionIds": [mid]})
    info = c.row("info")
    h.expect(info["baseAp"] == 70, f"AP après +50 : {info['baseAp']} (70 attendus)")
    h.expect(int(config.now()) - info["baseApUpdatedAt"] in range(99, 103), "la fraction de minute doit être gardée")
