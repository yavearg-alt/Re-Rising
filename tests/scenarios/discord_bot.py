"""Discord bot API (server/botapi.py): account linking through the profile comment, claims approved / refused,
duplicate characters, rules (caps, cooldowns, auto-approval). Driven through its HTTP handler, like the bot does."""
import itertools
import json
import os

import botapi
from harness import TMP, db, userdata

botapi.SECRET = "test-bot-secret-0123456789"
AUTH = {"Authorization": f"Bearer {botapi.SECRET}"}
RULES = os.path.join(TMP, "discord_rules_test.json")
botapi.RULES_PATH = RULES
_ids = itertools.count(900000000000000001)
RAIN, SACHI = 130224001, 130123002


def call(h, path, body=None, method="POST", headers=AUTH, expect=200) -> dict:
    code, out = botapi.handle(method, path, headers, json.dumps(body or {}).encode())
    h.expect(code == expect, f"{method} {path} : HTTP {code}, {expect} attendu ({out})")
    return out


def set_rules(rules: dict | None):
    if rules is None:
        if os.path.exists(RULES):
            os.remove(RULES)
        return
    with open(RULES, "w", encoding="utf-8") as f:
        json.dump(rules, f)
    os.utime(RULES, (os.path.getmtime(RULES) + 1,) * 2)  # several writes within one second: new mtime anyway


def set_comment(c, text: str):
    info = dict(c.row("info"), comment=text)
    c.push(put={"info": [info]}, trigger="profile")


def linked_player(h, rules=None):
    """A new player linked to a new Discord id."""
    set_rules(rules)
    c, did = h.new_player(), str(next(_ids))
    code = call(h, "/bot/link/start", {"discord_id": did})["code"]
    set_comment(c, code)
    out = call(h, "/bot/link/check", {"discord_id": did})
    h.expect(out.get("ok"), f"liaison refusée : {out}")
    return c, did


def claim(h, did, kind, target=None, amount=1) -> dict:
    return call(h, "/bot/claims", {"discord_id": did, "discord_name": "tester", "kind": kind, "target": target,
                                   "amount": amount})


def decide(h, cid, approve, note="") -> dict:
    return call(h, f"/bot/claims/{cid}/decide", {"approve": approve, "staff_id": "42", "staff_name": "staff",
                                                 "note": note})


def open_gifts(c):
    c.pull()
    ids = [g["id"] for g in c.t("gift") if g["state"] == 0]
    c.call("gift/receive", {"giftIds": ids})


def test_auth(h):
    call(h, "/bot/whoami", {"discord_id": "1"}, headers={}, expect=401)
    call(h, "/bot/whoami", {"discord_id": "1"}, headers={"Authorization": "Bearer wrong"}, expect=401)
    call(h, "/bot/nope", {"discord_id": "1"}, expect=404)
    secret, botapi.SECRET = botapi.SECRET, ""
    try:
        call(h, "/bot/whoami", {"discord_id": "1"}, expect=503)  # no secret configured: API off
    finally:
        botapi.SECRET = secret
    chars = call(h, "/bot/characters", method="GET")["characters"]
    h.expect(any(k == str(RAIN) for k, _ in chars), "Rain absente de la liste des personnages")
    h.expect(any(k == "246000011" for k, _ in call(h, "/bot/items", method="GET")["items"]), "objet absent")


def test_link_profile_comment(h):
    set_rules(None)
    c, did = h.new_player(), str(next(_ids))
    h.expect(not call(h, "/bot/whoami", {"discord_id": did})["linked"], "lié avant /link")
    start = call(h, "/bot/link/start", {"discord_id": did})
    code = start["code"]
    h.expect(code.startswith("UB") and len(code) == 8, f"code {code}")
    h.expect(call(h, "/bot/link/start", {"discord_id": did})["code"] == code, "/link redonne un autre code")
    out = call(h, "/bot/link/check", {"discord_id": did})
    h.expect(out.get("error") == "not_found", f"lié sans le code dans le commentaire : {out}")
    # typed by hand in the comment: lower case, a dash, other text around it
    set_comment(c, f"hello {code[:4].lower()}-{code[4:].lower()} :)")
    out = call(h, "/bot/link/check", {"discord_id": did})
    h.expect(out.get("ok") and out["account"]["uid"] == c.uid, f"liaison : {out}")
    me = call(h, "/bot/whoami", {"discord_id": did})
    h.expect(me["linked"] and me["account"]["uid"] == c.uid, f"whoami : {me}")
    h.expect(call(h, "/bot/link/check", {"discord_id": did}).get("error") == "no_code", "code réutilisable")
    # another Discord user cannot take this account
    other = str(next(_ids))
    code2 = call(h, "/bot/link/start", {"discord_id": other})["code"]
    set_comment(c, code2)
    out = call(h, "/bot/link/check", {"discord_id": other})
    h.expect(out.get("error") == "taken", f"compte déjà lié pris par un autre : {out}")
    # re-linking to another account (Mosdra: gifts sent to an old account)
    c2 = h.new_player()
    code3 = call(h, "/bot/link/start", {"discord_id": did})["code"]
    set_comment(c2, code3)
    out = call(h, "/bot/link/check", {"discord_id": did})
    h.expect(out.get("ok") and out["account"]["uid"] == c2.uid, f"changement de compte : {out}")
    h.expect(call(h, "/bot/unlink", {"discord_id": did})["ok"], "unlink")
    h.expect(not call(h, "/bot/whoami", {"discord_id": did})["linked"], "toujours lié après unlink")
    h.expect(call(h, "/bot/unlink", {"discord_id": did}).get("error") == "not_linked", "double unlink")
    out = claim(h, did, "cubes", amount=10)
    h.expect(out.get("error") == "not_linked", f"réclamation sans compte lié : {out}")


def test_claim_approve_refuse(h):
    c, did = linked_player(h)
    before = userdata.gems(c.uid)
    out = claim(h, did, "cubes", amount=10000)
    h.expect(out.get("ok") and not out["auto"] and out["claim"]["status"] == "pending", f"réclamation : {out}")
    cid = out["claim"]["id"]
    h.expect(not [g for g in userdata.table(c.uid, "gift") if g["rewardType"] == 3 and g["state"] == 0],
             "cadeau envoyé avant validation")
    out = decide(h, cid, True)
    h.expect(out.get("ok") and out["claim"]["status"] == "approved", f"validation : {out}")
    led = db.get_discord_claim(cid)
    h.expect(led["decided_by"] == "42" and led["snapshot_id"], f"registre : {led}")
    h.expect(db.get_snapshot(led["snapshot_id"])["user_id"] == c.uid, "sauvegarde du compte avant cadeau absente")
    gifts = [g for g in userdata.table(c.uid, "gift") if g["rewardType"] == 3 and g["state"] == 0]
    h.expect(len(gifts) == 1 and gifts[0]["amount"] == 10000 and gifts[0]["reasonId"] == 400100090000,
             f"cadeau : {gifts}")
    h.expect(decide(h, cid, True).get("error") == "already_decided", "réclamation validée deux fois")
    open_gifts(c)
    h.expect(userdata.gems(c.uid) == before + 10000, f"cubes après ouverture : {userdata.gems(c.uid)}")
    # refused: nothing in the gift box, the reason is kept
    out = claim(h, did, "item", "246000011", 5)
    h.expect(out.get("ok") and out["claim"]["label"].startswith("5 × "), f"réclamation objet : {out}")
    n = len(userdata.table(c.uid, "gift"))
    out = decide(h, out["claim"]["id"], False, "not now")
    h.expect(out["claim"]["status"] == "refused" and out["claim"]["note"] == "not now", f"refus : {out}")
    h.expect(len(userdata.table(c.uid, "gift")) == n, "cadeau envoyé malgré le refus")
    for bad, why in ((("item", "999999999", 1), "objet inconnu"), (("cubes", None, 0), "0 cube"),
                     (("cubes", None, 10 ** 9), "montant énorme"), (("gold", None, 1), "type inconnu"),
                     (("character", "nobody at all", 1), "personnage inconnu")):
        h.expect(not claim(h, did, *bad).get("ok"), f"{why} accepté")
    hist = call(h, "/bot/claims/list", {"discord_id": did})["claims"]
    h.expect([x["status"] for x in hist] == ["refused", "approved"], f"historique : {hist}")


def test_character_duplicates(h):
    c, did = linked_player(h)
    owned = c.t("pc")[0]["pcId"]
    h.expect(claim(h, did, "character", str(owned)).get("error") == "owned", "personnage déjà possédé accepté")
    out = claim(h, did, "character", "[Dusk Approaching] Rain")
    h.expect(out.get("ok") and out["claim"]["target"] == RAIN, f"réclamation par nom : {out}")
    h.expect(claim(h, did, "character", str(RAIN)).get("error") == "already_claimed", "double réclamation en attente")
    decide(h, out["claim"]["id"], True)
    h.expect(claim(h, did, "character", str(RAIN)).get("error") == "in_gifts", "personnage déjà dans les cadeaux")
    open_gifts(c)
    h.expect(any(p["pcId"] == RAIN for p in c.t("pc")), "Rain absente après ouverture du cadeau")
    h.expect(claim(h, did, "character", str(RAIN)).get("error") == "owned", "Rain réclamée une 2e fois")
    # pending, then the account gets the character elsewhere: approval refuses instead of granting a duplicate
    out = claim(h, did, "character", str(SACHI))
    import api  # noqa: PLC0415
    api.add_gift(c.uid, 5, SACHI, 1, 1, 0)
    out = decide(h, out["claim"]["id"], True)
    h.expect(out.get("error") == "in_gifts" and out["claim"]["status"] == "refused", f"doublon validé : {out}")


def test_rules_caps_cooldown_auto(h):
    try:
        _rules(h)
    finally:
        set_rules(None)


def _rules(h):
    # caps: 2 cube claims / 15000 cubes per week
    c, did = linked_player(h, {"max_pending": 10, "caps": {"cubes": {"days": 7, "max_total": 15000, "max_claims": 2}}})
    h.expect(claim(h, did, "cubes", amount=10000).get("ok"), "1re réclamation refusée")
    out = claim(h, did, "cubes", amount=6000)
    h.expect(out.get("error") == "limit" and "5000 left" in out["message"], f"plafond de cubes : {out}")
    second = claim(h, did, "cubes", amount=5000)
    h.expect(second.get("ok"), f"réclamation sous le plafond refusée : {second}")
    decide(h, second["claim"]["id"], False)  # refused claims do not use up the caps
    h.expect(claim(h, did, "cubes", amount=1000).get("ok"), "un refus compte dans le plafond")
    h.expect(claim(h, did, "cubes", amount=1).get("error") == "limit", "plafond de nombre de réclamations")
    h.expect(claim(h, did, "item", "246000011", 1).get("ok"), "le plafond des cubes bloque les objets")
    # pending limit (default rules: 3)
    c, did = linked_player(h)
    for _ in range(3):
        h.expect(claim(h, did, "cubes", amount=1).get("ok"), "réclamation refusée sous la limite d'attente")
    out = claim(h, did, "cubes", amount=1)
    h.expect(out.get("error") == "limit" and "waiting" in out["message"], f"limite d'attente : {out}")
    # cooldown
    c, did = linked_player(h, {"cooldown_seconds": 3600})
    h.expect(claim(h, did, "cubes", amount=1).get("ok"), "1re réclamation refusée")
    out = claim(h, did, "cubes", amount=1)
    h.expect(out.get("error") == "limit" and "wait" in out["message"], f"délai entre réclamations : {out}")
    # auto-approval: small cube amounts for everyone, everything for a trusted user, per-user caps
    c, did = linked_player(h)
    trusted = str(next(_ids))
    set_rules({"auto_approve": {"users": [trusted], "rewards": [{"kind": "cubes", "max_amount": 500}]},
               "caps": {"cubes": {"days": 7, "max_total": 1000}},
               "users": {did: {"caps": {"cubes": {"days": 7, "max_total": 100000}}}}})
    out = claim(h, did, "cubes", amount=500)
    h.expect(out.get("ok") and out["auto"] and out["claim"]["status"] == "approved"
             and out["claim"]["decided_by"] == "rules", f"validation automatique : {out}")
    h.expect(any(g["rewardType"] == 3 and g["amount"] == 500 for g in userdata.table(c.uid, "gift")),
             "cadeau automatique absent")
    out = claim(h, did, "cubes", amount=5000)
    h.expect(out.get("ok") and not out["auto"], f"au-dessus du seuil auto / plafond personnel : {out}")
    c2 = h.new_player()
    code = call(h, "/bot/link/start", {"discord_id": trusted})["code"]
    set_comment(c2, code)
    call(h, "/bot/link/check", {"discord_id": trusted})
    out = claim(h, trusted, "character", str(RAIN))
    h.expect(out.get("ok") and out["auto"], f"membre de confiance : {out}")
    h.expect(claim(h, trusted, "cubes", amount=1001).get("error") == "limit", "plafond global ignoré")
    # a broken rules file falls back to the defaults (manual approval)
    with open(RULES, "w", encoding="utf-8") as f:
        f.write("{not json")
    os.utime(RULES, (os.path.getmtime(RULES) + 5,) * 2)
    out = claim(h, trusted, "cubes", amount=1)
    h.expect(out.get("ok") and not out["auto"], f"fichier de règles invalide : {out}")


def test_ids_page(h):
    """/ids (server/idspage.py): every id the bot accepts, game data only, nothing able to break out of the page."""
    import re  # noqa: PLC0415

    import idspage  # noqa: PLC0415
    page = idspage.page().decode("utf-8")
    blob = re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
    h.expect(blob, "données absentes de la page")
    d = json.loads(blob.group(1))
    h.expect(any(r[0] == RAIN and r[1] == "[Dusk Approaching] Rain" for r in d["pcs"]), "Rain absente de /ids")
    h.expect(any(r[0] == 246000011 for r in d["items"]), "objet absent de /ids")
    h.expect(d["equipment"], "équipements absents de /ids")
    h.expect(page.count("</script>") == 2 and "<!--" not in blob.group(1), "le JSON peut sortir de la balise script")
    names = {str(r[0]) for r in d["pcs"]}
    h.expect({k for k, _ in botapi.character_choices()} <= names, "personnage réclamable absent de /ids")


def test_claim_refuses_season_points(h):
    import api  # noqa: PLC0415
    c, did = linked_player(h)
    pt = next(iter(api.season_point_items()))
    out = claim(h, did, "item", str(pt), 100)
    h.expect(out.get("error") == "not_claimable", f"points de saison réclamables : {out}")
    out = claim(h, did, "item", "200000046", 1)  # "Season pt", an item.bin category header
    h.expect(out.get("error") == "not_claimable", f"en-tête de catégorie réclamable : {out}")
    out = claim(h, did, "item", "524101090", 1)  # exchange-shop row "monthly21 04 01" (item.bin sub 8)
    h.expect(out.get("error") == "not_claimable", f"ligne de la boutique d'échange réclamable : {out}")
    items = {k for k, _ in call(h, "/bot/items", method="GET")["items"]}
    h.expect("524101090" not in items, "ligne d'échange proposée par le bot")
    h.expect("200000046" not in items and str(pt) not in items, "objets non réclamables proposés par le bot")


def test_guild_stamp_claimed_once(h):
    """A guild stamp is owned or not: the bot gives 1 whatever the amount asked, and refuses it once owned."""
    import api  # noqa: PLC0415
    stamp = next(iter(api.unique_items()))
    c, did = linked_player(h)
    out = claim(h, did, "item", str(stamp), 11)
    h.expect(out.get("ok") and out["claim"]["amount"] == 1, f"tampon de guilde : {out}")
    decide(h, out["claim"]["id"], True)
    open_gifts(c)
    h.expect(c.item(stamp) == 1, f"tampon reçu : {c.item(stamp)}")
    h.expect(claim(h, did, "item", str(stamp), 1).get("error") == "owned", "tampon réclamé deux fois")


def test_claim_equipment(h):
    """/claim equipment: a weapon in the gift box (rewardType 2), 10 at most per claim, unknown ids refused."""
    from harness import master  # noqa: PLC0415
    eq = 370100460
    h.expect(str(eq) in master.NAMES["equipment"], "équipement de test inconnu")
    c, did = linked_player(h)
    out = claim(h, did, "equipment", str(eq), 2)
    h.expect(out.get("ok") and out["claim"]["reward_type"] == 2, f"réclamation d'équipement : {out}")
    decide(h, out["claim"]["id"], True)
    open_gifts(c)
    h.expect(len([e for e in c.t("equipmentStock") if e["equipmentId"] == eq]) >= 2, "équipements absents")
    h.expect(not claim(h, did, "equipment", str(eq), 11).get("ok"), "11 équipements acceptés")
    h.expect(claim(h, did, "equipment", "999999999", 1).get("error") == "unknown_equipment", "équipement inconnu accepté")
    lst = call(h, "/bot/equipment", method="GET")["equipment"]
    h.expect(any(k == str(eq) for k, _ in lst), "liste d'équipements du bot incomplète")


def test_placeholder_characters_refused(h):
    """The master's unfinished characters (trainee Eugeo / Kirito...) came as broken units (tester report, 2026-10-21)."""
    import botapi  # noqa: PLC0415
    c, did = linked_player(h)
    h.expect("130111012" in botapi.placeholder_pcs() and len(botapi.placeholder_pcs()) < 20,
             f"personnages factices : {sorted(botapi.placeholder_pcs())}")
    out = claim(h, did, "character", "130111012")
    h.expect(not out.get("ok"), f"Eugeo trainee réclamable : {out}")
    h.expect(all(k != "130111012" for k, _ in botapi.character_choices()), "Eugeo trainee proposé par le bot")


def test_model_only_equipment_refused(h):
    """The "(3D)" weapons have no icon nor stats (sun, tester report, 2026-10-25): not offered, not claimable."""
    import botapi  # noqa: PLC0415
    c, did = linked_player(h)
    h.expect("370109160" in botapi.model_only_equipment(), "夜空の剣(3D) doit être reconnu")
    out = claim(h, did, "equipment", "370109160")
    h.expect(out.get("error") == "not_claimable", f"arme 3D réclamable : {out}")
    listed = {k for k, _ in call(h, "/bot/equipment", method="GET")["equipment"]}
    h.expect("370109160" not in listed and "370100000" in listed, "liste d'équipement du bot")


def _transfer_setup(h):
    """The player's account (linked, on its old device) and the fresh account a new device created."""
    old, did = linked_player(h)
    new = h.new_player()
    db.bind_xuid(f"gree-old-{old.uid}", str(old.uid), "app")
    db.bind_xuid(f"gree-new-{new.uid}", str(new.uid), "app")
    return old, new, did


def _pull_as(uid, device_uid):
    import json as _json  # noqa: PLC0415
    from harness import api  # noqa: PLC0415
    headers = {"SAOAL-USER-ID": str(uid), "SAOAL-PLATFORM-USER-ID": f"test-device-{device_uid}"}
    return api.handle_api("POST", "/api/user/pull", headers, _json.dumps({"hash": {}}).encode(), lambda *_: None)[0]


def test_transfer_moves_account(h):
    old, new, did = _transfer_setup(h)
    code = call(h, "/bot/transfer/start", {"discord_id": did})["code"]
    h.expect(code.startswith("TR"), f"code de transfert : {code}")
    set_comment(old, code)
    set_comment(new, code)
    out = call(h, "/bot/transfer/check", {"discord_id": did})
    h.expect(out.get("ok") and out["account"]["uid"] == old.uid and out["other"] == new.uid, f"transfert : {out}")
    h.expect(db.get_device(f"gree-new-{new.uid}")["x_uid"] == str(old.uid), "le nouvel appareil doit ouvrir le compte")
    h.expect(db.get_device(f"gree-old-{old.uid}")["x_uid"] == str(new.uid), "l'ancien appareil reçoit le compte vide")
    h.expect(_pull_as(old.uid, new.uid) == 200, "le compte doit répondre au nouvel appareil")
    h.expect(_pull_as(old.uid, old.uid) == 403, "le compte ne doit plus répondre à l'ancien appareil")
    h.expect(_pull_as(old.uid, 123) == 403, "ni à un appareil inconnu")
    again = call(h, "/bot/transfer/check", {"discord_id": did})
    h.expect(again.get("error") == "no_code", f"le code ne sert qu'une fois : {again}")
    h.expect(call(h, "/bot/transfer/start", {"discord_id": did}).get("error") == "cooldown", "un transfert par semaine")


def test_transfer_needs_both_comments(h):
    """Control of the old account now (its comment) and of the new device (the fresh account's comment)."""
    old, new, did = _transfer_setup(h)
    code = call(h, "/bot/transfer/start", {"discord_id": did})["code"]
    set_comment(new, code)
    h.expect(call(h, "/bot/transfer/check", {"discord_id": did}).get("error") == "old_missing",
             "sans le code dans l'ancien compte, pas de transfert")
    set_comment(new, "")
    set_comment(old, code)
    h.expect(call(h, "/bot/transfer/check", {"discord_id": did}).get("error") == "new_missing",
             "sans le code dans le nouveau compte, pas de transfert")
    h.expect(db.get_device(f"gree-old-{old.uid}")["x_uid"] == str(old.uid), "rien ne doit avoir bougé")


def test_transfer_cannot_take_a_real_account(h):
    """An attacker linked to their own account cannot push a played account off its device, and a Discord user
    without a link cannot start anything."""
    attacker, victim, did = _transfer_setup(h)
    info = dict(victim.row("info"), totalExp=50_000_000)
    victim.push(put={"info": [info]}, trigger="profile")
    code = call(h, "/bot/transfer/start", {"discord_id": did})["code"]
    set_comment(attacker, code)
    set_comment(victim, code)  # even with the code in the victim's comment (tricked victim)
    out = call(h, "/bot/transfer/check", {"discord_id": did})
    h.expect(out.get("error") == "new_not_fresh", f"un compte joué ne doit pas être écrasé : {out}")
    h.expect(db.get_device(f"gree-new-{victim.uid}")["x_uid"] == str(victim.uid), "l'appareil de la victime reste à elle")
    h.expect(call(h, "/bot/transfer/start", {"discord_id": str(next(_ids))}).get("error") == "not_linked",
             "sans compte lié, pas de transfert")
