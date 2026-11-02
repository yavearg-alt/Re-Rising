"""Friends (follow / follower) and real players as quest supporters. Bodies from the client (FriendManager)."""


def test_follow_and_unfollow(h):
    """Friends > Search found the player but Follow did nothing (tester report, 2026-10-17): friend/follow/request had no
    route, so no UserFollow row."""
    other = h.new_player()
    c = h.new_player()
    c.call("guest/search", {"guestUserIds": [other.uid]})
    c.call("friend/follow/request", {"userId": c.uid, "followUserId": other.uid})
    h.expect([r.get("followUserId") for r in c.t("follow")] == [other.uid], f"follow après Follow : {c.t('follow')}")
    h.expect(any(g.get("guestUserId") == other.uid for g in c.t("guest")), "le profil suivi doit être dans guest")
    other.pull()
    h.expect([r.get("followerUserId") for r in other.t("follower")] == [c.uid],
             f"le joueur suivi doit avoir un follower : {other.t('follower')}")
    c.call("friend/follow/request", {"userId": c.uid, "followUserId": other.uid})
    h.expect(len(c.t("follow")) == 1, "suivre deux fois ne doit pas dupliquer la ligne")
    c.call("friend/follow/remove", {"userId": c.uid, "followUserId": other.uid})
    h.expect(not c.t("follow"), f"follow après remove : {c.t('follow')}")
    other.pull()
    h.expect(not other.t("follower"), "le follower doit disparaître chez l'autre joueur")
    # removing a follower drops the other side's follow
    other.call("friend/follow/request", {"userId": other.uid, "followUserId": c.uid})
    c.pull()
    c.call("friend/follower/remove", {"userId": c.uid, "followerUserId": other.uid})
    h.expect(not c.t("follower"), "follower après follower/remove")
    other.pull()
    h.expect(not other.t("follow"), "le follow de l'autre joueur doit disparaître")


def test_real_supporters(h):
    """quest/supporter only listed the made-up guests: followed players come first, then other real players."""
    friend = h.new_player()
    stranger = h.new_player()
    c = h.new_player()
    c.call("friend/follow/request", {"userId": c.uid, "followUserId": friend.uid})
    c.call("quest/supporter", {"level": 1, "questId": 8011012001})
    ids = [g.get("guestUserId") for g in c.t("guest")]
    h.expect(ids and ids[0] == friend.uid, f"le joueur suivi doit être le premier soutien : {ids[:4]}")
    h.expect(stranger.uid in ids, "les autres joueurs doivent aussi être proposés")
    h.expect(c.uid not in ids, "on ne doit pas être son propre soutien")
    first = c.t("guest")[0]
    h.expect(first["supporterPcSlot"] and first["supporterPcSlot"][0]["pcId"] in [p["pcId"] for p in first["pc"]],
             f"le soutien réel doit avoir un personnage de soutien : {first['supporterPcSlot']}")
    # picking the real supporter at quest start fills the supporter slot
    slot = first["supporterPcSlot"][0]
    c.call("quest/start", {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001,
                           "supporterPcId": slot["pcId"], "supporterPcSlotId": slot["id"],
                           "supporterUserId": friend.uid})
    h.expect(c.row("questSupporterSlot").get("supporterUserId") == friend.uid,
             f"questSupporterSlot : {c.row('questSupporterSlot')}")


def test_rotation_tutorial_supporter(h):
    """Rotation tutorial 1-2-2 (8011011002): quest.bin imposes Eugeo 130111002, already in a new account's party.
    Imposing him left the sub member slot EMPTY and the tutorial could not go on (tester report, 2026-10-18): the usual
    supporter list must be offered instead."""
    c = h.new_player()
    members = {sl.get("pcId") for p in c.t("party") for sl in p.get("slots", [])}
    h.expect(130111002 in members, f"équipe d'un nouveau compte : {members}")
    c.call("quest/supporter", {"level": 1, "questId": 8011011002})
    pcs = [p["pcId"] for g in c.t("guest") for p in g["pc"]]
    h.expect(pcs and 130111002 not in pcs, f"soutiens du tuto rotation : {pcs}")


def test_imposed_supporter(h):
    """Beginner's Residence lesson 8014061001 (party restricted to Kirito, quest.bin sub 9 imposes Eugeo
    130114020 Lv100): the supporter list must be that Eugeo only, a Kirito supporter blocked the start (2026-10-17)."""
    h.new_player()
    c = h.new_player()
    c.call("quest/supporter", {"level": 1, "questId": 8014061001})
    pcs = [p["pcId"] for g in c.t("guest") for p in g["pc"]]
    h.expect(pcs == [130114020], f"soutiens de la leçon : {pcs}, [130114020] attendu")
    # below 1000 = NPC for the client: its incarnate is usable (a player id needs mutual friendship)
    h.expect(all(g["guestUserId"] < 1000 for g in c.t("guest")), f"id du soutien imposé : {c.t('guest')[0]['guestUserId']}")
    pc = c.t("guest")[0]["pc"][0]
    c.call("quest/start", {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8014061001,
                           "supporterPcId": pc["pcId"], "supporterPcSlotId": 1,
                           "supporterUserId": c.t("guest")[0]["guestUserId"]})
    h.expect(c.row("questSupporterSlot").get("pc", {}).get("pcId") == 130114020,
             f"questSupporterSlot de la leçon : {c.row('questSupporterSlot')}")
