"""Daily-limited events (Vigil Key / Growth Plant collection: 5 runs a game day, event.bin sub 0 field 4): every
clear counts in UserEventProgress.playCount, which starts again the next game day; UserQuestProgress.playCountOfDay
too (Discord 2026-10-28 "Vigil Key Collection Level Attempts stay at 5")."""
from harness import config, userdata

EVENT = 601102000019
QUEST = {"boostItemIds": [], "eventId": EVENT, "level": 1, "partyId": 1, "questId": 8014058001, "supporterPcId": 0,
         "supporterPcSlotId": 0, "supporterUserId": 0}


def _event_row(c):
    return next((r for r in c.t("eventProgress") if r.get("eventId") == EVENT), {})


def _run(c, skip=False):
    if skip:
        c.call("quest/skip", QUEST)
    else:
        c.call("quest/start", QUEST)
        c.call("quest/clear")


def test_event_runs_counted_per_day(h):
    c = h.new_player()
    _run(c)
    _run(c, skip=True)
    row = _event_row(c)
    h.expect(row.get("playCount") == 2, f"essais du jour après 2 parties : {row}")
    progress = next(r for r in c.t("questProgress") if r.get("questLevelId") == 8140580011)
    h.expect(progress["playCountOfDay"] == 2, f"playCountOfDay : {progress['playCountOfDay']}")
    # a game day later the counts start again
    yesterday = int(config.now()) - 86400
    rows = [dict(r, lastPlayedAt=yesterday) if r.get("eventId") == EVENT else r
            for r in userdata.table(c.uid, "eventProgress")]
    qp = [dict(r, lastPlayedAt=yesterday) if r.get("questLevelId") == 8140580011 else r
          for r in userdata.table(c.uid, "questProgress")]
    userdata.set_tables(c.uid, {"eventProgress": rows, "questProgress": qp})
    c.pull()
    _run(c)
    h.expect(_event_row(c).get("playCount") == 1, f"essais le lendemain : {_event_row(c)}")
    progress = next(r for r in c.t("questProgress") if r.get("questLevelId") == 8140580011)
    h.expect(progress["playCountOfDay"] == 1 and progress["playCount"] == 3,
             f"playCountOfDay le lendemain : {progress['playCountOfDay']} (total {progress['playCount']})")


def test_unlimited_event_not_counted(h):
    c = h.new_player()
    before = [dict(r) for r in c.t("eventProgress")]
    c.call("quest/start", dict(QUEST, eventId=601102000018, questId=8014044003))
    c.call("quest/clear")
    h.expect(c.t("eventProgress") == before, "un événement sans limite ne doit pas être compté")
