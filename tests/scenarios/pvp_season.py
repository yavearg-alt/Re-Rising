"""Ordinal Battle (PvP) between real accounts, season pass. Bodies copied from real client requests."""
from harness import db, master, userdata

PVP = 603100000038


def test_pvp_match_start_clear(h):
    rival = h.new_player()
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP, "reloadFlag": False})
    opponents = c.t("pvpNpcParty")
    h.expect(len(opponents) == 3, f"pvp/matching : {len(opponents)} adversaires, 3 attendus")
    real = set(db.user_ids())
    h.expect(any(o["enemyUserId"] in real for o in opponents), "les adversaires doivent être d'abord de vrais comptes")
    h.expect(all(o["enemyUserId"] != c.uid for o in opponents), "on ne doit pas s'affronter soi-même")
    bp = c.row("pvp").get("bp")
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 1, "npcPartyIndex": 1, "pvpId": PVP})
    h.expect(c.row("pvp").get("bp") == bp - 1, f"BP après pvp/start : {c.row('pvp').get('bp')}, {bp - 1} attendus")
    c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": True})
    h.expect(c.row("pvp").get("point", 0) > 0, "une victoire doit rapporter des points")
    h.expect(c.row("pvpResult"), "pvpResult manquant après pvp/clear")
    # 3 = battle over: the only status for which the client opens the result screen (1 sent players home)
    h.expect(c.row("pvpStatus").get("status") == 3, f"pvpStatus après pvp/clear : {c.row('pvpStatus')}, status 3 attendu")
    c.call("login")
    h.expect(c.server_table("pvpStatus")[0].get("status") == 1, "après le login suivant, pvpStatus doit revenir au repos (1)")


def test_pvp_loss(h):
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP, "reloadFlag": False})
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 1, "npcPartyIndex": 1, "pvpId": PVP})
    c.call("pvp/clear", {"elapsedTurn": 0, "pvpId": PVP, "winFlag": False})
    h.expect(c.row("pvp").get("consecutiveWinCount", 0) == 0, "une défaite doit remettre la série à 0")


def test_season_reward(h):
    c = h.new_player()
    rid = 5022209378  # real request
    c.call("season-reward/receive", {"seasonRewardIds": [rid]})
    h.expect(any(r.get("seasonRewardId") == rid for r in c.t("seasonReward")), "palier de saison non marqué reçu")
    before = [dict(r) for r in c.t("item")]
    c.call("season-reward/receive", {"seasonRewardIds": [rid]})
    h.expect(c.t("item") == before, "un palier déjà reçu ne doit pas redonner sa récompense")


def test_season_purchases(h):
    c = h.new_player()
    userdata.add_gems(c.uid, 10000)
    boost_id = next(iter(master.SEASON["boosts"]))
    c.call("season-point-boost/buy", {"seasonPointBoostId": int(boost_id)})
    h.expect(c.t("seasonPointBoost"), "seasonPointBoost non créé")
    pass_id = next(iter(master.SEASON["passes"]))
    c.call("premium-pass/buy", {"premiumPassId": int(pass_id)})
    h.expect(c.t("premiumPass"), "premiumPass non créé")
    paid = next((k for k, r in master.SEASON["rewards"].items() if r["cost"]), None)
    if paid:
        c.call("season-reward/unlock", {"seasonRewardId": int(paid), "cost": master.SEASON["rewards"][paid]["cost"]})


def test_pvp_opponent_difficulties(h):
    """Every opponent had rank 0, so all three showed x0.8 points (Discord): one is now a rank above (x2), one the
    same rank (x1), one below (x0.8), and a win pays accordingly."""
    import api
    h.new_player()
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP, "reloadFlag": False})
    mine = api.pvp_rank(PVP, c.row("pvp").get("point", 0))
    ranks = sorted((o["rank"] for o in c.t("pvpNpcParty")), reverse=True)
    h.expect(ranks == [2, 1, 0], f"difficultés adverses {ranks}, [2, 1, 0] attendu")
    hard = next(o for o in c.t("pvpNpcParty") if o["rank"] == api.PVP_HARDER)
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 1, "npcPartyIndex": hard["partyIndex"], "pvpId": PVP})
    c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": True})
    base = master.PVP_RANK_ROWS[f"{PVP}:{mine}"]["point"]  # x1.10 (8 turns) x2.00 (rank above)
    h.expect(c.row("pvp").get("point") == base * 11000 * 20000 // 10 ** 8,
             f"victoire x2 : {c.row('pvp').get('point')} points")


def test_pvp_bp_multiplies_points(h):
    """5 BP on a battle gave the same points as 1 BP (Discord)."""
    import api
    h.new_player()
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 5, "pvpId": PVP, "reloadFlag": False})
    mine = api.pvp_rank(PVP, 0)
    same = next(o for o in c.t("pvpNpcParty") if o["rank"] == api.PVP_SAME)
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 5, "npcPartyIndex": same["partyIndex"], "pvpId": PVP})
    c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": True})
    base = master.PVP_RANK_ROWS[f"{PVP}:{mine}"]["point"] * 11000 // 10000
    h.expect(c.row("pvp").get("point") == base * 5, f"victoire avec 5 BP : {c.row('pvp').get('point')}")


def test_pvp_result_screen(h):
    """The result screen showed Turn Bonus x0.00, Opponent Bonus x0.00, BP Bonus x0 and an empty Reward box
    (Discord, 2026-10-17): pvpResult must carry the ratios (x10000), pvpStatus.consumingBp must stay, and the win
    / BP bonus rewards must be listed and granted."""
    import api
    h.new_player()
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 5, "pvpId": PVP, "reloadFlag": False})
    mine = api.pvp_rank(PVP, 0)
    row = master.PVP_RANK_ROWS[f"{PVP}:{mine}"]
    hard = next(o for o in c.t("pvpNpcParty") if o["rank"] == api.PVP_HARDER)
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 5, "npcPartyIndex": hard["partyIndex"], "pvpId": PVP})
    medal = master.REWARDS[str(row["win"])]["items"][0]["itemId"]
    before = c.item(medal)
    c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": True})
    res = c.row("pvpResult")
    h.expect(res.get("turnBonus") == 11000 and res.get("enemyBonus") == 20000 and res.get("rankPoint") == row["point"],
             f"pvpResult bonus : {res}")
    h.expect(c.row("pvpStatus").get("consumingBp") == 5, f"pvpStatus.consumingBp : {c.row('pvpStatus')}, 5 attendu")
    h.expect(c.row("pvpStatus").get("npcPartyIndex") == hard["partyIndex"], "l'adversaire battu doit rester affiché")
    h.expect(res.get("rewardGroupIds") == [row["win"]] and res.get("bpBonusRewardGroupIds") == [row["bp"]] * 4,
             f"récompenses : {res.get('rewardGroupIds')} / {res.get('bpBonusRewardGroupIds')}")
    # + the rank reached for the first time (572 points: rank 2 or more)
    h.expect(res.get("rankUp") and res.get("firstRewardGroupIds"), f"montée de rang : {res}")
    groups = [row["win"]] + [row["bp"]] * 4 + res["firstRewardGroupIds"]
    gain = sum(it["amount"] for g in groups for it in master.REWARDS[str(g)]["items"]
               if it["itemId"] == medal)
    h.expect(c.item(medal) == before + gain, f"matériau Ordinal : {before} -> {c.item(medal)}, +{gain} attendu")
    h.expect(res.get("totalPoint") == (row["point"] * 11000 * 20000 // 10 ** 8) * 5, f"points : {res.get('totalPoint')}")


def test_pvp_difficulty_at_higher_rank(h):
    """UserPvpNpcParty.rank is the difficulty (2 harder x2, 1 same x1, 0 easier x0.8), not an Ordinal rank: at
    rank D (4740 points) all three opponents showed x1 (tester report, 2026-10-17)."""
    import api
    h.new_player()
    c = h.new_player()
    rows, me = api.pvp_row(c.uid, PVP)
    me["point"] = me["maxReachingPoint"] = 4740
    userdata.set_tables(c.uid, {"pvp": rows})
    c.pull()
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP, "reloadFlag": False})
    ranks = sorted((o["rank"] for o in c.t("pvpNpcParty")), reverse=True)
    h.expect(ranks == [2, 1, 0], f"difficultés au rang D : {ranks}, [2, 1, 0] attendu")
    pts = {o["rank"]: o["point"] for o in c.t("pvpNpcParty")}
    mine = api.pvp_rank(PVP, 4740)
    h.expect(api.pvp_rank(PVP, pts[2]) > mine and api.pvp_rank(PVP, pts[0]) < mine, f"points adverses {pts}")
    # changing opponents costs the BP the client announces (1)
    bp = c.row("pvp")["bp"]
    c.call("pvp/matching", {"expectedConsumingBp": 1, "pvpId": PVP, "reloadFlag": True})
    h.expect(c.row("pvp")["bp"] == bp - 1, f"BP après changement d'adversaires : {bp} -> {c.row('pvp')['bp']}")


def test_season_level_unlock(h):
    """"Unlock Season Level" 1 -> 100 for 2459 cubes gave nothing (tester report): it must buy the season points up to
    the target tier (season.bin sub 1 field 4) in the season's point item, so the tiers can be received."""
    c = h.new_player()
    userdata.add_gems(c.uid, 5000)
    rid = "5022209100"  # Season 9, level 100
    tier = master.SEASON["rewards"][rid]
    item = master.SEASON["pointItems"][str(tier["season"])]
    h.expect(item == 405220900 and tier["points"] > 0, f"saison 9 : objet {item}, points {tier['points']}")
    c.call("season-reward/unlock", {"seasonRewardId": int(rid), "cost": 2459})
    h.expect(c.item(item) == tier["points"], f"points de saison après déblocage : {c.item(item)}, {tier['points']} attendus")
    h.expect(userdata.gems(c.uid) == 5000 - 2459, f"cubes : {userdata.gems(c.uid)}")


def test_season_points_earned(h):
    """Season points were never earned by playing: a quest clear gives its AP cost, an Ordinal win 20 per BP, x2
    with a bought point boost; questResult / pvpResult show them."""
    import api
    quest = {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001,
             "supporterPcId": 0, "supporterPcSlotId": 0, "supporterUserId": 0}
    h.new_player()
    c = h.new_player()
    item = master.SEASON["pointItems"][api.current_season()]
    ap = master.QUEST_LEVELS["8011000001:1"]["ap"]
    c.call("quest/start", quest)
    c.call("quest/clear")
    res = c.row("questResult")
    h.expect(res.get("seasonPoint") == ap and res.get("seasonPointItemId") == item and c.item(item) == ap,
             f"points de saison d'une quête ({ap} AP) : {res.get('seasonPoint')}, objet {c.item(item)}")
    # x2 with the point boost
    userdata.add_gems(c.uid, 1000)
    c.call("season-point-boost/buy", {"seasonPointBoostId": int(next(iter(master.SEASON["boosts"])))})
    c.call("quest/start", quest)
    c.call("quest/clear")
    h.expect(c.row("questResult").get("seasonPoint") == 2 * ap and c.row("questResult").get("seasonPointBoostRatio") == 20000,
             f"avec le boost : {c.row('questResult')}")
    # Ordinal win with 2 BP
    before = c.item(item)
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP, "reloadFlag": False})
    c.call("pvp/start", {"activePartyId": 1, "consumingBp": 2, "npcPartyIndex": 1, "pvpId": PVP})
    c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": True})
    want = 2 * api.SEASON_POINTS_PER_BP * 2
    h.expect(c.row("pvpResult").get("seasonPoint") == want and c.item(item) == before + want,
             f"points de saison Ordinal : {c.row('pvpResult').get('seasonPoint')}, {want} attendus")


def test_pvp_last_reward_receive(h):
    """An ended season's row (Vol.37) must be marked received, or "The event has ended" comes back each time."""
    c = h.new_player()
    c.call("pvp/matching", {"expectedConsumingBp": 0, "pvpId": PVP - 1, "reloadFlag": False})
    h.expect(not c.row("pvp").get("rewardReceivedAt"), "une ligne de saison neuve n'est pas encore reçue")
    c.call("pvp/last-reward/receive", {"userId": c.uid, "pvpId": PVP - 1})
    row = next((r for r in c.t("pvp") if r.get("pvpId") == PVP - 1), {})
    h.expect(row.get("rewardReceivedAt"), f"pvp/last-reward/receive doit remplir rewardReceivedAt : {row}")


def test_season_extra_tier_repeats(h):
    """Tier 101 ("extra", every 500 points after level 100) can be received again, up to season.bin field 6 times."""
    c = h.new_player()
    rid = 5022209101
    limit = master.SEASON["rewards"][str(rid)]["cost"]
    for _ in range(2):
        c.call("season-reward/receive", {"seasonRewardIds": [rid]})
    row = next((r for r in c.t("seasonReward") if r.get("seasonRewardId") == rid), {})
    h.expect(limit > 1 and row.get("acquiredCount") == 2, f"palier extra reçu 2 fois : {row} (limite {limit})")


def test_pvp_consecutive_win_bonus(h):
    """Consecutive-win bonus = the rank's points (pvp.bin sub 1 field 6: 30 at rank 1 ... 160 at rank 9) from the
    2nd win in a row (field 5); a loss starts the streak again. It was a made-up min(streak, 5) x 2 ("160 -> 10 ->
    4", Discord 2026-10-28)."""
    import api
    h.new_player()
    c = h.new_player()

    def fight(win):
        c.call("pvp/matching", {"expectedConsumingBp": 1, "pvpId": PVP, "reloadFlag": True})
        same = next(o for o in c.t("pvpNpcParty") if o["rank"] == api.PVP_SAME)
        rank = api.pvp_rank(PVP, c.row("pvp").get("point", 0))
        c.call("pvp/start", {"activePartyId": 1, "consumingBp": 1, "npcPartyIndex": same["partyIndex"], "pvpId": PVP})
        c.call("pvp/clear", {"elapsedTurn": 8, "pvpId": PVP, "winFlag": win})
        c.call("login")  # result seen: back to idle
        return c.row("pvpResult").get("consecutiveWinBonus"), master.PVP_RANK_ROWS[f"{PVP}:{rank}"]

    first, _ = fight(True)
    h.expect(first == 0, f"1re victoire : bonus {first}, 0 attendu")
    second, row = fight(True)
    h.expect(second == row["streak"] and row["streak"] > 0, f"2e victoire d'affilée : bonus {second}, {row['streak']}")
    fight(False)
    after_loss, _ = fight(True)
    h.expect(after_loss == 0, f"après une défaite : bonus {after_loss}, 0 attendu")


def test_event_season_points(h):
    """An event with its own point season ("Unleash Blading" 601101000202 -> point item 405220906) never gave its
    points: eventSeasonPoint stayed 0 (Discord 2026-10-27)."""
    c = h.new_player()
    quest = {"boostItemIds": [], "eventId": 601101000202, "level": 1, "partyId": 1, "questId": 8013993007,
             "supporterPcId": 0, "supporterPcSlotId": 0, "supporterUserId": 0}
    before = c.item(405220906)
    c.call("quest/start", quest)
    c.call("quest/clear")
    res = c.row("questResult")
    h.expect(res.get("eventSeasonPoint", 0) > 0 and res.get("eventSeasonPointItemId") == 405220906,
             f"points de saison d'événement : {res.get('eventSeasonPoint')} / {res.get('eventSeasonPointItemId')}")
    h.expect(c.item(405220906) == before + res["eventSeasonPoint"], "points non ajoutés à l'objet de saison")
