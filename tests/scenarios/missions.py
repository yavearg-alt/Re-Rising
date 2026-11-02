"""Server-tracked missions (server/missions.py): quest and chapter clears, logins, scouts, daily/weekly resets.
They all stayed at 0 before (tester report, Discord 2026-10-17: "after completing a chapter it doesn't allow completion")."""
from harness import master, userdata

QUEST = {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001, "supporterPcId": 0,
         "supporterPcSlotId": 0, "supporterUserId": 0}


def _row(c, mission_id):
    return next((m for m in c.t("mission") if m["missionId"] == mission_id), None)


def test_chapter_clear_mission(h):
    c = h.new_player()
    mid = 15100020001  # "clear Underworld chapter 1 NORMAL" = quest 8013001004 level 1
    h.expect(_row(c, mid) and _row(c, mid)["state"] == 2, f"mission absente ou déjà faite : {_row(c, mid)}")
    c.call("quest/start", dict(QUEST, questId=8013001004))
    c.call("quest/clear")
    h.expect(_row(c, mid)["state"] == 3 and _row(c, mid)["progress"] == 1, f"après la quête : {_row(c, mid)}")
    c.call("mission/receive", {"missionIds": [mid]})
    h.expect(_row(c, mid)["state"] == 4, "la mission doit pouvoir être reçue")


def test_login_days_mission(h):
    c = h.new_player()
    c.call("login")
    row = _row(c, 15100010001)  # "1 login day"
    h.expect(row and row["state"] == 3, f"mission 1 jour de connexion : {row}")


def test_daily_quest_mission_and_reset(h):
    import missions
    c = h.new_player()
    daily = [m for m in c.t("mission") if master.MISSIONS.get(str(m["missionId"]), [0])[0] == 9002
             and missions.is_open(master.MISSIONS[str(m["missionId"])][6])]
    h.expect(daily, "aucune mission quotidienne « finir des quêtes » ouverte dans le compte neuf")
    one = next(m for m in daily if master.MISSIONS[str(m["missionId"])][2] == 1)
    c.call("quest/start", QUEST)
    c.call("quest/clear")
    h.expect(_row(c, one["missionId"])["state"] == 3, f"quotidienne après une quête : {_row(c, one['missionId'])}")
    # next day: daily missions start again
    with userdata._lock:
        data = userdata.load(c.uid)
        data["missions"]["day"] -= 1
        data["missions"]["reset"]["day"] -= 1
        userdata.db.save_user(c.uid, data)
    c.call("login")
    h.expect(_row(c, one["missionId"])["state"] == 2 and _row(c, one["missionId"])["progress"] == 0,
             f"le lendemain la quotidienne repart à zéro : {_row(c, one['missionId'])}")


def test_character_scout_missions(h):
    c = h.new_player()
    userdata.add_gems(c.uid, 5000)
    price_id, price = next((k, p) for k, p in master.LOTTERY["prices"].items()
                           if p["type"] == 2 and p["cost"] and not p["limit"] and not p["ticket"])
    c.call("lottery/draw", {"gemAmount": price["cost"], "lotteryPriceId": int(price_id), "step": 1,
                            "ticketAmount": 0, "ticketId": 0})
    row = _row(c, 15100230001)  # "scout characters 10 times"
    h.expect(row and row["progress"] == 10 and row["state"] == 3, f"mission 10 tirages : {row}")


def test_affinity_titles_keep_restored_progress(h):
    """Accounts restored from the archive keep the game's own progress on the "clear N quests with X" titles: the
    server counters (from 2026-10-17) must not pull it back, and the next clear counts on top (tester report, 2026-10-22)."""
    c = h.new_player()
    party = next(p for p in c.t("party") if p["id"] == 1)
    pc_id = next(sl["pcId"] for sl in party["slots"] if sl.get("pcId"))
    character = str(master.PC_CHARACTER.get(str(pc_id)))
    ids = sorted(int(k) for k, m in master.MISSIONS.items() if m[0] == 1301 and m[1] == 1 and m[3] == character)
    h.expect(len(ids) >= 2, f"missions d'affinité du personnage {character} : {ids}")
    first, second = ids[0], ids[1]
    target = master.MISSIONS[str(second)][2]
    rows = [dict(m) for m in userdata.table(c.uid, "mission")]
    have = {m["missionId"] for m in rows}
    rows += [dict(rows[0], missionId=mid, state=2, progress=0, completedAt=0) for mid in (first, second)
             if mid not in have]
    for m in rows:
        if m["missionId"] == first:
            m.update(state=4, progress=master.MISSIONS[str(first)][2])
        elif m["missionId"] == second:
            m.update(state=2, progress=target - 1)
    userdata.set_tables(c.uid, {"mission": rows})
    c.call("login")  # the first mission pass after the update reads the restored progress
    c.call("quest/start", QUEST)
    c.call("quest/clear")
    row = next(m for m in userdata.table(c.uid, "mission") if m["missionId"] == second)
    h.expect(row["progress"] == target and row["state"] == 3, f"titre 2 après une quête de plus : {row}")
