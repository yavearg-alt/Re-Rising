"""Friend points (item 205000007) for the quest supporter: 30 with a friend (followed / follower), 10 otherwise,
none without a supporter; shown on the result screen (UserQuestResult.friendPoint)."""
FRIEND_POINT = 205000007
QUEST = {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001, "supporterPcId": 0,
         "supporterPcSlotId": 0, "supporterUserId": 0}


def _play(c, supporter=0):
    c.call("quest/start", dict(QUEST, supporterUserId=supporter))
    c.call("quest/clear")
    return c.row("questResult")


def test_friend_points(h):
    friend, stranger, c = h.new_player(), h.new_player(), h.new_player()
    c.call("friend/follow/request", {"userId": c.uid, "followUserId": friend.uid})
    res = _play(c)
    h.expect(res.get("friendPoint") == 0 and c.item(FRIEND_POINT) == 0, f"points sans soutien : {res.get('friendPoint')}")
    res = _play(c, friend.uid)
    h.expect(res.get("friendPoint") == 30 and res.get("supporterUserId") == friend.uid, f"résultat avec un ami : {res}")
    h.expect(c.item(FRIEND_POINT) == 30, f"points d'amitié après un ami : {c.item(FRIEND_POINT)}")
    res = _play(c, stranger.uid)
    h.expect(res.get("friendPoint") == 10 and c.item(FRIEND_POINT) == 40, f"avec un autre joueur : {c.item(FRIEND_POINT)}")
    # the follower side counts as a friend too
    res = _play(friend, c.uid)
    h.expect(res.get("friendPoint") == 30, f"le joueur suivi doit aussi gagner 30 points : {res.get('friendPoint')}")


def test_training_room_abilities(h):
    """Training Room C (event 601102000018, quest 8014044003): its clear drops include rewardType 8 (equipment
    ability items), which used to be skipped."""
    from harness import master  # noqa: PLC0415
    import api  # noqa: PLC0415
    c = h.new_player()
    grant = api.Grant(c.uid)
    h.expect(grant.reward(8, 402900040, 2), "rewardType 8 non géré")
    api.userdata.set_tables(c.uid, grant.changes())
    c.pull()
    h.expect(c.item(402900040) == 2, f"capacité d'équipement absente : {c.item(402900040)}")
    got = 0
    for _ in range(15):
        c.call("quest/start", dict(QUEST, questId=8014044003, eventId=601102000018))
        c.call("quest/clear")
        got = sum(r["amount"] for r in c.t("item") if 402900000 <= r["itemId"] < 403000000) - 2
        if got:
            break
    h.expect(got > 0, "aucune capacité après 15 Training Room")
    h.expect(master.QUEST_LEVELS.get("8014044003:1"), "quête Training Room absente")


def test_exchange_limits(h):
    """item.bin sub 9: field 2 is the purchase limit, field 4 only the display order (the cube shop's Item Boost
    x2, order 94, stopped selling after 94; the illustrations have a limit of 1)."""
    from harness import master, userdata  # noqa: PLC0415
    out = master.EXCHANGE["out"]
    h.expect(out["525600050"]["max"] == 0, f"Item Boost x2 limité : {out['525600050']}")
    h.expect(out["525130082"]["max"] == 1, f"illustration non limitée à 1 : {out['525130082']}")
    c = h.new_player()
    userdata.add_gems(c.uid, 2000)
    userdata.set_tables(c.uid, {"exchangedItem": [{"userId": c.uid, "exchangeOutputItemId": 525600050, "count": 94,
                                                   "lastUpdatedAt": 1, "readState": 0}]})
    c.pull()
    c.call("item/exchange", {"exchangeOutputItemList": [{"count": 2, "id": 525600050, "inputGroupItemId": 5005}]})
    h.expect(c.item(249000003) == 2, f"Item Boost x2 après 94 achats : {c.item(249000003)}")
    c.call("item/exchange", {"exchangeOutputItemList": [{"count": 1, "id": 525130082, "inputGroupItemId": 5125}]})
    gems = userdata.gems(c.uid)
    c.call("item/exchange", {"exchangeOutputItemList": [{"count": 1, "id": 525130082, "inputGroupItemId": 5125}]})
    h.expect(userdata.gems(c.uid) == gems, "illustration achetée deux fois")


def test_ticket_boss_consumes_tickets(h):
    """Ticket Boss quests (event.bin sub 2 field 5 ticket label, field 6 cost) take their tickets at quest start."""
    from harness import master, userdata  # noqa: PLC0415
    import api  # noqa: PLC0415
    key, (label, cost) = next(iter(master.EVENT_BOSS_TICKETS.items()))
    event_id, quest_id = map(int, key.split(":"))
    ticket = master.EXCHANGE["labels"][label]
    c = h.new_player()
    grant = api.Grant(c.uid)
    grant.item(ticket, cost + 5)
    userdata.set_tables(c.uid, grant.changes())
    c.pull()
    c.call("quest/start", dict(QUEST, questId=quest_id, eventId=event_id))
    h.expect(c.item(ticket) == 5, f"tickets après un boss à {cost} : {c.item(ticket)} (5 attendus)")
    c.call("quest/retire")
    c.call("quest/start", dict(QUEST, questId=quest_id, eventId=event_id))
    h.expect(c.item(ticket) == 0, f"jamais en dessous de 0 : {c.item(ticket)}")


def test_guild_mission_point_gift_opens(h):
    """rewardType 9 ("Guild mission pt", guild mission rewards) opens instead of staying in the gift box forever."""
    from harness import userdata  # noqa: PLC0415
    c = h.new_player()
    gifts = list(userdata.table(c.uid, "gift"))
    gifts.append({"userId": c.uid, "id": 7777, "senderType": 1, "senderId": 0, "rewardType": 9, "itemId": 221003716,
                  "level": 0, "amount": 90, "reasonId": 400100012001, "state": 0, "startAt": 0, "expireAt": 0,
                  "createdAt": 0})
    userdata.set_tables(c.uid, {"gift": gifts})
    c.pull()
    c.call("gift/receive", {"giftIds": [7777]})
    h.expect(next(g for g in c.t("gift") if g["id"] == 7777)["state"] == 1, "cadeau de points de guilde non ouvert")
