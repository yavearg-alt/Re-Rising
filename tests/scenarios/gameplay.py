"""Quests, missions, gift box, summons, exchanges, shop-like purchases. Bodies copied from real client requests
(server/logs/api_requests.jsonl)."""
from harness import master, userdata

QUEST = {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001, "supporterPcId": 0,
         "supporterPcSlotId": 0, "supporterUserId": 0}
GOLD = 205000001


def test_quest_start_clear(h):
    c = h.new_player()
    c.call("quest/supporter", {"level": 1, "questId": 8011012001})
    h.expect(c.t("guest"), "quest/supporter doit renvoyer des soutiens (table guest)")
    c.call("quest/start", QUEST)
    st = c.row("questStatus")
    h.expect(st.get("questId") == QUEST["questId"] and st.get("type") == 1, f"questStatus après start : {st}")
    h.expect(len(c.t("stageStatus")) >= 1, "quest/start doit écrire une ligne stageStatus par étape")
    gold = c.item(GOLD)
    c.call("quest/clear")
    h.expect(c.row("questStatus").get("type") == 2, "après quest/clear, questStatus doit être « terminée » (type 2 : résultat puis liste des quêtes)")
    h.expect(c.row("questResult").get("questId") == QUEST["questId"], "questResult doit décrire la quête finie")
    ql = master.QUEST_LEVELS.get(f"{QUEST['questId']}:1", {})
    if ql.get("gold"):
        h.expect(c.item(GOLD) == gold + ql["gold"], f"or de fin de quête : {gold} -> {c.item(GOLD)}, +{ql['gold']} attendu")


def test_quest_retire_and_continue(h):
    c = h.new_player()
    c.call("quest/start", QUEST)
    c.call("quest/continue")
    h.expect(c.row("questStatus").get("continueCount") == 1, "quest/continue doit incrémenter continueCount")
    c.call("quest/retire", {"isUserRetire": True})
    # idle = the last finished quest (type 4), or the new-account row (type 0) when none was finished yet
    h.expect(c.row("questStatus").get("type") != 1, "quest/retire doit remettre questStatus au repos")
    c.call("quest/start", QUEST)
    c.call("quest/clear")
    c.call("quest/start", QUEST)
    c.call("quest/retire", {"isUserRetire": True})
    st = c.row("questStatus")
    h.expect(st.get("type") == 4 and st.get("questId") == QUEST["questId"],
             f"après une quête finie, retire doit remettre la dernière quête terminée (type 4) : {st}")


def test_tower_floor_start(h):
    """Cathedral floor (event 601101000060, quest 8014033001 = TowerEventQuest 601600003001, party 101 as sent by the
    client): towerEventStatus.towerEventQuestId must be the floor id, the field found no floor with the quest id."""
    c = h.new_player()
    event_id, quest_id = 601101000060, 8014033001
    userdata.set_tables(c.uid, {"towerEventStatus": [{"userId": c.uid, "eventId": event_id, "towerLevel": 1, "state": 0,
                                                      "towerEventQuestId": 0, "towerEventQuestGroupId": 1,
                                                      "lastUsedRecoveryItemCountAt": 0, "usedRecoveryItemCount": 0,
                                                      "lostCount": 0}]})
    c.call("quest/start", dict(QUEST, eventId=event_id, questId=quest_id))
    st = c.row("towerEventStatus")
    h.expect(st.get("towerEventQuestId") == 601600003001 and st.get("state") == 1,
             f"towerEventStatus après quest/start d'un étage : {st}, towerEventQuestId 601600003001 attendu")
    # the client keeps only the stageStatus rows whose filterType is the quest's category (1 = tower): with 0 the
    # floor had no stage, so no boss battle (black field)
    rows = [r for r in c.t("stageStatus") if r.get("questId") == quest_id]
    h.expect(rows and all(r.get("filterType") == 1 for r in rows),
             f"stageStatus d'un étage : filterType {[r.get('filterType') for r in rows]}, 1 (tour) attendu")
    c.call("quest/start", QUEST)
    rows = [r for r in c.t("stageStatus") if r.get("questId") == QUEST["questId"]]
    h.expect(rows and all(r.get("filterType") == 0 for r in rows),
             f"stageStatus d'une quête normale : filterType {[r.get('filterType') for r in rows]}, 0 attendu")


def test_event_boss_filter_type(h):
    """Event boss quests (event.bin sub 2 field 2 = 2) showed a black battle: their stages need filterType 2."""
    c = h.new_player()
    event_id, quest_id = 601101000001, 8013002014
    h.expect(f"{event_id}:{quest_id}" in master.EVENT_BOSS_QUESTS, "8013002014 doit être une quête de boss")
    c.call("quest/start", dict(QUEST, eventId=event_id, questId=quest_id))
    rows = [r for r in c.t("stageStatus") if r.get("questId") == quest_id]
    h.expect(rows and all(r.get("filterType") == 2 for r in rows),
             f"stageStatus d'un boss d'événement : filterType {[r.get('filterType') for r in rows]}, 2 attendu")


def test_tower_next_floor(h):
    """Clearing a Cathedral floor must keep towerLevel (difficulty) and the cleared floor with state 2: the client
    unlocks the next floor from it. towerLevel + 1 / state 0 kept floor 2 locked (tester report, 2026-10-17)."""
    import api
    c = h.new_player()
    event_id = 601101000060
    status = {"userId": c.uid, "eventId": event_id, "towerLevel": 1, "state": 0, "towerEventQuestId": 0,
              "towerEventQuestGroupId": 1, "lastUsedRecoveryItemCountAt": 0, "usedRecoveryItemCount": 0,
              "lostCount": 0}
    userdata.set_tables(c.uid, {"towerEventStatus": [status]})
    c.call("quest/start", dict(QUEST, eventId=event_id, questId=8014033001, partyId=101))
    c.call("quest/clear")
    st = c.row("towerEventStatus")
    h.expect(st.get("towerLevel") == 1 and st.get("state") == 2 and st.get("towerEventQuestId") == 601600003001,
             f"après l'étage 1 : {st}, towerLevel 1 / state 2 / towerEventQuestId 601600003001 attendu")
    c.call("quest/start", dict(QUEST, eventId=event_id, questId=8014033002, partyId=101))
    c.call("quest/retire", {"isUserRetire": True})
    st = c.row("towerEventStatus")
    h.expect(st.get("state") == 2 and st.get("towerEventQuestId") == 601600003001,
             f"abandon de l'étage 2 : {st}, retour à « étage 1 réussi » attendu")
    # account broken by the old code: 2 floors cleared -> towerLevel 3, state 0
    userdata.set_tables(c.uid, {"towerEventStatus": [dict(status, towerLevel=3)]})
    api.tower_repair(c.uid)
    st = userdata.table(c.uid, "towerEventStatus")[0]
    h.expect(st.get("towerLevel") == 1 and st.get("state") == 2 and st.get("towerEventQuestId") == 601600003002,
             f"réparation d'un compte (2 étages faits) : {st}")


def test_quest_boost_items(h):
    """x2 EXP / Col / items did nothing: the client takes the enemies' experience, gold and drops from stageStatus
    as the server writes them (VPS log: same +28 EXP with x2 EXP). The server doubles them, and the clear gold."""
    import api
    c = h.new_player()
    boosts = [api.BOOST_GOLD, api.BOOST_EXP, api.BOOST_ITEM]
    grant = api.Grant(c.uid)
    for b in boosts:
        grant.item(b, 1)
    userdata.set_tables(c.uid, grant.changes())
    c.pull()

    def enemies():
        return {(enc["enemyPartyId"], e["enemyIndex"]): e for s in c.t("stageStatus")
                for enc in s["encountStatus"] + s["bossEncountStatus"] for e in enc["enemyStatus"]}
    c.call("quest/start", QUEST)
    plain = enemies()
    c.call("quest/retire", {"isUserRetire": True})
    c.call("quest/start", dict(QUEST, boostItemIds=boosts))
    h.expect(all(c.item(b) == 0 for b in boosts), "les boosts doivent être consommés")
    boosted = enemies()
    same = [k for k in boosted if k in plain]
    h.expect(same, "aucun ennemi commun pour comparer")
    for k in same:
        a, b = plain[k], boosted[k]
        h.expect(b["experience"] == 2 * a["experience"] and b["gold"] == 2 * a["gold"],
                 f"ennemi {k} : exp {a['experience']} -> {b['experience']}, or {a['gold']} -> {b['gold']} (x2 attendu)")
        drops = b["rewardGroupIds"]
        h.expect(len(drops) % 2 == 0 and drops[:len(drops) // 2] * 2 == drops, f"butin de l'ennemi {k} non doublé : {b['rewardGroupIds']}")
    ql = master.QUEST_LEVELS.get(f"{QUEST['questId']}:1", {})
    gold = c.item(GOLD)
    c.call("quest/clear")
    if ql.get("gold"):
        h.expect(c.item(GOLD) == gold + 2 * ql["gold"], f"or de fin de quête avec x2 Col : {gold} -> {c.item(GOLD)}")


def test_quest_skip(h):
    c = h.new_player()
    c.call("quest/skip", QUEST)
    h.expect(c.row("questStatus").get("type") == 2, "quest/skip doit finir la quête (type 2)")


def test_mission_and_gift(h):
    c = h.new_player()
    m = next((m for m in c.t("mission") if master.MISSION_REWARD.get(str(m["missionId"]))), None)
    h.expect(m, "aucune mission avec récompense dans le compte neuf")
    c.push({"mission": [dict(m, state=3, progress=1)]})
    c.call("mission/receive", {"missionIds": [m["missionId"]]})
    got = next(r for r in c.t("mission") if r["missionId"] == m["missionId"])
    h.expect(got["state"] == 4, f"mission reçue : state {got['state']}, 4 attendu")
    gifts = [g for g in c.t("gift") if g.get("state") == 0]
    if gifts:
        c.call("gift/receive", {"giftIds": [g["id"] for g in gifts]})
        h.expect(all(g["state"] == 1 for g in c.t("gift")), "les cadeaux ouverts doivent passer en state 1")


def test_lottery_draw_gems(h):
    c = h.new_player()
    userdata.add_gems(c.uid, 5000)
    price_id, price = next((k, p) for k, p in master.LOTTERY["prices"].items()
                           if p["type"] == 2 and p["cost"] and not p["limit"] and not p["ticket"])
    pcs = len(c.t("pc"))
    c.call("lottery/draw", {"gemAmount": price["cost"], "lotteryPriceId": int(price_id), "step": 1,
                            "ticketAmount": 0, "ticketId": 0})
    res = c.row("lotteryResult")
    h.expect(len(res.get("resultItems") or []) == 11, f"tirage x11 : {len(res.get('resultItems') or [])} résultats")
    h.expect(userdata.gems(c.uid) == 5000 - price["cost"], f"cubes : {userdata.gems(c.uid)}, {5000 - price['cost']} attendus")
    h.expect(len(c.t("pc")) >= pcs, "les personnages tirés doivent être ajoutés")


def test_lottery_refused_without_gems(h):
    c = h.new_player()
    price_id, price = next((k, p) for k, p in master.LOTTERY["prices"].items()
                           if p["type"] == 2 and p["cost"] and not p["limit"] and not p["ticket"])
    out = c.call("lottery/draw", {"gemAmount": price["cost"], "lotteryPriceId": int(price_id), "step": 1,
                                  "ticketAmount": 0, "ticketId": 0}, expect_code=None)
    h.expect(not (out.get("table") or {}).get("replace", {}).get("lotteryResult"), "tirage accordé sans cubes")
    h.note(out.get("code") == 0, "tirage refusé faute de cubes mais code 0 : le jeu croit que ça a marché")


def test_item_exchange(h):
    c = h.new_player()
    userdata.add_gems(c.uid, 100)
    c.call("item/exchange", {"exchangeOutputItemList": [{"count": 1, "id": 525600046, "inputGroupItemId": 5005}]})
    h.expect(any(r["exchangeOutputItemId"] == 525600046 for r in c.t("exchangedItem")), "exchangedItem non mis à jour")
    h.expect(userdata.gems(c.uid) == 95, f"coût de l'échange : {userdata.gems(c.uid)} cubes, 95 attendus")


def test_equipment_expand(h):
    c = h.new_player()
    userdata.add_gems(c.uid, 100)
    before = c.row("info").get("equipmentStockAddCount", 0)
    c.call("equipment/expand", {"addCount": 10, "consumeGem": 50})
    h.expect(c.row("info").get("equipmentStockAddCount") == before + 10, "equipmentStockAddCount non augmenté")


def test_boost_item_purchase(h):
    c = h.new_player()
    item_id, price = next(iter(master.BOOST_ITEMS.items()))
    c.push({"item": [{"userId": c.uid, "itemId": GOLD, "amount": price, "reservedAmount": 0, "readState": 0}]})
    c.call("quest/boost-item/purchase", {"boostItemIds": [int(item_id)]})
    h.expect(c.item(int(item_id)) >= 1, "objet de boost non reçu")
    h.expect(c.item(GOLD) == 0, f"or restant {c.item(GOLD)}, 0 attendu")


def test_boost_item_purchase_without_gold_answers(h):
    """Not enough gold: nothing bought, but the item table is sent (an empty answer froze the quest screen, Discord
    2026-10-30 "Freeze with skip ticket and Shia boost")."""
    c = h.new_player()
    shia = 249000001
    price = master.BOOST_ITEMS[str(shia)]
    c.push({"item": [{"userId": c.uid, "itemId": GOLD, "amount": price - 1, "reservedAmount": 0, "readState": 0}]})
    owned = c.item(shia)
    out = c.call("quest/boost-item/purchase", {"boostItemIds": [shia]})
    h.expect("item" in (out.get("table") or {}).get("replace", {}), f"table item absente de la réponse : {out}")
    h.expect(c.item(shia) == owned and c.item(GOLD) == price - 1, "achat sans assez d'or")


def test_guest_search(h):
    c = h.new_player()
    c.call("quest/supporter", {"level": 1, "questId": 8011012001})
    ids = [g["guestUserId"] for g in c.t("guest")][:2]
    c.call("guest/search", {"guestUserIds": ids})


def test_client_log(h):
    c = h.new_player()
    c.call("client/log", {"error": 7, "errorCode": 0, "failCount": 0, "message": "Unable to download file",
                          "type": "assetDownloadError"})


def test_lottery_extras(h):
    """lottery/select, pickup/select, point/exchange, item/custom/exchange: request bodies are guessed (no trace)."""
    c = h.new_player()
    items = master.LOTTERY.get("itemIndex", {})
    h.note(not items, "master.LOTTERY sans itemIndex")
    any_item = int(next(iter(items))) if items else 0
    c.call("lottery/pickup/select", {"lotteryItemId": any_item, "lotteryPriceId": 0})
    c.call("lottery/point/exchange", {"lotteryItemId": any_item, "count": 1})
    c.call("lottery/select", {"lotteryItemId": any_item, "lotteryPriceId": 0})
    c.call("item/custom/exchange", {"exchangeItemId": 0})


def test_quest_start_heals(h):
    """A new quest starts at full hp/mp (the client only pushes what is left after each battle)."""
    c = h.new_player()
    kirito = next(p for p in c.t("pc") if p["pcId"] == 130111001)
    c.push({"pc": [dict(kirito, hp=297, mp=10)]})
    c.call("quest/start", QUEST)
    kirito = next(p for p in c.t("pc") if p["pcId"] == 130111001)
    full = master.pc_max_hp_mp(130111001, kirito["totalExp"])
    h.expect((kirito["hp"], kirito["mp"]) == full, f"PV/PM au départ de quête : {kirito['hp']}/{kirito['mp']}, {full} attendus")
    c.call("quest/retire", {"isUserRetire": True})
    c.call("quest/start", QUEST)  # nobody hurt: the pc table is not resent


def test_enemy_hp_matches_client_rows(h):
    """Every enemy sent by quest/start is the enemy.bin sub 4 row the client fights with: level = the party's level
    for that member (enemy.bin sub 2 fields 46-50), hp = that row's hp (otherwise the hp bar overflows).
    Checked against the raw master JSON, independently of master.py."""
    import json
    import os
    rows = json.load(open(os.path.join(master.JSON_DIR, "enemy.4.json"), encoding="utf-8"))
    hp = {(r["2"], r["6"]): r["9"] for r in rows}
    parties = {r["0"]: r for r in json.load(open(os.path.join(master.JSON_DIR, "enemy.2.json"), encoding="utf-8"))}
    c = h.new_player()
    checked = 0
    for quest_id in (8011000001, 8011010001, 8011020001, 8012010001, 8014021001):  # last: event stage (level 800)
        c.call("quest/start", dict(QUEST, questId=quest_id))
        for st in c.t("stageStatus"):
            for enc in st["encountStatus"] + st["bossEncountStatus"]:
                party = parties[enc["enemyPartyId"]]
                for e in enc["enemyStatus"]:
                    want_level = party.get(str(46 + e["enemyIndex"])) or 1
                    h.expect(e["level"] == want_level, f"groupe {enc['enemyPartyId']} membre {e['enemyIndex']} : "
                                                       f"niveau {e['level']}, {want_level} attendu")
                    key = (e["enemyBaseId"], e["level"])
                    if key in hp:
                        h.expect(e["hp"] == hp[key], f"{key} : {e['hp']} PV envoyés, {hp[key]} dans la ligne du jeu")
                    else:  # between two sample levels: between their hp
                        near = sorted(v for (b, _), v in hp.items() if b == e["enemyBaseId"])
                        h.expect(near and near[0] <= e["hp"] <= near[-1], f"{key} : {e['hp']} PV hors de {near}")
                    checked += 1
        c.call("quest/retire", {"isUserRetire": True})
    h.expect(checked > 10, f"seulement {checked} ennemis vérifiés")


def test_event_clear_drops_and_first_clear(h):
    """EXP growth event (経験値の恵み集め): every clear rolls the quest level's drop table (EXP Water...), and the
    first clear reward (diamond cubes) is given once, not on every clear."""
    c = h.new_player()
    body = dict(QUEST, eventId=601102000007, questId=8014021001)
    ql = master.QUEST_LEVELS[f"{body['questId']}:1"]
    h.expect(ql.get("drops"), f"la quête doit avoir une table de butin : {ql}")
    gems = userdata.gems(c.uid)
    exp_items = lambda: sum(r["amount"] for r in c.t("item") if 246000011 <= r["itemId"] <= 246000074)
    before = exp_items()
    cubes = []
    for _ in range(2):
        c.call("quest/start", body)
        c.call("quest/clear")
        res = c.row("questResult")
        h.expect(res.get("questRewardGroupIds"), f"butin de fin de quête vide : {res.get('questRewardGroupIds')}")
        cubes.append(userdata.gems(c.uid) - gems)
        gems = userdata.gems(c.uid)
    h.expect(exp_items() > before, f"objets d'EXP : {before} -> {exp_items()}")
    first = sum(i["amount"] for i in master.REWARDS[str(ql["clear_reward"])]["items"])
    h.expect(cubes == [first, 0], f"cubes de 1re victoire : {cubes}, [{first}, 0] attendus")


def test_story_bonus_missions(h):
    """Story quest 1-1 (missions: all chests, nobody dead, boss rank S): the client pushes its battle results in
    stageStatus, the clear marks the 3 bonus missions and gives 1 Diamond Cube each + 2 for all, once."""
    c = h.new_player()
    body = dict(QUEST, questId=8011010001)
    missions = master.QUEST_LEVELS["8011010001:1"]["missions"]
    c.call("quest/start", body)
    stages = []
    for st in c.t("stageStatus"):
        st = dict(st)
        st["bossEncountStatus"] = [dict(e, battleWinRank=1, isRewarded=True, turnCount=5, deathCount=0)
                                   for e in st.get("bossEncountStatus") or []]
        st["treasureStatus"] = [dict(t, dropped=True) for t in st.get("treasureStatus") or []]
        stages.append(st)
    c.push({"stageStatus": stages})
    def cubes():
        return sum(g["amount"] for g in c.t("gift") if g.get("rewardType") == 3)
    before = cubes()
    c.call("quest/clear")
    res = c.row("questResult")
    h.expect(res["afterParams"]["bonusMission"] == [1, 1, 1], f"missions bonus : {res['afterParams']['bonusMission']}")
    row = next(r for r in c.t("questProgress") if r.get("questLevelId") == master.QUEST_LEVELS["8011010001:1"]["id"])
    h.expect(row.get("bonusMission") == [1, 1, 1] and row.get("bossClearRank") == 1, f"progression : {row}")
    h.expect(cubes() - before == len(missions) + 2, f"cubes au cadeau : +{cubes() - before}, +{len(missions) + 2} attendus")
    gained = cubes()
    c.call("quest/start", body)
    c.push({"stageStatus": stages})
    c.call("quest/clear")
    h.expect(cubes() == gained, "rejouer la quête ne redonne pas les cubes des missions déjà faites")


def test_lottery_ticket_multiuse(h):
    """Issue #5: using several Climax Scout Tickets at once (ticketAmount N on a 1-ticket price) gives N draws and
    consumes N tickets (seen in production: 6 and 5 tickets consumed for a single draw)."""
    c = h.new_player()
    ticket, price_id = 177000214, 20212291101
    c.push({"item": [{"userId": c.uid, "itemId": ticket, "amount": 6, "reservedAmount": 0, "readState": 0}]})
    c.call("lottery/draw", {"lotteryPriceId": price_id, "step": 0, "gemAmount": 0, "ticketId": ticket,
                            "ticketAmount": 5})
    results = c.row("lotteryResult").get("resultItems", [])
    h.expect(len(results) == 5, f"5 tickets -> {len(results)} tirage(s), 5 attendus")
    h.expect(c.item(ticket) == 1, f"tickets restants : {c.item(ticket)}, 1 attendu")
    row = next(r for r in c.t("lotteryPrice") if r["lotteryPriceId"] == price_id)
    h.expect(row["drawCount"] == 5, f"drawCount {row['drawCount']}, 5 attendu")
    c.call("lottery/draw", {"lotteryPriceId": price_id, "step": 0, "gemAmount": 0, "ticketId": ticket,
                            "ticketAmount": 5}, expect_code=None)
    h.expect(c.item(ticket) == 1, "sans assez de tickets, rien n'est consommé")


def test_chest_mission_counts_earlier_runs(h):
    """'Complete the quest with all field treasure chests opened': a chest opened in an earlier run stays open (the
    client remembers it in UserStage.treasure and cannot open it again), so it counts (reported on 1-2-1)."""
    c = h.new_player()
    body = dict(QUEST, questId=8011012001)
    missions = master.QUEST_LEVELS["8011012001:1"]["missions"]
    chest_mission = missions.index(8003000017)
    c.call("quest/start", body)
    stages, remembered = [], []
    for st in c.t("stageStatus"):
        st = dict(st)
        chests = st.get("treasureStatus") or []
        # the "modern" chest at position 17 was opened in an earlier run: still closed in this run's status
        st["treasureStatus"] = [dict(t, dropped=t["position"] != 17 or st["stageId"] != 9101200103) for t in chests]
        st["bossEncountStatus"] = [dict(e, battleWinRank=1, isRewarded=True, turnCount=5)
                                   for e in st.get("bossEncountStatus") or []]
        if st["stageId"] == 9101200103:
            remembered.append({"userId": c.uid, "stageLevelId": master.STAGE_LEVELS["9101200103:1"],
                               "treasure": [{"dropCount": 1, "lastGetAt": 0, "position": 17}]})
        stages.append(st)
    h.expect(remembered, "l'étape 9101200103 doit avoir des coffres")
    c.push({"stageStatus": stages, "stage": remembered})
    c.call("quest/clear")
    flags = c.row("questResult")["afterParams"]["bonusMission"]
    h.expect(flags[chest_mission] == 1, f"mission coffres : {flags}")


def test_boost_items_used_up(h):
    """Players kept their x2 EXP boost forever: the boost item is now taken at each quest start (one each)."""
    c = h.new_player()
    boost = 249000002
    c.push({"item": [{"userId": c.uid, "itemId": boost, "amount": 2, "reservedAmount": 0, "readState": 0}]})
    c.call("quest/start", dict(QUEST, boostItemIds=[boost]))
    h.expect(c.item(boost) == 1, f"boost après 1 quête : {c.item(boost)}, 1 attendu")
    h.expect(c.row("questSetting").get("boostItemIds") == [boost], "questSetting doit garder le boost utilisé")
    c.call("quest/clear")
    c.call("quest/start", dict(QUEST, boostItemIds=[boost]))
    c.call("quest/clear")
    c.call("quest/start", dict(QUEST, boostItemIds=[boost]))
    h.expect(c.item(boost) == 0, f"boost épuisé : {c.item(boost)}, 0 attendu (jamais négatif)")
    h.expect(c.row("questSetting").get("boostItemIds") == [], "un boost non possédé ne doit pas être appliqué")


def test_duplicate_character_crystals(h):
    """A duplicate 4* (Eydis) gives 100 crystals of its element (lottery.bin sub 7), not 1."""
    import api
    c = h.new_player()
    pc_id = 130124005  # [Knight of the Abyss] Eydis, 4*, dark
    item, amount = api.dup_item(pc_id)
    h.expect((item, amount) == (182000007, 100), f"doublon Eydis : {(item, amount)}, (182000007, 100) attendus")
    before = c.item(item)
    api.add_gift(c.uid, 5, pc_id, 1)
    api.add_gift(c.uid, 5, pc_id, 1)
    c.pull()
    c.call("gift/receive", {"giftIds": [g["id"] for g in c.t("gift") if g.get("state") == 0]})
    h.expect(sum(1 for p in c.t("pc") if p["pcId"] == pc_id) == 1, "le personnage ne doit être ajouté qu'une fois")
    h.expect(c.item(item) == before + 100, f"cristaux après doublon : {c.item(item) - before}, 100 attendus")
    # plus Custom Material R4 x25 (lottery.bin sub 7, field 7 = 1), never given before
    h.expect(api.dup_extra(pc_id) == (411000004, 25), f"bonus de doublon : {api.dup_extra(pc_id)}")
    h.expect(c.item(411000004) == 25, f"Custom Material R4 après doublon : {c.item(411000004)}, 25 attendus")


def test_operator_gift_missions_done_at_login(h):
    """Pre-registration gift and the social media contest prize stayed at 0/1: login completes them."""
    c = h.new_player()
    ids = {15100090001, 15100090040}
    rows = [m for m in c.t("mission") if m["missionId"] in ids]
    h.expect(len(rows) == 2, f"missions cadeau dans le compte neuf : {len(rows)}, 2 attendues")
    c.call("login")
    rows = [m for m in c.t("mission") if m["missionId"] in ids]
    h.expect(all(m["state"] == 3 and m["progress"] == 1 for m in rows), f"après login : {rows}")
    c.call("mission/receive", {"missionIds": sorted(ids)})
    c.call("login")
    h.expect(all(m["state"] == 4 for m in c.t("mission") if m["missionId"] in ids),
             "une mission reçue ne doit pas redevenir à recevoir")


def test_story_novel_quest_clear(h):
    """Story quests (type 3): the Unity StoryPlayer sends quest/start then quest/clear when the story ends. The
    clear counts one more play/clear in questProgress (CLEAR/COMPLETE stamp of the list) and gives the first
    clear reward only the first time. 0-1-2 is already cleared by a new player; 1-2-1 "Underworld" is not."""
    c = h.new_player()
    for quest_id in (8011000002, 8011011001):
        body = dict(QUEST, questId=quest_id)
        ql = master.QUEST_LEVELS[f"{quest_id}:1"]
        progress = lambda: next((r for r in c.t("questProgress") if r.get("questLevelId") == ql["id"]), {})
        before = progress().get("clearCount", 0)
        c.call("quest/start", body)
        h.expect(c.row("questStatus").get("questId") == quest_id, f"quest/start de la Story {quest_id}")
        c.call("quest/clear")
        h.expect(progress().get("clearCount") == before + 1, f"clear compté pour {quest_id} : {before} -> {progress()}")
        h.expect(c.row("questStatus").get("type") == 2, "questStatus « terminée » après la Story")
        first = c.row("questResult").get("firstClearRewardGroupId")
        h.expect(first == (ql.get("clear_reward") or 0 if before == 0 else 0), f"premier clear {quest_id} : {first}")
        c.call("quest/start", body)
        c.call("quest/clear")
        h.expect(c.row("questResult").get("firstClearRewardGroupId") == 0, "la récompense de premier clear n'est donnée qu'une fois")


def test_chests_go_to_explorable_stage(h):
    """Event quest 8013001002 visits area 511002029 three times: 201 (story scene, no field setup), 202 (explored),
    205 (scene). Its chests were given to 201, never shown, and the "open every chest" mission could not be done."""
    c = h.new_player()
    c.call("quest/start", dict(QUEST, questId=8013001002))
    by_stage = {s["stageId"]: s.get("treasureStatus") or [] for s in c.t("stageStatus")}
    h.expect(not by_stage.get(9300100201), f"étape scénario 201 : {by_stage.get(9300100201)}")
    h.expect(sorted(t["position"] for t in by_stage.get(9300100202, [])) == [16, 17, 18],
             f"coffres de l'étape explorée 202 : {by_stage.get(9300100202)}")


def test_stuck_quest_abandoned_after_two_boots(h):
    """Tester report (2026-10-13): the game crashed entering a quest, then at every boot showed "This battle cannot be
    resumed" and stayed stuck. One boot keeps the quest (the client may resume it); the second in a row abandons it."""
    c = h.new_player()
    c.call("quest/start", QUEST)
    c.call("start")
    h.expect(c.server_table("questStatus")[0].get("type") == 1, "une seule relance doit garder la quête (reprise)")
    c.call("start")
    st = c.server_table("questStatus")[0]
    h.expect(st.get("type") != 1, f"deux relances de suite doivent abandonner la quête bloquée : {st}")
    c.call("quest/start", QUEST)
    c.call("start")
    h.expect(c.server_table("questStatus")[0].get("type") == 1, "une nouvelle quête repart avec un compteur neuf")


def test_event_quest_drop_table(h):
    """Event quests have their own drop table (event.bin sub 2 field 8): event items, and the equipment events'
    armors. It was never rolled ("no armor drops"). Equipment Generation Vol.1 - Hard: Frost Armor R3 at 100 %."""
    c = h.new_player()
    before = {e["id"] for e in c.t("equipmentStock")}
    c.call("quest/start", dict(QUEST, questId=8014008002, eventId=601102000000 + 39))
    c.call("quest/clear")
    new = [e for e in c.t("equipmentStock") if e["id"] not in before]
    h.expect(any(str(e["equipmentId"]).startswith("375") for e in new),
             f"armure de l'événement attendue, équipements reçus : {[e['equipmentId'] for e in new]}")
    groups = c.row("questResult").get("questRewardGroupIds") or []
    h.expect(groups, "questRewardGroupIds doit lister les récompenses tirées")


def test_point_exchange_owned_character(h):
    """Exchanging 300 scout points for a character already owned crashed the client: the result did not say the
    character was replaced by crystals (replaceItemId/Amount), as lottery/draw does."""
    import api
    c = h.new_player()
    pc_id, point_item = 130124071, 257001227
    api.add_gift(c.uid, 5, pc_id, 1)
    c.pull()
    c.call("gift/receive", {"giftIds": [g["id"] for g in c.t("gift") if g.get("state") == 0]})
    c.push({"item": [{"userId": c.uid, "itemId": point_item, "amount": 300, "reservedAmount": 0, "readState": 0}]})
    c.call("lottery/point/exchange", {"lotteryItemId": 31227910001, "lotteryPriceId": 20212279101,
                                      "upperLimitLotteryPointItemId": point_item})
    res = c.row("lotteryResult")["resultItems"][0]
    item, amount = api.dup_item(pc_id)
    h.expect((res["replaceItemId"], res["replaceItemAmount"]) == (item, amount) and not res["isNew"],
             f"résultat d'échange d'un doublon : {res}")
    h.expect(c.item(point_item) == 0, f"points restants : {c.item(point_item)}")


def test_point_exchange_new_character(h):
    """The client crashed right after exchanging 300 points for [闇を制する者] Eydis (not owned): the result said
    lotteryPriceId 0; it must be the price the client sent."""
    c = h.new_player()
    point_item = 257001227
    c.push({"item": [{"userId": c.uid, "itemId": point_item, "amount": 300, "reservedAmount": 0, "readState": 0}]})
    c.call("lottery/point/exchange", {"lotteryItemId": 31227910001, "lotteryPriceId": 20212279101,
                                      "upperLimitLotteryPointItemId": point_item})
    res = c.row("lotteryResult")
    h.expect(res.get("lotteryPriceId") == 20212279101, f"lotteryResult.lotteryPriceId : {res.get('lotteryPriceId')}")
    h.expect(res["resultItems"][0]["isNew"] and any(p["pcId"] == 130124071 for p in c.t("pc")),
             f"Eydis doit être ajoutée : {res['resultItems']}")


def test_party_rests_after_quest(h):
    """A character knocked out in a quest stayed at 0 hp on the party screen (only quest/start healed): retire and
    clear now put the party back to full hp."""
    c = h.new_player()
    c.call("quest/start", QUEST)
    pcs = [dict(p, hp=0) if p["pcId"] == 130111001 else p for p in c.t("pc")]
    c.push(put={"pc": [p for p in pcs if p["pcId"] == 130111001]})
    c.call("quest/retire", {"isUserRetire": True})
    kirito = next(p for p in c.t("pc") if p["pcId"] == 130111001)
    full = master.pc_max_hp_mp(130111001, kirito["totalExp"])[0]
    h.expect(kirito["hp"] == full, f"PV de Kirito après abandon : {kirito['hp']}, {full} attendus")
    c.call("quest/start", QUEST)
    c.push(put={"pc": [dict(kirito, hp=1)]})
    c.call("quest/clear")
    kirito = next(p for p in c.t("pc") if p["pcId"] == 130111001)
    h.expect(kirito["hp"] == full, f"PV de Kirito après la fin de quête : {kirito['hp']}, {full} attendus")


def test_tower_floor_rewards_and_missions(h):
    """Cathedral: each floor's first-clear reward (event.bin sub 3 field 13: Fragments, cubes...) is given once, and
    the Cathedral missions count (both were never given, tester reports 2026-10-18 / 10-21)."""
    import missions
    c = h.new_player()
    event_id = 601101000060
    userdata.set_tables(c.uid, {"towerEventStatus": [{
        "userId": c.uid, "eventId": event_id, "towerLevel": 1, "state": 0, "towerEventQuestId": 0,
        "towerEventQuestGroupId": 1, "lastUsedRecoveryItemCountAt": 0, "usedRecoveryItemCount": 0, "lostCount": 0}]})
    mid = 15500360001  # "Complete Cathedral Assault difficulty level NORMAL" (kind 801, event 601101000060)
    rows = [dict(m) for m in userdata.table(c.uid, "mission")]
    if not any(m["missionId"] == mid for m in rows):
        rows.append(dict(rows[0], missionId=mid, state=2, progress=0, completedAt=0))
        userdata.set_tables(c.uid, {"mission": rows})
    floors = missions._tower_floors(str(event_id), "1")
    h.expect(floors, "étages NORMAL de 601101000060 introuvables")
    for q in floors:
        c.call("quest/start", dict(QUEST, eventId=event_id, questId=int(q), partyId=101))
        c.call("quest/clear")
        want = master.TOWER_FLOOR_REWARDS.get(q, 0)
        got = c.row("questResult").get("firstClearRewardGroupId")
        h.expect(got == want, f"étage {q} : récompense de premier passage {got}, {want} attendue")
    c.call("quest/start", dict(QUEST, eventId=event_id, questId=int(floors[0]), partyId=101))
    c.call("quest/clear")
    h.expect(not c.row("questResult").get("firstClearRewardGroupId"), "la récompense d'étage ne se donne qu'une fois")
    row = next(m for m in userdata.table(c.uid, "mission") if m["missionId"] == mid)
    h.expect(row["state"] == 3, f"mission « finir la Cathédrale NORMAL » : {row}")


def test_cube_shop_limit_starts_again(h):
    """Battle Skip in the cube shop (525600066, 10 per period, item.bin sub 9 field 11): the count starts again,
    after 10 it was refused for good (tester report, 2026-10-25)."""
    c = h.new_player()
    userdata.add_gems(c.uid, 1000)
    rows = [{"userId": c.uid, "exchangeOutputItemId": 525600066, "count": 10, "lastUpdatedAt": 1, "readState": 0}]
    userdata.set_tables(c.uid, {"exchangedItem": rows})
    c.call("item/exchange", {"exchangeOutputItemList": [{"count": 1, "id": 525600066, "inputGroupItemId": 5005}]})
    row = next((r for r in c.t("exchangedItem") if r["exchangeOutputItemId"] == 525600066), {})
    h.expect(row.get("count") == 1, f"Battle Skip après la période : {row}")
    h.expect(any(i["itemId"] == 249000005 and i["amount"] >= 1 for i in c.t("item")), "Battle Skip non reçu")


def test_refusals_answer_with_tables(h):
    """A refused draw / purchase (not enough Diamond Cubes) answered {}: the client waited for the change it expected.
    It now gets the current rows of the tables involved, and nothing is taken."""
    c = h.new_player()
    userdata.add_gems(c.uid, -userdata.gems(c.uid))
    price = next(int(pid) for pid, p in master.LOTTERY["prices"].items() if p["type"] == 2 and p["cost"] == 250)
    out = c.call("lottery/draw", {"gemAmount": 250, "lotteryPriceId": price, "step": 0, "ticketAmount": 0,
                                  "ticketId": 0})
    h.expect("lotteryPrice" in (out.get("table") or {}).get("replace", {}), f"tirage refusé sans tables : {out}")
    h.expect(not c.row("lotteryResult").get("resultItems") or c.row("lotteryResult").get("lotteryPriceId") != price,
             "un tirage sans Diamond Cubes ne doit rien donner")
    out = c.call("season-reward/unlock", {"seasonRewardId": 5022209100, "cost": 2196})
    h.expect("item" in (out.get("table") or {}).get("replace", {}), f"achat de niveau refusé sans tables : {out}")
    h.expect(c.item(405220900) == 0, "points de saison donnés sans payer")
    out = c.call("equipment/expand", {"addCount": 5, "consumeGem": 50})
    h.expect("info" in (out.get("table") or {}).get("replace", {}), "agrandissement refusé sans tables")
