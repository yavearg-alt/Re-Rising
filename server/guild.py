"""Guilds: shared between accounts, stored one JSON document per guild (db.guilds).

The client keeps only per-viewer copies of the guild tables; after every guild request (and when it pulls a
guild table) we rebuild them from the shared document for the requesting player. Formats are the client
serializers (field order matters for the table md5):
  UserGuild                   FUN_0153428c  UserGuildMember              FUN_01539170
  UserGuildChat               FUN_0153e194  UserGuildBoard               FUN_015400a8
  UserGuildTimeLine           FUN_01541bec  UserGuild{Chat,Board,TimeLine}UpdatedAt  FUN_01536774/015374a0/015381cc
  UserGuildJoinRequest        FUN_0153a314  UserGuildJoinRequestReceive  FUN_0153ae78
  UserGuildMemberInvite       FUN_0153bc8c  UserGuildMemberInviteReceive FUN_0153cc60
  UserGuildNotification       FUN_015440f0  UserGuildClosedChatAgreement FUN_01547444
  UserGuildRecommendInviteMember FUN_01547df4  UserGuildMission FUN_0154c490
  UserGuildMissionStatus      FUN_0154ab24  UserGuildMemberMission       FUN_0154db58
Roles (FUN_01342898): 1 leader, 2 sub-leader, 3 member. Timeline types = message.guild_timeline_<n>.
Errors are returned as the response "code" (message.server_error_<code>).
"""
import json
import random
import time

import config
import db
import master
import missions
import userdata

ROLE_LEADER, ROLE_SUBLEADER, ROLE_MEMBER = 1, 2, 3
MAX_MEMBERS = 30
KEEP_MESSAGES = 200
# guild mission states (guessed, same scale as UserMission: 2 = in progress, 3 = done)
MISSION_OPEN, MISSION_ACCEPTED, MISSION_DONE = 1, 2, 3

ERR_FULL = 28122807
ERR_ALREADY_MEMBER = 28122808
ERR_NO_USER = 28122810
ERR_DISBANDED = 28122811
ERR_ALREADY_REQUESTED = 28122812
ERR_NOT_MEMBER = 28122813
ERR_INVITE_GONE = 28122814
ERR_REQUEST_GONE = 28122815
ERR_KICK_LEADER = 28122816
ERR_MISSION_GONE = 28122820
ERR_MISSION_TAKEN = 28122822
ERR_MISSION_OTHER = 28122824

# timeline types (message.guild_timeline_<n>): parameterA/B/C fill {0}/{1}/{2}
TL_RECRUITED, TL_JOINED, TL_LEFT, TL_INVITED, TL_INVITE_CANCELLED, TL_INVITE_REJECTED = 1, 3, 4, 5, 6, 7
TL_LEADER, TL_SUBLEADER, TL_JOIN_REQUEST = 8, 9, 10


def now() -> int:
    return int(config.now())


# ---------------------------------------------------------------- storage

def load(guild_id: int):
    return db.get_guild(int(guild_id or 0)) if guild_id else None


def save(g: dict):
    g["updatedAt"] = now()
    db.save_guild(g["id"], g)


def guild_of(uid: int):
    """The guild the player belongs to (or None)."""
    for g in db.list_guilds():
        if str(uid) in g.get("members", {}):
            return g
    return None


def _next_id(g: dict, key: str) -> int:
    g.setdefault("ids", {})
    g["ids"][key] = g["ids"].get(key, 0) + 1
    return g["ids"][key]


def timeline(g: dict, kind: int, a="", b="", c=""):
    g.setdefault("timeline", []).append({"id": _next_id(g, "timeline"), "type": kind, "parameterA": str(a),
                                         "parameterB": str(b), "parameterC": str(c), "registeredAt": now()})
    g["timeline"] = g["timeline"][-KEEP_MESSAGES:]


def _user_info(uid: int) -> dict:
    return (userdata.table(uid, "info") or [{}])[0]


def _name(uid: int) -> str:
    return _user_info(uid).get("name", "")


def combat_power(uid: int) -> int:
    """No server-side formula is known: sum of the active party's max hp + mp, a stable stand-in."""
    info = _user_info(uid)
    party = next((p for p in userdata.table(uid, "party") if p.get("id") == info.get("activePartyId", 1)),
                 {"slots": []})
    members = {s.get("pcId") for s in party.get("slots", []) if s.get("pcId")}
    total = 0
    for pc in userdata.table(uid, "pc"):
        if pc.get("pcId") in members:
            hp, mp = master.pc_max_hp_mp(pc["pcId"], pc.get("totalExp", 0))
            total += hp + mp
    return total


def add_member(g: dict, uid: int, role: int = ROLE_MEMBER):
    g["members"][str(uid)] = {"role": role, "registeredAt": now(), "missionPoint": 0, "missionPointUpdatedAt": 0,
                              "rewardReceivedAt": 0, "dialogReadAt": 0}
    g.get("requests", {}).pop(str(uid), None)
    g.get("invites", {}).pop(str(uid), None)
    # a player is in one guild only: drop their other pending requests and invites
    for other in db.list_guilds():
        if other["id"] != g["id"] and (str(uid) in other.get("requests", {}) or str(uid) in other.get("invites", {})):
            other.get("requests", {}).pop(str(uid), None)
            other.get("invites", {}).pop(str(uid), None)
            save(other)


def remove_member(g: dict, uid: int) -> bool:
    """Remove a member; hands the lead over (sub-leader, else oldest member). False if the guild is now empty
    (it is then deleted)."""
    m = g["members"].pop(str(uid), None)
    if not g["members"]:
        db.delete_guild(g["id"])
        return False
    if g.get("subLeader") == uid:
        g["subLeader"] = 0
    if m and m["role"] == ROLE_LEADER:
        heir = g.get("subLeader") or min(g["members"], key=lambda k: g["members"][k]["registeredAt"])
        g["leader"] = int(heir)
        g["members"][str(heir)]["role"] = ROLE_LEADER
        if g.get("subLeader") == int(heir):
            g["subLeader"] = 0
        timeline(g, TL_LEADER, _name(int(heir)))
    return True


def role(g: dict, uid: int) -> int:
    return (g or {}).get("members", {}).get(str(uid), {}).get("role", 0)


def is_officer(g: dict, uid: int) -> bool:
    return role(g, uid) in (ROLE_LEADER, ROLE_SUBLEADER)


# ---------------------------------------------------------------- guild missions

def game_day(ts: float) -> int:
    return int((ts - (4 * 3600 - 9 * 3600)) // 86400)  # daily reset 04:00 JST


def missions_today(g: dict) -> bool:
    """Draw the day's guild missions (guildMissionReceiveCount missions per group). True if redrawn."""
    day = game_day(now())
    if g.get("missionDay") == day and g.get("missions"):
        return False
    done = sum(1 for m in g.get("missions", []) if m["state"] == MISSION_DONE)
    g["yesterdayCompleted"] = done if g.get("missionDay") == day - 1 else 0
    by_group = {}
    for mid, m in master.GUILD["missions"].items():
        by_group.setdefault(str(m["group"]), []).append(int(mid))
    rng = random.Random(g["id"] * 100003 + day)
    picks = []
    for group in sorted(by_group, key=int):
        pool = sorted(by_group[group])
        picks += rng.sample(pool, min(len(pool), master.GUILD["counts"].get(group, 1)))
    g["missions"] = [{"index": i, "guildMissionId": mid, "chargeUserId": 0, "state": MISSION_OPEN,
                      "acceptedAt": 0, "completedAt": 0} for i, mid in enumerate(picks)]
    g["missionDay"] = day
    g["missionLottedAt"] = now()
    week = day // 7
    if g.get("missionWeek") != week:
        g["previousWeekPoint"] = g.get("missionPoint", 0) if g.get("missionWeek") == week - 1 else 0
        g["previousWeekPointUpdatedAt"] = now()
        g["missionPoint"] = 0
        g["missionWeek"] = week
        for m in g["members"].values():
            m["missionPoint"] = 0
    return True


def sync_missions(g: dict) -> bool:
    """Accepted guild missions whose player has completed the matching UserMission become done (+ points)."""
    changed = False
    for m in g.get("missions", []):
        if m["state"] != MISSION_ACCEPTED or not m["chargeUserId"]:
            continue
        info = master.GUILD["missions"].get(str(m["guildMissionId"]), {})
        row = next((r for r in userdata.table(m["chargeUserId"], "mission")
                    if r.get("missionId") == info.get("missionId")), None)
        if row and row.get("state", 0) >= 3:
            m.update(state=MISSION_DONE, completedAt=row.get("completedAt") or now())
            g["missionPoint"] = g.get("missionPoint", 0) + info.get("point", 0)
            g["missionPointUpdatedAt"] = now()
            member = g["members"].get(str(m["chargeUserId"]))
            if member:
                member["missionPoint"] += info.get("point", 0)
                member["missionPointUpdatedAt"] = now()
            changed = True
    return changed


def point_reward_group(points: int) -> int:
    group = 0
    for need, reward in master.GUILD["pointRewards"]:
        if points >= need:
            group = reward
    return group


# ---------------------------------------------------------------- per-viewer tables

def guild_row(viewer: int, g: dict) -> dict:
    leader_info = _user_info(g["leader"])
    power = sum(combat_power(int(u)) for u in g["members"])
    return {"userId": viewer, "guildId": g["id"], "leaderUserId": g["leader"],
            "leaderUserRank": master.player_rank(leader_info.get("totalExp", 0)),
            "subLeaderUserId": g.get("subLeader", 0), "guildRank": 1, "guildLastRaidEventRank": 0,
            "comment": g.get("comment", ""), "iconItemId": g.get("iconItemId", 0),
            "atmosphereType": g.get("atmosphereType", 0), "totalCombatPower": power,
            "isRequiredApproval": bool(g.get("isRequiredApproval")), "memberCount": len(g["members"]),
            "name": g.get("name", ""), "lastUpdatedAt": g.get("updatedAt", 0),
            "raidEventDefeatPointBonusHour": g.get("bonusHour", 0), "raidEventDefeatPointBonusTime": 0}


def member_rows(viewer: int, g: dict) -> list:
    return [{"userId": viewer, "guildId": g["id"], "memberUserId": int(u), "combatPower": combat_power(int(u)),
             "role": m["role"], "registeredAt": m["registeredAt"], "isBanUser": False}
            for u, m in g["members"].items()]


def guest_row(viewer: int, other: int):
    """UserGuest row (profile card) of a real player, built from their tables."""
    t = userdata.load(other)["tables"]
    info = (t.get("info") or [None])[0]
    if not info:
        return None
    slots = t.get("supporterPcSlot") or []
    wanted = {s.get("pcId") for s in slots} | {info.get("profilePcId"), info.get("homePcId")}
    pcs = [p for p in t.get("pc", []) if p.get("pcId") in wanted] or (t.get("pc") or [])[:1]
    gear = {p.get(k) for p in pcs for k in ("weaponEquipmentStockId", "armorEquipmentStockId",
                                            "badgeEquipmentStockId")} - {0, None}
    return {"userId": viewer, "guestUserId": other, "info": info, "pc": pcs,
            "equipmentStock": [e for e in t.get("equipmentStock", []) if e.get("id") in gear],
            "supporterPcSlot": slots, "imaginationStock": [], "profileTrackers": []}


def with_guests(viewer: int, uids) -> list:
    """The viewer's guest table plus the profiles of these players."""
    known = {g["guestUserId"]: g for g in userdata.table(viewer, "guest")}
    for u in uids:
        if u and u != viewer:
            row = guest_row(viewer, int(u))
            if row:
                known[int(u)] = row
    return list(known.values())


def tables(uid: int, extra=()) -> dict:
    """Every guild table for this viewer: their guild (if any), plus the guilds in `extra` (search results)."""
    g = guild_of(uid)
    if g and (missions_today(g) | sync_missions(g)):
        save(g)
    gid = g["id"] if g else 0
    everyone = db.list_guilds()
    # the guilds that invited me or that I asked to join: the invitation / request screens read their UserGuild
    # row; it was missing, so opening the invitations crashed the game (tester report, Discord 2026-10-30)
    pending = [x for x in everyone if str(uid) in x.get("invites", {}) or str(uid) in x.get("requests", {})]
    shown = [g] if g else []
    for x in list(extra) + pending:
        if x and x["id"] != gid and all(x["id"] != y["id"] for y in shown):
            shown.append(x)
    out = {
        "guild": [guild_row(uid, x) for x in shown],
        "guildMember": [r for x in shown for r in member_rows(uid, x)],
    }
    # the client decides "in a guild" (guild menu) vs "no guild" (search screen) from UserInfo.guildId, not from
    # the guild tables: keep it in step with the membership (it stayed 0, so members kept seeing the search)
    info = dict((userdata.table(uid, "info") or [{}])[0])
    if info and info.get("guildId", 0) != gid:
        info["guildId"] = gid
        if gid:
            info["lastGuildJoinedAt"] = g["members"].get(str(uid), {}).get("registeredAt") or now()
        out["info"] = [info]
    chat = (g or {}).get("chat", [])
    board = (g or {}).get("board", [])
    tl = (g or {}).get("timeline", [])
    out["guildChat"] = [{"userId": uid, "guildId": gid, "id": c["id"], "speakerUserId": c["speaker"],
                         "text": c["text"], "updatedAt": c["at"], "isDeleted": c.get("deleted", False),
                         "fixedMessageId": c.get("fixedMessageId", 0), "stampId": c.get("stampId", 0),
                         "battleLogId": c.get("battleLogId", 0), "battleLogType": c.get("battleLogType", 0)}
                        for c in chat]
    out["guildBoard"] = [{"userId": uid, "guildId": gid, "id": b["id"], "speakerUserId": b["speaker"],
                          "text": b["text"], "updatedAt": b["at"], "isDeleted": b.get("deleted", False),
                          "type": b.get("type", 0), "parameterA": b.get("parameterA", "")} for b in board]
    out["guildTimeLine"] = [dict({"userId": uid, "guildId": gid}, **t) for t in tl]
    out["guildChatUpdatedAt"] = [{"userId": uid, "guildId": gid, "lastChatUpdatedAt": chat[-1]["at"] if chat else 0,
                                  "lastChatId": chat[-1]["id"] if chat else 0}] if g else []
    out["guildBoardUpdatedAt"] = [{"userId": uid, "guildId": gid,
                                   "lastBoardUpdatedAt": board[-1]["at"] if board else 0,
                                   "lastBoardId": board[-1]["id"] if board else 0}] if g else []
    out["guildTimeLineUpdatedAt"] = [{"userId": uid, "guildId": gid,
                                      "lastTimeLineUpdatedAt": tl[-1]["registeredAt"] if tl else 0,
                                      "lastTimeLineId": tl[-1]["id"] if tl else 0}] if g else []
    # my pending join requests / invites received (all guilds)
    out["guildJoinRequest"] = [{"userId": uid, "guildId": x["id"], "registeredAt": x["requests"][str(uid)]}
                               for x in everyone if str(uid) in x.get("requests", {})]
    out["guildMemberInviteReceive"] = [
        {"userId": uid, "guildId": x["id"], "inviteUserId": x["invites"][str(uid)]["by"],
         "registeredAt": x["invites"][str(uid)]["at"], "readState": 0}
        for x in everyone if str(uid) in x.get("invites", {})]
    # what my guild has pending
    out["guildJoinRequestReceive"] = [{"userId": uid, "guildId": gid, "requestUserId": int(u), "registeredAt": at}
                                      for u, at in (g or {}).get("requests", {}).items()]
    out["guildMemberInvite"] = [{"userId": uid, "guildId": gid, "invitedUserId": int(u), "inviteUserId": i["by"],
                                 "registeredAt": i["at"]} for u, i in (g or {}).get("invites", {}).items()]
    out["guildMission"] = [dict({"userId": uid, "guildId": gid}, **m) for m in (g or {}).get("missions", [])]
    out["guildMissionStatus"] = [{
        "userId": uid, "guildId": gid, "guildMissionPoint": g.get("missionPoint", 0),
        "guildMissionLottedAt": g.get("missionLottedAt", 0),
        "guildMissionPointUpdatedAt": g.get("missionPointUpdatedAt", 0),
        "previousWeekGuildMissionPoint": g.get("previousWeekPoint", 0),
        "previousWeekGuildMissionPointUpdatedAt": g.get("previousWeekPointUpdatedAt", 0),
        "previousWeekGuildMissionPointRewardGroupId": point_reward_group(g.get("previousWeekPoint", 0)),
        "yesterdayGuildMissionCompletedCount": g.get("yesterdayCompleted", 0)}] if g else []
    out["guildMemberMission"] = [{"userId": uid, "guildId": gid, "memberUserId": int(u),
                                  "guildMissionPoint": m.get("missionPoint", 0),
                                  "guildMissionPointUpdatedAt": m.get("missionPointUpdatedAt", 0),
                                  "guildMissionRewardReceivedAt": m.get("rewardReceivedAt", 0),
                                  "guildMissionRewardDialogReadAt": m.get("dialogReadAt", 0)}
                                 for u, m in (g or {}).get("members", {}).items()]
    out["guildNotification"] = [notification(uid, g, out)]
    people = {x["leader"] for x in shown} | {int(u) for x in shown for u in x["members"]}
    people |= {int(u) for u in (g or {}).get("requests", {})} | {int(u) for u in (g or {}).get("invites", {})}
    people |= {r["inviteUserId"] for r in out["guildMemberInviteReceive"]}
    out["guest"] = with_guests(uid, people)
    return out


def notification(uid: int, g, out: dict) -> dict:
    def last_read(name, key):
        return max((r.get(key, 0) for r in userdata.table(uid, name)), default=0)
    chat, board = out["guildChat"], out["guildBoard"]
    my_missions = any(m["state"] == MISSION_OPEN for m in (g or {}).get("missions", []))
    return {"userId": uid, "guildId": g["id"] if g else 0,
            "hasNotReadChat": bool(chat) and chat[-1]["id"] > last_read("guildChatLastRead", "lastReadChatId"),
            "hasNotReadBoard": bool(board) and board[-1]["id"] > last_read("guildBoardLastRead", "lastReadBoardId"),
            "hasJoinRequested": bool(g and is_officer(g, uid) and g.get("requests")),
            "hasInvite": bool(out["guildMemberInviteReceive"]), "hasNotReadRaidResult": False,
            "hasAcceptableGuildMission": bool(g) and my_missions}


def respond(uid: int, extra=(), more: dict | None = None) -> dict:
    changes = tables(uid, extra)
    changes.update(more or {})
    return userdata.set_tables(uid, changes)


def refresh_silently(uid: int):
    """Store fresh guild tables (so a following user/pull sees what other members changed)."""
    userdata.set_tables(uid, tables(uid))


# ---------------------------------------------------------------- endpoints

def _int(req, key) -> int:
    try:
        return int((req or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def ep_top(uid, req):
    return respond(uid)


def _apply_settings(g: dict, req: dict):
    # texts length-capped and numbers forced to numbers: they are stored and sent to every member
    for key, field, limit in (("name", "name", 40), ("comment", "comment", 300)):
        if key in (req or {}):
            g[field] = str(req[key] or "")[:limit]
    for key, field in (("atmosphereType", "atmosphereType"), ("iconId", "iconItemId"),
                       ("raidEventDefeatPointBonusHour", "bonusHour")):
        if key in (req or {}):
            g[field] = _int(req, key)
    if "isRequiredApproval" in (req or {}):
        g["isRequiredApproval"] = bool(req["isRequiredApproval"])


def ep_form(uid, req):
    """{"name", "comment", "atmosphereType", "iconId", "isRequiredApproval"}"""
    if guild_of(uid):
        return {"code": ERR_ALREADY_MEMBER}
    gid = db.create_guild()
    g = {"id": gid, "leader": uid, "subLeader": 0, "members": {}, "requests": {}, "invites": {}, "chat": [],
         "board": [], "timeline": [], "createdAt": now(), "name": "", "comment": "", "atmosphereType": 1,
         "iconItemId": 0, "isRequiredApproval": False, "bonusHour": 0}
    _apply_settings(g, req)
    add_member(g, uid, ROLE_LEADER)
    timeline(g, TL_JOINED, _name(uid), g["name"])
    missions_today(g)
    save(g)
    return respond(uid)


def ep_info_update(uid, req):
    g = guild_of(uid)
    if not g or not is_officer(g, uid):
        return {"code": ERR_NOT_MEMBER}
    _apply_settings(g, req)
    save(g)
    return respond(uid)


def ep_search(uid, req):
    """{"guildId"}: one guild by id."""
    g = load(_int(req, "guildId"))
    if not g:
        return {"code": ERR_DISBANDED}
    return respond(uid, [g])


def ep_search_recommend(uid, req):
    mine = guild_of(uid)
    found = [g for g in db.list_guilds() if len(g.get("members", {})) < MAX_MEMBERS
             and (not mine or g["id"] != mine["id"])]
    random.shuffle(found)
    return respond(uid, found[:10])


def ep_search_friend(uid, req):
    follows = {r.get("followUserId") for r in userdata.table(uid, "follow")}
    found = [g for g in db.list_guilds() if follows & {int(u) for u in g.get("members", {})}]
    return respond(uid, found[:10] or [])


def ep_join_request(uid, req):
    """{"guildId"}: join at once, or file a request if the guild requires approval."""
    if guild_of(uid):
        return {"code": ERR_ALREADY_MEMBER}
    g = load(_int(req, "guildId"))
    if not g:
        return {"code": ERR_DISBANDED}
    if len(g["members"]) >= MAX_MEMBERS:
        return {"code": ERR_FULL}
    if g.get("isRequiredApproval"):
        if str(uid) in g.setdefault("requests", {}):
            return {"code": ERR_ALREADY_REQUESTED}
        g["requests"][str(uid)] = now()
        timeline(g, TL_JOIN_REQUEST, _name(uid), g["name"])
    else:
        add_member(g, uid)
        timeline(g, TL_JOINED, _name(uid), g["name"])
    save(g)
    return respond(uid, [g])


def ep_join_request_cancel(uid, req):
    g = load(_int(req, "guildId"))
    if g and g.get("requests", {}).pop(str(uid), None) is not None:
        save(g)
    return respond(uid)


def ep_join_request_list(uid, req):
    return respond(uid)


def ep_join_request_accept(uid, req):
    """{"requestUserId"}"""
    g = guild_of(uid)
    other = _int(req, "requestUserId")
    if not g or not is_officer(g, uid):
        return {"code": ERR_NOT_MEMBER}
    if str(other) not in g.get("requests", {}):
        return {"code": ERR_REQUEST_GONE}
    if guild_of(other):
        g["requests"].pop(str(other), None)
        save(g)
        return {"code": ERR_ALREADY_MEMBER}
    if len(g["members"]) >= MAX_MEMBERS:
        return {"code": ERR_FULL}
    add_member(g, other)
    timeline(g, TL_RECRUITED, _name(uid), _name(other))
    save(g)
    return respond(uid)


def ep_join_request_reject(uid, req):
    g = guild_of(uid)
    if g and is_officer(g, uid) and g.get("requests", {}).pop(str(_int(req, "requestUserId")), None) is not None:
        save(g)
    return respond(uid)


def ep_leave(uid, req):
    g = guild_of(uid)
    if not g:
        return respond(uid)
    name = g["name"]
    if remove_member(g, uid):
        timeline(g, TL_LEFT, _name(uid), name)
        save(g)
    return respond(uid)


def ep_force_kick(uid, req):
    """{"kickedUserId"}"""
    g = guild_of(uid)
    other = _int(req, "kickedUserId")
    if not g or not is_officer(g, uid):
        return {"code": ERR_NOT_MEMBER}
    if str(other) not in g["members"]:
        return {"code": ERR_NOT_MEMBER}
    if role(g, other) == ROLE_LEADER or (role(g, other) == ROLE_SUBLEADER and role(g, uid) != ROLE_LEADER):
        return {"code": ERR_KICK_LEADER}
    remove_member(g, other)
    timeline(g, TL_LEFT, _name(other), g["name"])
    save(g)
    return respond(uid)


def ep_role_update(uid, req):
    """{"memberUserId", "role"}: 1 hands the lead over, 2 appoints the (single) sub-leader, 3 demotes."""
    g = guild_of(uid)
    other, new = _int(req, "memberUserId"), _int(req, "role")
    if not g or role(g, uid) != ROLE_LEADER or str(other) not in g["members"]:
        return {"code": ERR_NOT_MEMBER}
    members = g["members"]
    if new == ROLE_LEADER:
        members[str(uid)]["role"] = ROLE_MEMBER
        members[str(other)]["role"] = ROLE_LEADER
        g["leader"] = other
        if g.get("subLeader") == other:
            g["subLeader"] = 0
        timeline(g, TL_LEADER, _name(other))
    elif new == ROLE_SUBLEADER:
        if g.get("subLeader") and str(g["subLeader"]) in members:
            members[str(g["subLeader"])]["role"] = ROLE_MEMBER
        members[str(other)]["role"] = ROLE_SUBLEADER
        g["subLeader"] = other
        timeline(g, TL_SUBLEADER, _name(other))
    else:
        members[str(other)]["role"] = ROLE_MEMBER
        if g.get("subLeader") == other:
            g["subLeader"] = 0
    save(g)
    return respond(uid)


def ep_invite(uid, req):
    """{"invitedUserId"}"""
    g = guild_of(uid)
    other = _int(req, "invitedUserId")
    if not g:
        return {"code": ERR_NOT_MEMBER}
    if not db.get_user(other):
        return {"code": ERR_NO_USER}
    if guild_of(other):
        return {"code": ERR_ALREADY_MEMBER}
    g.setdefault("invites", {})[str(other)] = {"by": uid, "at": now()}
    timeline(g, TL_INVITED, _name(uid), _name(other))
    save(g)
    return respond(uid)


def ep_invite_cancel(uid, req):
    g = guild_of(uid)
    other = _int(req, "invitedUserId")
    if g and g.get("invites", {}).pop(str(other), None) is not None:
        timeline(g, TL_INVITE_CANCELLED, _name(uid), _name(other))
        save(g)
    return respond(uid)


def ep_invite_accept(uid, req):
    """{"guildId"}"""
    if guild_of(uid):
        return {"code": ERR_ALREADY_MEMBER}
    g = load(_int(req, "guildId"))
    if not g:
        return {"code": ERR_DISBANDED}
    if str(uid) not in g.get("invites", {}):
        return {"code": ERR_INVITE_GONE}
    if len(g["members"]) >= MAX_MEMBERS:
        return {"code": ERR_FULL}
    add_member(g, uid)
    timeline(g, TL_JOINED, _name(uid), g["name"])
    save(g)
    return respond(uid)


def ep_invite_reject(uid, req):
    g = load(_int(req, "guildId"))
    if g and g.get("invites", {}).pop(str(uid), None) is not None:
        timeline(g, TL_INVITE_REJECTED, _name(uid))
        save(g)
    return respond(uid)


def ep_invite_list(uid, req):
    return respond(uid)


def ep_invite_member_list(uid, req):
    return respond(uid)


def ep_invite_recommend_list(uid, req):
    """Players in no guild: UserGuildRecommendInviteMember {userId, guestId} + their profiles."""
    in_guild = {int(u) for g in db.list_guilds() for u in g.get("members", {})}
    free = [u for u in db.user_ids() if u != uid and u not in in_guild and userdata.table(u, "info")]
    random.shuffle(free)
    free = free[:20]
    rows = [{"userId": uid, "guestId": u} for u in free]
    changes = tables(uid)
    changes["guildRecommendInviteMember"] = rows
    changes["guest"] = with_guests(uid, free + [g["guestUserId"] for g in changes["guest"]])
    return userdata.set_tables(uid, changes)


def ep_agreement(uid, req):
    rows = userdata.table(uid, "guildClosedChatAgreement")
    version = (rows[0].get("version") if rows else 0) or 1
    return respond(uid, more={"guildClosedChatAgreement": [{"userId": uid, "version": version,
                                                            "lastAgreeAt": now()}]})


def ep_chat_send(uid, req):
    """{"message", "fixedMessageId", "battleLogType", "battleLog", "stampId"}"""
    g = guild_of(uid)
    if not g:
        return {"code": ERR_NOT_MEMBER}
    req = req or {}
    msg = {"id": _next_id(g, "chat"), "speaker": uid, "text": str(req.get("message") or "")[:500], "at": now(),
           "fixedMessageId": _int(req, "fixedMessageId"), "stampId": _int(req, "stampId"),
           "battleLogType": _int(req, "battleLogType"), "battleLogId": 0}
    if req.get("battleLog") and len(json.dumps(req["battleLog"])) <= 64 * 1024:
        msg["battleLogId"] = _next_id(g, "battleLog")
        g.setdefault("battleLogs", {})[str(msg["battleLogId"])] = req["battleLog"]
        g["battleLogs"] = dict(list(g["battleLogs"].items())[-50:])
    g.setdefault("chat", []).append(msg)
    g["chat"] = g["chat"][-KEEP_MESSAGES:]
    save(g)
    return respond(uid)


def ep_battle_log(uid, req):
    """{"battleLogId", "speakerUserId"}: the shared battle log, as the viewer's UserBattleLog."""
    g = guild_of(uid)
    log = (g or {}).get("battleLogs", {}).get(str(_int(req, "battleLogId")))
    if not log:
        return {"code": 28122818}
    current = (userdata.table(uid, "battleLog") or [{}])[0]
    row = dict(current, **log) if isinstance(log, dict) else current
    return userdata.set_tables(uid, {"battleLog": [row]})


def ep_board_send(uid, req):
    """{"message", "guildId"}: officers only."""
    g = guild_of(uid)
    if not g or not is_officer(g, uid):
        return {"code": ERR_NOT_MEMBER}
    g.setdefault("board", []).append({"id": _next_id(g, "board"), "speaker": uid,
                                      "text": str((req or {}).get("message") or ""), "at": now(), "type": 0,
                                      "parameterA": ""})
    g["board"] = g["board"][-KEEP_MESSAGES:]
    save(g)
    return respond(uid)


def ep_board_delete(uid, req):
    g = guild_of(uid)
    bid = _int(req, "deletedBoardId") or _int(req, "boardId")
    if g and is_officer(g, uid):
        for b in g.get("board", []):
            if b["id"] == bid:
                b["deleted"] = True
                b["at"] = now()
        save(g)
    return respond(uid)


def ep_refresh(uid, req):
    return respond(uid)


def ep_mission_refresh(uid, req):
    return respond(uid)


def _mission(g, req):
    idx, mid = _int(req, "index"), _int(req, "guildMissionId")
    return next((m for m in g.get("missions", []) if m["index"] == idx and m["guildMissionId"] == mid), None)


def ep_mission_accept(uid, req):
    """{"guildId", "index", "guildMissionId"}: take the mission; its UserMission row starts (state 2)."""
    g = guild_of(uid)
    if not g:
        return {"code": ERR_NOT_MEMBER}
    missions_today(g)
    m = _mission(g, req)
    if not m:
        return {"code": ERR_MISSION_GONE}
    if m["state"] != MISSION_OPEN:
        return {"code": ERR_MISSION_TAKEN}
    m.update(chargeUserId=uid, state=MISSION_ACCEPTED, acceptedAt=now())
    save(g)
    mission_id = master.GUILD["missions"].get(str(m["guildMissionId"]), {}).get("missionId")
    rows = [dict(r) for r in userdata.table(uid, "mission") if r.get("missionId") != mission_id]
    rows.append({"userId": uid, "missionId": mission_id, "progress": 0, "state": 2, "readState": 0,
                 "completedAt": 0, "isNotifiable": False, "progressUpdatedAt": now()})
    missions.guild_accepted(uid, mission_id)
    return respond(uid, more={"mission": rows})


def ep_mission_cancel(uid, req):
    g = guild_of(uid)
    if not g:
        return {"code": ERR_NOT_MEMBER}
    m = _mission(g, req)
    if not m:
        return {"code": ERR_MISSION_GONE}
    if m["chargeUserId"] != uid:
        return {"code": ERR_MISSION_OTHER}
    m.update(chargeUserId=0, state=MISSION_OPEN, acceptedAt=0)
    save(g)
    mission_id = master.GUILD["missions"].get(str(m["guildMissionId"]), {}).get("missionId")
    rows = [r for r in userdata.table(uid, "mission") if r.get("missionId") != mission_id]
    return respond(uid, more={"mission": rows})


ENDPOINTS = {
    "/guild/top": ep_top,
    "/guild/form": ep_form,
    "/guild/info/update": ep_info_update,
    "/guild/search": ep_search,
    "/guild/search/recommend": ep_search_recommend,
    "/guild/search/friend": ep_search_friend,
    "/guild/join/request": ep_join_request,
    "/guild/join/request/cancel": ep_join_request_cancel,
    "/guild/join/request/list": ep_join_request_list,
    "/guild/join/request/accept": ep_join_request_accept,
    "/guild/join/request/reject": ep_join_request_reject,
    "/guild/leave": ep_leave,
    "/guild/force/kick": ep_force_kick,
    "/guild/role/update": ep_role_update,
    "/guild/invite": ep_invite,
    "/guild/invite/cancel": ep_invite_cancel,
    "/guild/invite/accept": ep_invite_accept,
    "/guild/invite/reject": ep_invite_reject,
    "/guild/invite/list": ep_invite_list,
    "/guild/invite/member/list": ep_invite_member_list,
    "/guild/invite/recommend/list": ep_invite_recommend_list,
    "/guild/agreement": ep_agreement,
    "/guild/chat/send": ep_chat_send,
    "/guild/chat/receive": ep_refresh,
    "/guild/chat/refresh": ep_refresh,
    "/guild/board/send": ep_board_send,
    "/guild/board/delete": ep_board_delete,
    "/guild/board/receive": ep_refresh,
    "/guild/board/refresh": ep_refresh,
    "/guild/timeline/receive": ep_refresh,
    "/guild/battle/log": ep_battle_log,
    "/guild/mission/refresh": ep_mission_refresh,
    "/guild/mission/accept": ep_mission_accept,
    "/guild/mission/cancel": ep_mission_cancel,
}
