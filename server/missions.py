"""Missions the server advances (mission.bin sub 0 rows with field 16 = 1, `master.MISSIONS` index 8).

The client counts some missions itself (character levels, limit breaks, taps on the home character...) and pushes
them; everything decided on the server -- quest and chapter clears, bonus missions, scouts, logins, daily and weekly
counts -- stayed at 0 forever: those missions could never be completed (Discord, 2026-10-17).

Counters live in the account data (`data["missions"]`): totals, plus daily and weekly ones reset at 04:00 JST
(weeks start on Monday). `update(uid)` recomputes every server mission row of the account's mission table and
writes the changed rows; the hooks (`on_quest_clear`, `on_login`, `on_scout`) bump the counters first.
Row states: 2 in progress, 3 done (the player can receive it, `mission/receive`), 4 received.
"""
import time
from datetime import datetime, timedelta, timezone

import config
import db
import master
import userdata

JST = timezone(timedelta(hours=9))
SERVICE_END = "2023/01/16 05:59:59"  # tools/base_windows.SERVICE_END
DAY_RESET = 4 * 3600 - 9 * 3600  # 04:00 JST, as an offset from UTC midnight
DAILY, WEEKLY = 2, 5  # mission categories (field 2)
GROWTH_EVENT = 2  # event.bin sub 0 field 1: growth events (EXP, plants, Col...)
TOWER_EVENT = 3  # event.bin sub 0 field 3: Cathedral


def real_day(ts: float) -> int:
    return int((ts - DAY_RESET) // 86400)


def real_week(ts: float) -> int:
    return (real_day(ts) + 3) // 7  # day 0 (1970-01-01) is a Thursday: weeks start on Monday


def _ts(text: str) -> float | None:
    try:
        return datetime.strptime(text, "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST).timestamp()
    except (TypeError, ValueError):
        return None


def is_open(label: str) -> bool:
    """A release window (base.bin sub 15) open at the game clock; no label or an unknown one = always."""
    forced = (db.get_setting("release_windows", {}) or {}).get(label) if label else None
    if forced in ("open", "closed"):  # admin panel, Banners tab (served to the client the same way)
        return forced == "open"
    window = master.RELEASES.get(label) if label else None
    if not window:
        return True
    now = config.now()
    start, end = _ts(window[0]), _ts(window[1])
    if label in config.EXTEND_WINDOWS:  # served to the client extended (assets._extend_windows)
        end = _ts(SERVICE_END)
    return (start is None or start <= now) and (end is None or now <= end)


# ---------------------------------------------------------------------------------------------- counters

def _state(data: dict) -> dict:
    st = data.setdefault("missions", {})
    for k in ("total", "daily", "weekly"):
        st.setdefault(k, {})
    today, week = real_day(time.time()), real_week(time.time())
    if st.get("day") != today:
        st["day"], st["daily"] = today, {}
    if st.get("week") != week:
        st["week"], st["weekly"] = week, {}
    return st


def bump(uid: int, **counts):
    """Add to the total, daily and weekly counters, e.g. bump(uid, quest=1, growth=1)."""
    with userdata._lock:
        data = userdata.load(uid)
        st = _state(data)
        for key, n in counts.items():
            for scope in ("total", "daily", "weekly"):
                st[scope][key] = st[scope].get(key, 0) + n
        db.save_user(uid, data)


def counters(uid: int) -> dict:
    with userdata._lock:
        data = userdata.load(uid)
        st = _state(data)
        db.save_user(uid, data)
        return {k: dict(st[k]) for k in ("total", "daily", "weekly")}


# ---------------------------------------------------------------------------------------------- hooks

def on_quest_clear(uid: int, quest_id: int, event_id: int, party_pc_ids: list):
    counts = {"quest": 1, f"quest_{quest_id}": 1}
    if event_id:
        if master.EVENT_KINDS.get(str(event_id)) == GROWTH_EVENT:
            counts["growth"] = 1
        else:
            counts["event"] = 1
    else:
        counts["main"] = 1
    for character in {master.PC_CHARACTER.get(str(pc), 0) for pc in party_pc_ids} - {0}:
        counts[f"char_{character}"] = 1
    if master.EVENT_TYPES.get(str(event_id)) == TOWER_EVENT and party_pc_ids:
        # Cathedral missions: who cleared the floor (a character, only women, only one weapon type)
        for character in {master.PC_CHARACTER.get(str(pc), 0) for pc in party_pc_ids} - {0}:
            counts[f"quest_{quest_id}_char_{character}"] = 1
        traits = [master.PC_TRAITS.get(str(pc), [0, 0]) for pc in party_pc_ids]
        if len({t[0] for t in traits}) == 1:
            counts[f"quest_{quest_id}_gender_{traits[0][0]}"] = 1
        if len({t[1] for t in traits}) == 1:
            counts[f"quest_{quest_id}_weapon_{traits[0][1]}"] = 1
    bump(uid, **counts)


def on_login(uid: int):
    """Once per real day: login days. Every login: time bonus windows (JST hours)."""
    st = counters(uid)
    counts = {}
    if not st["daily"].get("login"):
        counts["login"] = 1
    hour = datetime.now(JST).hour
    counts[f"login_hour_{hour}"] = 1 if not st["daily"].get(f"login_hour_{hour}") else 0
    bump(uid, **{k: v for k, v in counts.items() if v})


def on_pvp_bp(uid: int, bp: int):
    """BP spent in Ordinal Battle (guild mission "consume 15 BPs")."""
    if bp > 0:
        bump(uid, pvp_bp=bp)


def guild_accepted(uid: int, mission_id: int):
    """A guild mission was taken: it counts from now on, so keep the account's total counters of this moment."""
    with userdata._lock:
        data = userdata.load(uid)
        st = _state(data)
        st.setdefault("guild", {})[str(mission_id)] = dict(st["total"])
        db.save_user(uid, data)


def _guild_value(kind: int, m: list, ctx: dict, mission_id) -> int | None:
    """Guild missions (mission.daily_guild_mission_*): what was done since the mission was taken (guild.py writes
    the row at accept and reads its state). They were left to guild.py, which never counted anything: every taken
    guild mission stayed at 0/N (シンキラ, 2026-10-21)."""
    param = m[3]
    keys = {9009: ["event"], 9005: ["growth"], 1008: ["main"], 9007: ["pvp_bp"],
            1301: [f"char_{param}"], 1007: [f"quest_{q}" for q in param.split("-") if q]}.get(kind)
    base = ctx["guild"].get(str(mission_id))
    if keys is None or base is None:
        return None
    total = ctx["counters"]["total"]
    return sum(max(0, total.get(k, 0) - base.get(k, 0)) for k in keys)


def on_scout(uid: int, characters: int = 0, weapons: int = 0):
    bump(uid, **{k: v for k, v in (("scout_character", characters), ("scout_weapon", weapons)) if v})


# ---------------------------------------------------------------------------------------------- progress

def _quest_rows(uid: int) -> dict:
    by_level_id = {r.get("questLevelId"): r for r in userdata.table(uid, "questProgress")}
    return by_level_id


def _quest_row(rows: dict, quest_id: str, level: str) -> dict:
    ql = master.QUEST_LEVELS.get(f"{quest_id}:{level or 1}", {})
    return rows.get(ql.get("id"), {})


def _value(kind: int, category: int, m: list, ctx: dict) -> int | None:
    """Current count of one mission, or None for the kinds the server does not track (left as they are)."""
    param, level, extra, name = m[3], m[4], m[5], m[7]
    total, daily, weekly = ctx["counters"]["total"], ctx["counters"]["daily"], ctx["counters"]["weekly"]
    quests = ctx["quests"]
    if name.startswith("mission.daily_guild_mission"):
        return _guild_value(kind, m, ctx, ctx.get("mission_id"))
    if kind == 1001:  # clear a quest level
        return 1 if _quest_row(quests, param, level).get("clearCount") else 0
    if kind == 1002:  # every bonus mission of a chapter's quests
        done = 0
        for quest in [q for q in param.split("-") if q]:
            flags = _quest_row(quests, quest, level).get("bonusMission") or []
            done += 1 if flags and all(flags) else 0
        return done
    if kind == 1003:  # boss beaten with rank `extra` or better (1 = S)
        rank = _quest_row(quests, param, level).get("bossClearRank") or 0
        return 1 if rank and rank <= int(extra or 1) else 0
    if kind == 1004:  # boss beaten within `extra` turns
        turns = _quest_row(quests, param, level).get("bossClearTurn") or 0
        return 1 if turns and turns <= int(extra or 0) else 0
    if kind in (801, 3002, 3003, 3005, 3007):
        return _tower_value(kind, param, level, extra, ctx)
    if kind == 1102:  # a quest cleared with this character in the party (Cathedral 100F with Kirito)
        return 1 if total.get(f"quest_{param}_char_{extra}") else 0
    if kind == 1301:  # quests cleared with this character in the party
        return (daily if category == DAILY else total).get(f"char_{param}", 0)
    if kind == 101:  # user rank
        return master.player_rank(ctx["exp"])
    if kind == 102:  # login days
        return total.get("login", 0)
    if kind == 401:
        return total.get("scout_weapon", 0)
    if kind == 402:
        return total.get("scout_character", 0)
    if kind == 1:  # every listed mission done
        ids = [int(x) for x in param.split("+") if x.isdigit()]
        return 1 if ids and all(ctx["states"].get(i, 0) >= 3 for i in ids) else 0
    if kind == 2:  # "every daily mission" done N times
        return total.get("daily_all", 0)
    daily_kinds = {9002: "quest", 9005: "growth", 9009: "event", 9008: "scout_character"}
    if kind in daily_kinds:
        return daily.get(daily_kinds[kind], 0)
    if kind == 9006:  # logged in between hours `param` and `level` (JST)
        start, end = int(param or 0), int(level or 0)
        return 1 if any(daily.get(f"login_hour_{h}") for h in range(start, end)) else 0
    weekly_kinds = {8002: "quest", 8003: "growth", 8007: "event", 8006: "scout_character", 8005: "login"}
    if kind in weekly_kinds:
        return weekly.get(weekly_kinds[kind], 0)
    return None


def _tower_floors(event_id: str, level: str) -> list:
    """Quest ids of the Cathedral floors of one difficulty (towerLevel)."""
    quests = {v: k.split(":")[1] for k, v in master.TOWER_EVENT_QUESTS.items() if k.startswith(f"{event_id}:")}
    return [quests[fid] for fid, lv, _ in master.TOWER_FLOORS.get(str(event_id), [])
            if str(lv) == str(level or 1) and fid in quests]


def _tower_value(kind: int, event_id: str, level: str, extra: str, ctx: dict) -> int:
    """Cathedral missions (category 6), all server-tracked and never counted: their Diamond Cubes could not be
    earned (tester report, 2026-10-18). 801 every floor of the difficulty cleared, 3002 floors with every bonus mission,
    3007 / 3005 floors cleared with only women / only one weapon type, 3003 every floor cleared retiring at most
    `extra` times (lostCount covers the whole event)."""
    floors = _tower_floors(event_id, level)
    rows = [_quest_row(ctx["quests"], q, "1") for q in floors]
    total = ctx["counters"]["total"]
    if kind == 3002:
        return sum(1 for r in rows if r.get("bonusMission") and all(r["bonusMission"]))
    if kind == 3007:
        return sum(1 for q in floors if total.get(f"quest_{q}_gender_{extra}"))
    if kind == 3005:
        return sum(1 for q in floors if total.get(f"quest_{q}_weapon_{extra}"))
    cleared = bool(floors) and all(r.get("clearCount") for r in rows)
    if kind == 3003:
        lost = next((s.get("lostCount", 0) for s in ctx["tower"] if str(s.get("eventId")) == str(event_id)), 0)
        return 1 if cleared and lost <= int(extra or 0) else 0
    return 1 if cleared else 0


def update(uid: int) -> dict:
    """Recompute the server missions of the account; returns the set_tables response (or {} when unchanged)."""
    rows = [dict(r) for r in userdata.table(uid, "mission")]
    if not rows:
        return {}
    _seed_character_counts(uid, rows)
    st = counters(uid)
    info = (userdata.table(uid, "info") or [{}])[0]
    now = int(config.now())
    with userdata._lock:
        data = userdata.load(uid)
        ms = _state(data)
        reset = ms.get("reset", {})
        guild_base = dict(ms.get("guild", {}))
    changed = False
    # daily and weekly missions start again each day / week (client-counted ones too)
    for category, key in ((DAILY, "day"), (WEEKLY, "week")):
        if reset.get(key) != ms[key]:
            for r in rows:
                m = master.MISSIONS.get(str(r.get("missionId")))
                cat = m[1] if m else None
                if cat == category and (r.get("progress") or r.get("state") != 2):
                    r.update(progress=0, state=2, completedAt=0, progressUpdatedAt=now, readState=0)
                    changed = True
            reset[key] = ms[key]
    ctx = {"counters": st, "quests": _quest_rows(uid), "exp": info.get("totalExp", 0), "guild": guild_base,
           "tower": userdata.table(uid, "towerEventStatus"),
           "states": {r.get("missionId"): r.get("state", 0) for r in rows}}
    for _ in range(2):  # "every daily mission" and the beginner lists depend on other rows of this pass
        for r in rows:
            m = master.MISSIONS.get(str(r.get("missionId")))
            if not m or not m[8] or r.get("state", 0) >= 3 or not is_open(m[6]):
                continue
            kind, category, target = m[0], m[1], m[2]
            if kind in (9001, 8001):  # every other daily / weekly mission done
                others = [x for x in rows if x is not r and _category(x) == category and _open(x)
                          and _kind(x) not in (9001, 8001, 9006)
                          and not str(master.MISSIONS.get(str(x.get("missionId")), [""] * 8)[7]).startswith(
                              "mission.daily_guild_mission")]
                value = 1 if others and all(x.get("state", 0) >= 3 for x in others) else 0
            else:
                ctx["mission_id"] = r.get("missionId")
                value = _value(kind, category, m, ctx)
            if value is None:
                continue
            progress = min(int(value), target)
            if category not in (DAILY, WEEKLY):
                # never backwards: the counters only started on 2026-10-17, while the accounts restored from the
                # archive kept the game's own progress (affinity titles 2 and 3 went back, tester report, 2026-10-22)
                progress = max(progress, int(r.get("progress") or 0))
            if progress != r.get("progress"):
                r.update(progress=progress, progressUpdatedAt=now)
                changed = True
            if progress >= target:
                r.update(state=3, completedAt=now, isNotifiable=True, readState=0)
                ctx["states"][r.get("missionId")] = 3
                changed = True
                if kind == 9001:
                    bump(uid, daily_all=1)
                    ctx["counters"]["total"]["daily_all"] = ctx["counters"]["total"].get("daily_all", 0) + 1
    with userdata._lock:
        data = userdata.load(uid)
        _state(data)["reset"] = reset
        db.save_user(uid, data)
    return userdata.set_tables(uid, {"mission": rows}) if changed else {}


def _seed_character_counts(uid: int, rows: list):
    """The "clear N quests with this character" counters (kind 1301, normal category: the affinity titles, 50 to
    1000 clears) start at the best progress the account's own rows show, so the next titles keep counting from
    there instead of from the server's first count."""
    best = {}
    for r in rows:
        m = master.MISSIONS.get(str(r.get("missionId")))
        if not m or m[0] != 1301 or m[1] != 1:
            continue
        done = m[2] if r.get("state", 0) >= 3 else 0
        key = f"char_{m[3]}"
        best[key] = max(best.get(key, 0), int(r.get("progress") or 0), done)
    if not best:
        return
    with userdata._lock:
        data = userdata.load(uid)
        total = _state(data)["total"]
        if all(total.get(k, 0) >= v for k, v in best.items()):
            return
        for k, v in best.items():
            total[k] = max(total.get(k, 0), v)
        db.save_user(uid, data)


def _kind(row) -> int:
    m = master.MISSIONS.get(str(row.get("missionId")))
    return m[0] if m else 0


def _category(row) -> int:
    m = master.MISSIONS.get(str(row.get("missionId")))
    return m[1] if m else 0


def _open(row) -> bool:
    m = master.MISSIONS.get(str(row.get("missionId")))
    return is_open(m[6]) if m else True
