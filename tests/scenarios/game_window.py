"""Game clock window from the admin panel (issue #29): set from the Server page, persisted, restored at boot; the raid
window still wins while raid mode is on."""
from datetime import datetime

import admin
from harness import config, db


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def test_game_window_from_panel(h):
    admin.PASSWORD = admin.PASSWORD or "test-password-123"
    try:
        back, msg = admin.do_action("game_window", {"start": "2022-12-01T06:00", "end": "2022-12-08T05:00"})
        h.expect(back == "server", f"redirection : {back}")
        spec = db.get_setting("game_window")
        h.expect(spec == "2022-12-01T06:00:00+09:00/2022-12-08T05:00:00+09:00", f"réglage enregistré : {spec}")
        a, b = (_ts(x) for x in spec.split("/"))
        h.expect(a <= config.now() <= b, f"heure du jeu hors de la fenêtre : {config.now()}")
        # restored at boot
        config.set_game_window(config.DEFAULT_GAME_WINDOW)
        admin.apply_settings()
        h.expect(config.GAME_WINDOW == spec, f"fenêtre non restaurée au démarrage : {config.GAME_WINDOW}")
        # raid mode first
        config.set_raid_mode(True)
        ra, rb = (_ts(x) for x in config.RAID_WINDOW.split("/"))
        h.expect(ra <= config.now() <= rb, "le mode raid doit rester prioritaire")
        config.set_raid_mode(False)
        # refused values leave the clock alone
        for bad in ({"start": "2022-12-08T05:00", "end": "2022-12-01T06:00"}, {"start": "", "end": "2022-12-01T06:00"},
                    {"start": "pas une date", "end": "2022-12-01T06:00"}):
            try:
                admin.do_action("game_window", bad)
                h.expect(False, f"fenêtre invalide acceptée : {bad}")
            except ValueError:
                pass
        h.expect(config.GAME_WINDOW == spec, "une valeur refusée a changé l'horloge")
        # the Server page shows the card
        code, out, _, _ = admin.handle("GET", admin.BASE + "/server", {"Cookie": _login()}, b"", "")
        h.expect(code == 200 and b"Game clock" in out, f"carte absente de la page Serveur (HTTP {code})")
        admin.do_action("game_window", {"default": "1"})
        h.expect(config.GAME_WINDOW == config.DEFAULT_GAME_WINDOW and db.get_setting("game_window") is None,
                 "retour à la fenêtre par défaut")
    finally:
        config.set_raid_mode(False)
        config.set_game_window(config.DEFAULT_GAME_WINDOW)
        db.set_setting("game_window", None)


def _login() -> str:
    _, _, _, extra = admin.handle("POST", admin.BASE + "/login", {"X-Forwarded-For": "203.0.113.9"},
                                  f"password={admin.PASSWORD}".encode(), "")
    return extra.get("Set-Cookie", "").split(";")[0]
