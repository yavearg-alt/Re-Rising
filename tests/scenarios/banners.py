"""Admin panel, Banners tab: activate / deactivate a scout banner (its release window, forced open / closed in the
served base.bin, rebuilt without a restart), and the server's own window checks follow."""
import contextlib
import io
import json

import admin
from harness import config, db, master

with contextlib.redirect_stdout(io.StringIO()):
    import assets
    import missions
    from flatmaster import Master
    from saoub_crypto import decrypt, hashed_name

OLD = "release.201912_lottery_edith"  # Eydis 2019 banner: closed at the game clock
CURRENT = "release.202211_lottery_edith"  # open until the end of service


def _served_base_rows(store):
    name = hashed_name("master/divided/base.enc")
    data = store.hashed(name)
    return {r.get(1): r for r in Master(decrypt(data)).rows(15)}, json.loads(decrypt(store.project_manifest_raw))


def test_banner_on_off(h):
    admin.PASSWORD = admin.PASSWORD or "test-password-123"
    with contextlib.redirect_stdout(io.StringIO()):
        store = assets.CURRENT or assets.AssetStore()
    try:
        h.expect(OLD in master.BANNERS and CURRENT in master.BANNERS, "bannières de test absentes")
        h.expect(not missions.is_open(OLD) and missions.is_open(CURRENT), "état initial inattendu")
        _, before = _served_base_rows(store)
        back, msg = admin.do_action("release_window", {"label": OLD, "state": "open"})
        h.expect(back == "banners", f"redirection : {back}")
        back, msg = admin.do_action("release_window", {"label": CURRENT, "state": "closed"})
        rows, after = _served_base_rows(store)
        h.expect(rows[OLD].get(6) == "2023/01/16 05:59:59", f"bannière activée : fin {rows[OLD].get(6)}")
        h.expect(rows[CURRENT].get(6) == "2020/01/01 00:00:01", f"bannière désactivée : fin {rows[CURRENT].get(6)}")
        h.expect(after["version"] != before["version"], "la version servie doit changer (les clients relisent)")
        key = "hashed/" + hashed_name("master/divided/base.enc")[:2] + "/" + hashed_name("master/divided/base.enc")
        h.expect(after["assets"][key]["md5"] != before["assets"][key]["md5"], "md5 de base.bin inchangé")
        h.expect(missions.is_open(OLD) and not missions.is_open(CURRENT), "le serveur doit suivre les réglages")
        for bad in ({"label": "release.nope", "state": "open"}, {"label": OLD, "state": "maybe"}):
            try:
                admin.do_action("release_window", bad)
                h.expect(False, f"réglage invalide accepté : {bad}")
            except ValueError:
                pass
        _, _, _, extra = admin.handle("POST", admin.BASE + "/login", {"X-Forwarded-For": "203.0.113.8"},
                                      f"password={admin.PASSWORD}".encode(), "")
        cookie = {"Cookie": extra.get("Set-Cookie", "").split(";")[0]}
        code, out, _, _ = admin.handle("GET", admin.BASE + "/banners?q=edith", cookie, b"", "")
        h.expect(code == 200 and b"Banners" in out and b"forced" in out, f"page Bannières (HTTP {code})")
        # banner pictures (decrypted from the game files, behind the login)
        pic = next(b[0] for b in master.BANNERS[CURRENT] if str(b[0]) in master.BANNER_IMAGES)
        code, data, ctype, _ = admin.handle("GET", admin.BASE + f"/banner_img/{pic}", cookie, b"", "")
        h.expect(code == 200 and ctype == "image/png" and data[:4] == b"\x89PNG", f"image de bannière : HTTP {code}")
        h.expect(f"/banner_img/{pic}".encode() in admin.handle("GET", admin.BASE + "/banners?q=edith", cookie,
                                                              b"", "")[1], "miniature absente de la page")
        code, _, _, _ = admin.handle("GET", admin.BASE + f"/banner_img/{pic}", {}, b"", "")
        h.expect(code == 303, f"image accessible sans connexion (HTTP {code})")
        admin.do_action("release_window", {"label": OLD, "state": "auto"})
        admin.do_action("release_window", {"label": CURRENT, "state": "auto"})
        rows, _ = _served_base_rows(store)
        h.expect(rows[CURRENT].get(6) == "2023/01/16 05:59:59" and not missions.is_open(OLD), "retour en auto")
    finally:
        db.set_setting("release_windows", {})
        if assets.CURRENT is not None:
            assets.CURRENT.refresh_windows()
    h.expect(config.EXTEND_WINDOWS, "Season 9 doit rester prolongée")
