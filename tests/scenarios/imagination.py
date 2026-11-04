"""Imaginarium (reward.bin rewardType 7, item.20 380xxxxxx): one UserImaginationStock row each, numbered past
info.lastImaginationStockId; they used to be dropped (Battle Pass tiers, login bonuses, gift box)."""
import api
from harness import master, userdata

SACHI_IMAG = 380124027  # Imaginarium of [Dear You,] Sachi


def test_imagination_reward_and_gift(h):
    c = h.new_player()
    grant = api.Grant(c.uid)
    h.expect(grant.reward(7, SACHI_IMAG, 2), "rewardType 7 non géré")
    userdata.set_tables(c.uid, grant.changes())
    c.pull()
    rows = [r for r in c.t("imaginationStock") if r["imaginationId"] == SACHI_IMAG]
    h.expect(len(rows) == 2 and len({r["id"] for r in rows}) == 2, f"stock d'Imaginarium : {c.t('imaginationStock')}")
    h.expect(c.row("info").get("lastImaginationStockId") == max(r["id"] for r in rows),
             f"lastImaginationStockId : {c.row('info').get('lastImaginationStockId')}")
    # numbering continues past the client's counter
    info = dict(c.row("info"), lastImaginationStockId=50)
    c.push(put={"info": [info]})
    grant = api.Grant(c.uid)
    grant.reward(7, SACHI_IMAG, 1)
    userdata.set_tables(c.uid, grant.changes())
    c.pull()
    h.expect(max(r["id"] for r in c.t("imaginationStock")) == 51, "l'id doit suivre lastImaginationStockId")
    # a gift of type 7 opens now
    gifts = list(userdata.table(c.uid, "gift"))
    gifts.append({"userId": c.uid, "id": 9999, "senderType": 1, "senderId": 0, "rewardType": 7, "itemId": SACHI_IMAG,
                  "level": 0, "amount": 1, "reasonId": 0, "state": 0, "startAt": 0, "expireAt": 0, "createdAt": 0})
    userdata.set_tables(c.uid, {"gift": gifts})
    c.pull()
    c.call("gift/receive", {"giftIds": [9999]})
    h.expect(next(g for g in c.t("gift") if g["id"] == 9999)["state"] == 1, "cadeau d'Imaginarium non ouvert")
    h.expect(len([r for r in c.t("imaginationStock") if r["imaginationId"] == SACHI_IMAG]) == 4, "Imaginarium du cadeau absent")


def test_season_reward_imagination(h):
    """A Battle Pass tier whose reward group holds an Imaginarium gives it."""
    tier = next((rid for rid, t in master.SEASON["rewards"].items()
                 if any(i["rewardType"] == 7 for i in master.REWARDS.get(str(t["reward"]), {}).get("items", []))), None)
    h.expect(tier, "aucun palier de saison avec un Imaginarium")
    c = h.new_player()
    c.call("season-reward/receive", {"seasonRewardIds": [int(tier)]})
    h.expect(c.t("imaginationStock"), f"palier {tier} : Imaginarium absent")


def test_imagination_backfill_at_login(h):
    """Imaginarium claimed as items (Discord bot) and Battle Pass tiers received before the fix: given at login, once."""
    c = h.new_player()
    tier = next(rid for rid, t in master.SEASON["rewards"].items()
                if any(i["rewardType"] == 7 for i in master.REWARDS.get(str(t["reward"]), {}).get("items", [])))
    grant = api.Grant(c.uid)
    grant.item(SACHI_IMAG, 3)  # what the bot did before: a plain item
    userdata.set_tables(c.uid, {**grant.changes(), "seasonReward": [
        {"userId": c.uid, "seasonRewardId": int(tier), "acquiredCount": 1, "lastAcquiredAt": 1}]})
    c.pull()
    c.call("login")
    c.pull()
    sachi = [r for r in c.t("imaginationStock") if r["imaginationId"] == SACHI_IMAG]
    h.expect(len(sachi) == 3 and c.item(SACHI_IMAG) == 0, f"conversion des objets : {len(sachi)}, item {c.item(SACHI_IMAG)}")
    total = len(c.t("imaginationStock"))
    h.expect(total > 3, "Imaginarium du palier de saison non rattrapé")
    c.call("login")
    c.pull()
    h.expect(len(c.t("imaginationStock")) == total, "rattrapage fait deux fois")
    # sent as an item now: becomes a real Imaginarium
    api.add_gift(c.uid, 1, SACHI_IMAG, 1)
    h.expect(userdata.table(c.uid, "gift")[-1]["rewardType"] == 7, "Imaginarium offert en type 1")


def test_iskahn_and_tales_kirito_imagination(h):
    """The Iskahn and Tales of ARISE Kirito Imaginarium are 381xxxxxx: they were not recognised, so a gift or a
    claim put them in UserItem, never shown (tester report, Discord 2026-10-28). Now Imaginarium, and the ones already in
    UserItem move to the stock at the next login."""
    for imag in (381214001, 381214004):
        h.expect(api.is_imagination(imag), f"{imag} doit être un Imaginarium")
    c = h.new_player()
    api.add_gift(c.uid, 1, 381214001, 1)
    c.pull()
    gift = next(g for g in c.t("gift") if g["itemId"] == 381214001)
    h.expect(gift["rewardType"] == 7, f"cadeau en rewardType {gift['rewardType']}, 7 attendu")
    c.call("gift/receive", {"giftIds": [gift["id"]]})
    h.expect(any(r["imaginationId"] == 381214001 for r in c.t("imaginationStock")), "Imaginarium d'Iskahn non reçu")
    # an account that got one as a plain item before the fix (the one-time backfill already ran)
    userdata.set_counter(c.uid, "imagination_backfill", 1)
    items = [dict(r) for r in userdata.table(c.uid, "item")] + [
        {"userId": c.uid, "itemId": 381214004, "amount": 1, "reservedAmount": 0, "readState": 0}]
    userdata.set_tables(c.uid, {"item": items})
    c.pull()
    c.call("login")
    h.expect(any(r["imaginationId"] == 381214004 for r in c.t("imaginationStock")), "Imaginarium de Kirito non déplacé")
    h.expect(c.item(381214004) == 0, "l'objet doit quitter UserItem")
