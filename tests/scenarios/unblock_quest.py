"""Server-side clear of a quest the iOS client cannot play (M-1-2 crash, 2026-10-20): the same quest/start +
quest/clear the game would send, then the idle quest status the client pushes after the result screen."""
import api
from harness import userdata

M12 = 8012001002


def server_clear(uid: int, quest_id: int, level: int = 1):
    info = (userdata.table(uid, "info") or [{}])[0]
    api.ep_quest_start(uid, {"questId": quest_id, "level": level, "eventId": 0, "partyId": info.get("activePartyId", 1),
                             "boostItemIds": [], "supporterUserId": 0, "supporterPcId": 0, "supporterPcSlotId": 0})
    api.ep_quest_clear(uid, {})
    status = dict((userdata.table(uid, "questStatus") or [{}])[0], type=4)
    userdata.set_tables(uid, {"questStatus": [status]})


def test_server_clear_unlocks_next(h):
    c = h.new_player()
    server_clear(c.uid, 8012001001)
    server_clear(c.uid, M12)
    c.pull()
    prog = {r["questLevelId"]: r for r in c.t("questProgress")}
    h.expect(prog.get(8120010021, {}).get("clearCount") == 1, f"M-1-2 non marquée terminée : {prog.get(8120010021)}")
    h.expect(c.item(221001011) >= 6, "récompense de première victoire absente")
    h.expect(c.row("questStatus").get("type") == 4, f"questStatus doit être inactif : {c.row('questStatus')}")
    # the next quest starts normally
    c.call("quest/start", {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8012001003,
                           "supporterPcId": 0, "supporterPcSlotId": 0, "supporterUserId": 0})
    h.expect(c.row("questStatus").get("questId") == 8012001003, "M-1-3 ne démarre pas")
