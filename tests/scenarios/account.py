"""New account, tutorial skips, login bonus."""


def test_new_account(h):
    c = h.new_player(tutorial_done=False)
    h.expect(c.row("info").get("userId") == c.uid, "UserInfo absent après le premier user/pull")
    # Kirito + Eugeo (userdata.FRESH_PCS; the CHANGELOG once said Kirito alone)
    h.expect(130111001 in [p["pcId"] for p in c.t("pc")], "compte neuf : Kirito attendu")
    h.expect(not c.t("tutorial"), "compte neuf : aucun tutoriel attendu")
    # a second pull with up-to-date hashes must not resend anything
    again = c.pull()
    h.expect(not (again.get("table") or {}).get("replace"), "user/pull renvoie des tables alors que les hash sont à jour")


def test_tutorial_skip(h):
    c = h.new_player(tutorial_done=False)
    c.call("tutorial/skip/first")
    c.call("tutorial/skip/second")
    done = {t["tutorialId"] for t in c.t("tutorial")}
    h.expect(100000015 in done, "tutorial/skip/second doit marquer le tutoriel d'équipement (100000015)")
    h.expect(c.t("questProgress"), "les quêtes du tutoriel doivent être terminées")


def test_login_bonus(h):
    c = h.new_player()
    c.call("login")
    h.expect(c.t("loginBonus"), "le bonus de connexion doit remplir UserLoginBonus")
    before = [dict(r) for r in c.t("loginBonus")]
    c.call("login")
    h.expect(c.t("loginBonus") == before, "deux connexions le même jour ne doivent pas avancer le bonus")


def test_push_pull_roundtrip(h):
    c = h.new_player()
    info = dict(c.row("info"), name="Testeur")
    c.push({"info": [info]})
    h.expect(c.server_table("info")[0]["name"] == "Testeur", "user/push n'a pas été enregistré")
    c.hashes["info"] = "stale"
    out = c.pull(["info"])
    h.expect((out.get("table") or {}).get("replace", {}).get("info"), "user/pull doit renvoyer une table au hash périmé")


def test_login_bonus_next_real_day_in_raid_mode(h):
    """Daily login stopped for days: the day was taken from the game clock, which loops over 4 days in raid mode
    (and over ~57 days otherwise) and so went backwards. It is now the real day."""
    import api
    import config
    from harness import userdata
    c = h.new_player()
    config.set_raid_mode(True)
    try:
        c.call("login")
        bonus = api.ACTIVE_LOGIN_BONUSES[0]
        day = next(r for r in c.t("loginBonus") if r["loginBonusId"] == bonus)["dayCount"]
        # the next real day (the game clock may well be earlier than at the previous login)
        userdata.set_counter(c.uid, f"login_day_{bonus}", api.game_day(__import__("time").time()) - 1)
        c.call("login")
        after = next(r for r in c.t("loginBonus") if r["loginBonusId"] == bonus)["dayCount"]
        h.expect(after == day + 1 or (day > 1 and after == 1), f"jour suivant : dayCount {day} -> {after}")
        c.call("login")
        again = next(r for r in c.t("loginBonus") if r["loginBonusId"] == bonus)["dayCount"]
        h.expect(again == after, "deux connexions le même jour réel ne doivent pas avancer le bonus")
    finally:
        config.set_raid_mode(False)


def test_login_bonus_screen_only_once(h):
    """The bonus screen came back at every boot: the rows stayed readState 0 (never sent back by the client)."""
    c = h.new_player()
    out = c.call("login")
    shown = (out.get("table") or {}).get("replace", {}).get("loginBonus") or []
    h.expect(shown and any(r["readState"] == 0 for r in shown), "la connexion du jour doit montrer le bonus")
    c.pull()
    h.expect(all(r["readState"] == 1 for r in c.t("loginBonus")), f"au démarrage suivant : {c.t('loginBonus')}")


def test_settings_survive_reboot(h):
    """Battle x2 speed / "Use Active Link during Auto" reset at every boot: UserSetting changes stay on the device
    until another push carries them, and user/pull sent the server's old row back. The client's copy now wins."""
    from harness import userdata
    c = h.new_player()
    c.pull()
    row = dict(c.row("setting"), battleSpeedUp=True, autoActiveLink=True)
    c.tables["setting"] = [row]
    c.hashes["setting"] = userdata.table_hash("setting", [row])  # changed on the device, not pushed yet
    resp = c.pull()
    h.expect("setting" not in ((resp.get("table") or {}).get("replace") or {}),
             "user/pull ne doit pas écraser les réglages du joueur")
    h.expect(c.row("setting").get("battleSpeedUp") is True, f"réglage x2 après pull : {c.row('setting')}")


def test_home_setting_survives_reboot(h):
    """Home customization / card illustrations came back after a reboot: UserHomeSetting is client-owned too."""
    from harness import userdata
    c = h.new_player()
    c.pull()
    row = dict(c.row("homeSetting"), homeBackgroundId=97000024)
    c.tables["homeSetting"] = [row]
    c.hashes["homeSetting"] = userdata.table_hash("homeSetting", [row])
    resp = c.pull()
    h.expect("homeSetting" not in ((resp.get("table") or {}).get("replace") or {}),
             "user/pull ne doit pas écraser la personnalisation de l'accueil")


def test_banner_read_states_kept(h):
    """Scout banners always "New" after a restart: the client marks them seen in UserLotteryInfo without pushing,
    and user/pull sent the server's empty copy back (Discord 2026-10-31). The client's copy now wins."""
    from harness import userdata
    c = h.new_player()
    c.pull()
    rows = [{"userId": c.uid, "lotteryId": 171207001, "readState": 1, "lastRemindedAt": 0},
            {"userId": c.uid, "lotteryId": 171100173, "readState": 1, "lastRemindedAt": 0}]
    c.tables["lotteryInfo"] = rows
    c.hashes["lotteryInfo"] = userdata.table_hash("lotteryInfo", rows)  # seen on the device, not pushed
    resp = c.pull()
    h.expect("lotteryInfo" not in ((resp.get("table") or {}).get("replace") or {}),
             "user/pull a renvoyé la copie serveur de UserLotteryInfo")
    h.expect(c.t("lotteryInfo") == rows, "bannières vues perdues après pull")


def test_one_time_illustration_bonus_does_not_loop(h):
    """The "Unleash Blading" campaign bonus (40010000191: one day, a home illustration) started a new lap every day:
    the illustration came back at every login (Discord 2026-10-27). One-time campaign bonuses giving something one
    owns once stop after their last day; ordinary bonuses still loop."""
    from harness import userdata
    c = h.new_player()
    c.call("login")
    row = next(r for r in c.t("loginBonus") if r["loginBonusId"] == 40010000191)
    h.expect(row["dayCount"] == 1, f"jour 1 du bonus : {row}")
    # the next real days
    for _ in range(2):
        for b in (40010000191, 40010000002):
            userdata.set_counter(c.uid, f"login_day_{b}", userdata.counter(c.uid, f"login_day_{b}") - 1)
        c.call("login")
    row = next(r for r in c.t("loginBonus") if r["loginBonusId"] == 40010000191)
    h.expect(row["laps"] == 0 and row["dayCount"] == 1, f"bonus unique relancé : {row}")
    weekly = next(r for r in c.t("loginBonus") if r["loginBonusId"] == 40010000002)
    h.expect(weekly["dayCount"] == 3, f"le bonus ordinaire doit avancer : {weekly}")
