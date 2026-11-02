"""Account protection (security audit 2026-10-10): an account only answers the device it is bound to."""
import json

from harness import api


def _pull(uid, headers):
    status, out, *_ = api.handle_api("POST", "/api/user/pull", dict(headers, **{"SAOAL-USER-ID": str(uid)}),
                                     json.dumps({"hash": {}}).encode(), lambda *_: None)
    return status


def test_account_bound_to_its_device(h):
    c = h.new_player()
    c.call("user/pull", {"hash": {}})  # binds the account to the player's device
    h.expect(_pull(c.uid, {"SAOAL-PLATFORM-USER-ID": f"test-device-{c.uid}"}) == 200, "le joueur doit garder l'accès")
    h.expect(_pull(c.uid, {"SAOAL-PLATFORM-USER-ID": "attacker-device"}) == 403,
             "un autre appareil ne doit pas pouvoir agir sur le compte")
    h.expect(_pull(c.uid, {}) == 403, "une requête sans identifiant d'appareil doit être refusée")


def test_dev_token(h):
    c = h.new_player()
    c.call("user/pull", {"hash": {}})
    old = api.DEV_TOKEN
    try:
        api.DEV_TOKEN = "secret-dev-token"
        h.expect(_pull(c.uid, {"SAOAL-DEV-TOKEN": "secret-dev-token"}) == 200, "le jeton développeur doit passer")
        h.expect(_pull(c.uid, {"SAOAL-DEV-TOKEN": "wrong"}) == 403, "un mauvais jeton doit être refusé")
        api.DEV_TOKEN = ""
        h.expect(_pull(c.uid, {"SAOAL-DEV-TOKEN": ""}) == 403, "sans jeton configuré, rien ne passe")
    finally:
        api.DEV_TOKEN = old


def test_account_size_cap(h):
    """A client can't fill the database: a push that makes the account bigger than the cap is refused."""
    c = h.new_player()
    c.call("user/pull", {"hash": {}})
    junk = [{"userId": c.uid, "itemId": 900000000 + i, "amount": 1, "pad": "x" * 2000} for i in range(5000)]
    out = c.call("user/push", {"table": [{"put": {"junkTable": junk}}]}, expect_code=None)
    h.expect(out.get("code") == 413, f"push géant : code {out.get('code')}, 413 attendu")
    h.expect("junkTable" not in api.userdata.load(c.uid)["tables"], "le push refusé ne doit rien enregistrer")


def test_reinstall_keeps_account(h):
    """A reinstall changes SAOAL-PLATFORM-USER-ID but not SAOAL-AD-ID: the player keeps the account (seen on a
    player account, refused with 403 before); a stranger with neither id is still refused."""
    c = h.new_player()
    api.db.clear_platform(c.uid)  # new_player() already bound the harness device
    first = {"SAOAL-PLATFORM-USER-ID": "phone-before", "SAOAL-AD-ID": "428e0c94-aaaa-bbbb-cccc-000000000001"}
    h.expect(_pull(c.uid, first) == 200, "premier appareil")
    reinstalled = dict(first, **{"SAOAL-PLATFORM-USER-ID": "phone-after"})
    h.expect(_pull(c.uid, reinstalled) == 200, "après réinstallation (même AD-ID), le compte doit rester accessible")
    h.expect(_pull(c.uid, {"SAOAL-PLATFORM-USER-ID": "phone-after"}) == 200, "la liaison suit le nouvel identifiant")
    h.expect(_pull(c.uid, {"SAOAL-PLATFORM-USER-ID": "x", "SAOAL-AD-ID": "11111111-2222"}) == 403, "un inconnu reste refusé")
    zeros = {"SAOAL-PLATFORM-USER-ID": "x", "SAOAL-AD-ID": "00000000-0000-0000-0000-000000000000"}
    h.expect(_pull(c.uid, zeros) == 403, "un AD-ID tout à zéro (suivi désactivé) ne compte pas")
