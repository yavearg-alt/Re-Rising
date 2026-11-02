"""Season pass "Bundle Set": its reward group (season points + themes) goes straight to the inventory, once per
purchase, also for bundles bought before the fix (at login); season points never go to the gift box."""
import api
from harness import master, userdata

BUNDLE, PLAIN = 5040210902, 5040210901  # Season 3 bundle (500 cubes) / plain pass (300)


def _group_items(pid):
    return master.REWARDS[str(master.SEASON["passes"][str(pid)]["bundle"])]["items"]


def test_bundle_content_given(h):
    h.expect(not master.SEASON["passes"][str(PLAIN)]["bundle"], "le pass simple ne doit rien donner en plus")
    items = _group_items(BUNDLE)
    points = next(i for i in items if i["itemId"] in api.season_point_items())
    c = h.new_player()
    userdata.add_gems(c.uid, 2000)
    gifts = len(userdata.table(c.uid, "gift"))
    c.call("premium-pass/buy", {"premiumPassId": BUNDLE})
    h.expect(c.item(points["itemId"]) == points["amount"], f"points de saison du bundle : {c.item(points['itemId'])}")
    for it in items:
        h.expect(c.item(it["itemId"]) >= it["amount"], f"contenu du bundle absent : {it}")
    h.expect(len(userdata.table(c.uid, "gift")) == gifts, "le bundle est passé par la boîte à cadeaux")
    h.expect(userdata.gems(c.uid) == 1500, f"prix du bundle : {userdata.gems(c.uid)}")
    c.call("login")
    h.expect(c.item(points["itemId"]) == points["amount"], "bundle redonné à la connexion")
    c.call("premium-pass/buy", {"premiumPassId": PLAIN})
    h.expect(c.item(points["itemId"]) == points["amount"], "le pass simple a donné des points")


def test_bundle_bought_before_fix(h):
    """An account that bought the bundle before: the content comes at the next login, once."""
    items = _group_items(BUNDLE)
    points = next(i for i in items if i["itemId"] in api.season_point_items())
    c = h.new_player()
    userdata.set_tables(c.uid, {"premiumPass": [{"userId": c.uid, "premiumPassId": BUNDLE, "purchasedCount": 1,
                                                 "purchasedAt": 1}]})
    c.call("login")
    c.pull()
    h.expect(c.item(points["itemId"]) == points["amount"], f"rattrapage à la connexion : {c.item(points['itemId'])}")
    c.call("login")
    c.pull()
    h.expect(c.item(points["itemId"]) == points["amount"], "rattrapage donné deux fois")


def test_no_season_points_in_gift_box(h):
    c = h.new_player()
    pt = next(iter(api.season_point_items()))
    try:
        api.add_gift(c.uid, 1, pt, 100, 1, 0)
        h.expect(False, "des points de saison ont été mis dans la boîte à cadeaux")
    except ValueError:
        pass
    h.expect(not userdata.table(c.uid, "gift") or all(g["itemId"] != pt for g in userdata.table(c.uid, "gift")),
             "cadeau de points de saison enregistré")
