"""Admin panel (server/admin.py), driven through its HTTP handler, and the effects seen by a game client."""
import re
import urllib.parse

import admin
from harness import db, userdata

admin.PASSWORD = "test-password-123"
B = admin.BASE


class Browser:
    def __init__(self, h):
        self.h, self.cookie, self.csrf = h, "", ""

    def req(self, method, path, form=None, expect=200):
        body = urllib.parse.urlencode(form or {}).encode()
        headers = {"Cookie": self.cookie, "X-Forwarded-For": "203.0.113.7"} if self.cookie else {"X-Forwarded-For": "203.0.113.7"}
        code, out, ctype, extra = admin.handle(method, B + path, headers, body, "10.0.0.1")
        if "Set-Cookie" in extra:
            self.cookie = extra["Set-Cookie"].split(";")[0]
        self.h.expect(code == expect, f"{method} {path} : HTTP {code}, {expect} attendu\n{out[:600]!r}")
        text = out.decode("utf-8", "replace")
        m = re.search(r'name="csrf" value="([^"]+)"', text)
        if m:
            self.csrf = m.group(1)
        return code, text, extra

    def action(self, _action, **form):
        code, _, extra = self.req("POST", f"/action/{_action}", dict(form, csrf=self.csrf), expect=303)
        loc = urllib.parse.unquote(extra.get("Location", ""))
        self.h.expect("err=" not in loc, f"action {_action} refusée : {loc}")
        return loc


def login(h):
    b = Browser(h)
    b.req("POST", "/login", {"password": admin.PASSWORD}, expect=303)
    b.req("GET", "/")
    return b


def test_login_and_protection(h):
    b = Browser(h)
    b.req("GET", "/players", expect=303)  # not logged in -> login page
    b.req("POST", "/login", {"password": "nope"})
    for _ in range(5):
        admin.handle("POST", B + "/login", {"X-Forwarded-For": "198.51.100.9"}, b"password=bad", "")
    code, out, _, _ = admin.handle("POST", B + "/login", {"X-Forwarded-For": "198.51.100.9"},
                                   f"password={admin.PASSWORD}".encode(), "")
    h.expect(b"Too many attempts" in out, "après 5 échecs, la connexion doit être bloquée")
    b = login(h)
    code, out, _, _ = admin.handle("POST", B + "/action/gems", {"Cookie": b.cookie}, b"uid=1&amount=5&csrf=wrong", "")
    h.expect(code == 403, f"formulaire sans bon jeton CSRF accepté (HTTP {code})")
    for path in ("/", "/players", "/gifts", "/announcements", "/server", "/names?kind=items&q=exp"):
        b.req("GET", path)


def test_player_edits_seen_by_client(h):
    b = login(h)
    c = h.new_player()
    b.req("GET", f"/player/{c.uid}")
    b.action("gems", uid=c.uid, amount=500)
    h.expect(userdata.gems(c.uid) == 500, f"cubes : {userdata.gems(c.uid)}")
    b.action("item", uid=c.uid, id="246000011 EXP Water", amount=7)
    b.action("pc", uid=c.uid, id=130114001)
    b.action("equipment", uid=c.uid, id=370100460, amount=2)
    b.action("rename", uid=c.uid, name="Admin-Test")
    c.pull()
    h.expect(c.item(246000011) >= 7, "objet donné absent côté client")
    h.expect(any(p["pcId"] == 130114001 for p in c.t("pc")), "personnage donné absent côté client")
    stock = [e for e in c.t("equipmentStock") if e["equipmentId"] == 370100460]
    h.expect(len(stock) >= 2, "équipements donnés absents côté client")
    h.expect(c.row("info").get("lastEquipmentStockId", 0) >= max(e["id"] for e in c.t("equipmentStock")),
             "lastEquipmentStockId doit couvrir les équipements donnés (sinon le jeu réutilise un numéro)")
    h.expect(c.row("info").get("name") == "Admin-Test", "renommage non vu par le client")


def test_gifts_one_and_all(h):
    b = login(h)
    c1, c2 = h.new_player(), h.new_player()
    b.action("gift", uid=c1.uid, type=1, id=246000011, amount=3, reason=400100002001)
    b.action("gift", uid="all", type=3, id="", amount=50, reason=400100002001)
    for c in (c1, c2):
        c.pull()
        new = [g for g in c.t("gift") if g["state"] == 0]
        h.expect(any(g["rewardType"] == 3 and g["amount"] == 50 for g in new), "cadeau « à tous » non reçu")
        gems = userdata.gems(c.uid)
        c.call("gift/receive", {"giftIds": [g["id"] for g in new]})
        h.expect(userdata.gems(c.uid) == gems + 50, "ouvrir le cadeau doit créditer les cubes")
    h.expect(c1.item(246000011) >= 3, "cadeau d'objet non crédité à l'ouverture")


def test_snapshot_restore_retire_heal(h):
    b = login(h)
    c = h.new_player()
    b.action("snapshot", uid=c.uid, label="propre")
    b.action("gems", uid=c.uid, amount=999)
    snap = next(s for s in db.list_snapshots(c.uid) if s["label"] == "propre")
    b.action("restore", uid=c.uid, snapshot=snap["id"])
    h.expect(userdata.gems(c.uid) == 0, f"restauration : {userdata.gems(c.uid)} cubes, 0 attendu")
    h.expect(any("before restoring" in s["label"] for s in db.list_snapshots(c.uid)),
             "la restauration doit sauvegarder l'état d'avant")
    c.call("quest/start", {"questId": 8011000001, "level": 1, "partyId": 1, "eventId": 0, "boostItemIds": [],
                           "supporterUserId": 0, "supporterPcId": 0, "supporterPcSlotId": 0})
    b.req("GET", f"/player/{c.uid}")
    b.action("retire", uid=c.uid)
    h.expect(userdata.table(c.uid, "questStatus")[0]["type"] != 1, "« sortir de la quête » n'a pas abandonné")
    kirito = next(p for p in userdata.table(c.uid, "pc") if p["pcId"] == 130111001)
    c.push({"pc": [dict(kirito, hp=1)]})
    b.action("heal", uid=c.uid)
    h.expect(next(p for p in userdata.table(c.uid, "pc") if p["pcId"] == 130111001)["hp"] > 1, "soin non appliqué")


def test_announcements_and_maintenance(h):
    b = login(h)
    b.action("announcement_save", id="", title="Bienvenue <script>", body="Ligne 1\n**gras** https://example.org",
             image_url="", visible="on")
    ann = db.list_announcements()[0]
    page = admin.game_page("/announcement").decode()
    h.expect("Bienvenue &lt;script&gt;" in page, "l'annonce doit apparaître (échappée) dans la liste du jeu")
    detail = admin.game_page(f"/announcement/{ann['id']}").decode()
    h.expect("<b>gras</b>" in detail and "<br>" in detail and "<script>" not in detail, "rendu de l'annonce incorrect")
    h.expect("Bienvenue" in admin.game_page("/announcement/171100162").decode(), "id inconnu : la liste attendue")
    b.action("announcement_save", id=ann["id"], title="Masquée", body="", image_url="")  # no "visible"
    h.expect("Masquée" not in admin.game_page("/announcement").decode(), "annonce masquée encore visible")
    b.action("announcement_delete", id=ann["id"])
    h.expect(admin.maintenance_json()["valid"] is False, "maintenance active par défaut")
    b.action("maintenance", enabled="on", message="Mise à jour")
    m = admin.maintenance_json()
    h.expect(m["valid"] and m["detail"] == "Mise à jour", f"maintenance : {m}")
    b.action("maintenance", message="")
    h.expect(admin.maintenance_json()["valid"] is False, "maintenance non désactivée")


def test_server_settings_and_devices(h):
    import config
    b = login(h)
    before = config.LOTTERY_STEP_BONUS
    b.action("lottery_bonus", on="0")
    h.expect(config.LOTTERY_STEP_BONUS is False and db.get_setting("lottery_step_bonus") is False, "paliers non désactivés")
    b.action("lottery_bonus", on="1" if before else "0")
    c = h.new_player()
    db.create_device("test-device-uuid", "dev")
    b.action("link", uid=c.uid, uuid="test-device-uuid")
    h.expect(db.get_device("test-device-uuid")["x_uid"] == str(c.uid), "appareil non lié")
    leader = h.new_player()
    leader.call("guild/agreement")
    leader.call("guild/form", {"name": "Adm", "comment": "", "atmosphereType": 1, "iconId": 0, "isRequiredApproval": 0})
    gid = leader.row("guild")["guildId"]
    b.req("GET", "/server")
    b.action("guild_delete", id=gid)
    h.expect(db.get_guild(gid) is None, "guilde non dissoute")


def test_add_gift_numeric_fields(h):
    """api.add_gift stores numbers even when given text (admin.DEFAULT_REASON is a string): a text reasonId made the
    client fail on the gift box ("error 200")."""
    from harness import api
    import admin as admin_mod
    c = h.new_player()
    api.add_gift(c.uid, "1", "177000214", "4", "1", admin_mod.DEFAULT_REASON)
    g = api.userdata.table(c.uid, "gift")[-1]
    bad = {k: v for k, v in g.items() if isinstance(v, str)}
    h.expect(not bad, f"champs texte dans le cadeau : {bad}")


def test_gift_wrong_type_refused_and_deletable(h):
    """A character id sent as an item gift (the form's default type) crashed the gift box: refused now, and an
    unopened gift can be deleted from the player page."""
    from harness import api
    c = h.new_player()
    try:
        admin.do_action("gift", {"uid": str(c.uid), "type": "1", "id": "130124027", "amount": "1", "reason": "0"})
        h.expect(False, "un id de personnage envoyé comme objet doit être refusé")
    except ValueError:
        pass
    admin.do_action("gift", {"uid": str(c.uid), "type": "5", "id": "130124027", "amount": "1", "reason": "0"})
    # a bad gift made before the check (add_gift refuses it now): written straight into the table
    gifts = list(userdata.table(c.uid, "gift"))
    gifts.append(dict(gifts[-1], id=gifts[-1]["id"] + 1, rewardType=1, itemId=130124027))
    userdata.set_tables(c.uid, {"gift": gifts})
    bad = userdata.table(c.uid, "gift")[-1]
    admin.do_action("gift_delete", {"uid": str(c.uid), "gift": str(bad["id"])})
    left = [g for g in userdata.table(c.uid, "gift") if g.get("itemId") == 130124027]
    h.expect([g["rewardType"] for g in left] == [5], f"après suppression : {left}")
