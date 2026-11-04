"""Guilds with several players (what can't be tried alone in game). Request keys from docs/api_routes.json."""
import guild as G


def _form(h, leader, approval=1):
    leader.call("guild/agreement")
    leader.call("guild/form", {"name": "Test", "comment": "salut", "atmosphereType": 1, "iconId": 0,
                               "isRequiredApproval": approval})
    g = leader.row("guild")
    h.expect(g.get("guildId") and g.get("leaderUserId") == leader.uid, f"guild/form : {g}")
    return g["guildId"]


def test_form_join_chat(h):
    a, b = h.new_player(), h.new_player()
    gid = _form(h, a)
    b.call("guild/search", {"guildId": gid})
    b.call("guild/join/request", {"guildId": gid})
    h.expect(b.t("guildJoinRequest"), "la demande d'adhésion doit apparaître chez le demandeur")
    b.call("guild/join/request", {"guildId": gid}, expect_code=G.ERR_ALREADY_REQUESTED)
    a.call("guild/join/request/list", {"guildId": gid})
    h.expect(any(r["requestUserId"] == b.uid for r in a.t("guildJoinRequestReceive")), "le chef doit voir la demande")
    a.call("guild/join/request/accept", {"requestUserId": b.uid})
    h.expect(a.row("guild").get("memberCount") == 2, f"memberCount {a.row('guild').get('memberCount')}, 2 attendu")
    b.call("guild/top")
    h.expect(b.row("guild").get("guildId") == gid, "le nouveau membre doit voir sa guilde")
    # the client shows the guild menu (not the search) from UserInfo.guildId
    h.expect(a.row("info").get("guildId") == gid, f"info.guildId du chef : {a.row('info').get('guildId')}, {gid} attendu")
    h.expect(b.row("info").get("guildId") == gid, f"info.guildId du membre : {b.row('info').get('guildId')}, {gid} attendu")
    b.call("guild/chat/send", {"message": "bonjour", "fixedMessageId": 0, "battleLogType": 0, "battleLog": "",
                               "stampId": 0})
    a.call("guild/chat/refresh")
    h.expect(any(m["text"] == "bonjour" for m in a.t("guildChat")), "le chef doit recevoir le message du membre")
    h.expect(a.row("guildNotification").get("hasNotReadChat") is True, "notification de chat non lu attendue")
    a.call("guild/board/send", {"message": "annonce", "guildId": gid})
    b.call("guild/board/refresh")
    h.expect(any(m["text"] == "annonce" for m in b.t("guildBoard")), "le membre doit voir l'annonce du tableau")
    b.call("guild/timeline/receive", {"guildId": gid, "thresholdTimelineId": 0, "getCount": 20})
    h.expect(b.t("guildTimeLine"), "l'historique de guilde ne doit pas être vide")


def test_direct_join_invite_roles_leave(h):
    a, b, c = h.new_player(), h.new_player(), h.new_player()
    gid = _form(h, a, approval=0)
    b.call("guild/join/request", {"guildId": gid})
    h.expect(b.row("guild").get("guildId") == gid, "sans approbation, l'adhésion doit être directe")
    a.call("guild/invite/recommend/list")
    a.call("guild/invite", {"invitedUserId": c.uid})
    c.call("guild/invite/list")
    h.expect(any(r["inviteUserId"] == a.uid for r in c.t("guildMemberInviteReceive")), "l'invité doit voir l'invitation")
    c.call("guild/invite/accept", {"guildId": gid})
    a.call("guild/role/update", {"memberUserId": b.uid, "role": 2})
    h.expect(a.row("guild").get("subLeaderUserId") == b.uid, "b doit être sous-chef")
    b.call("guild/force/kick", {"kickedUserId": a.uid}, expect_code=G.ERR_KICK_LEADER)
    a.call("guild/force/kick", {"kickedUserId": c.uid})
    c.call("guild/top")
    h.expect(not c.row("guild").get("guildId"), "le membre exclu ne doit plus avoir de guilde")
    h.expect(c.row("info").get("guildId") == 0, "info.guildId du membre exclu doit revenir à 0")
    a.call("guild/leave")
    b.call("guild/top")
    h.expect(b.row("guild").get("leaderUserId") == b.uid, "au départ du chef, le sous-chef doit devenir chef")
    b.call("guild/leave")
    h.expect(b.row("info").get("guildId") == 0, "info.guildId doit revenir à 0 après le départ")
    a.call("guild/search", {"guildId": gid}, expect_code=G.ERR_DISBANDED)  # last member left: guild deleted


def test_guild_missions(h):
    a = h.new_player()
    gid = _form(h, a)
    a.call("guild/mission/refresh", {"guildId": gid})
    missions = a.t("guildMission")
    h.expect(missions, "guild/mission/refresh doit tirer les missions du jour")
    m = missions[0]
    a.call("guild/mission/accept", {"guildId": gid, "index": m["index"], "guildMissionId": m["guildMissionId"]})
    mine = next(r for r in a.t("guildMission") if r["index"] == m["index"])
    h.expect(mine["chargeUserId"] == a.uid, "la mission acceptée doit être attribuée au joueur")
    a.call("guild/mission/cancel", {"guildId": gid, "index": m["index"], "guildMissionId": m["guildMissionId"]})


def test_invite_reject_and_cancel(h):
    a, b = h.new_player(), h.new_player()
    gid = _form(h, a)
    a.call("guild/invite", {"invitedUserId": b.uid})
    a.call("guild/invite/member/list", {"guildId": gid})
    a.call("guild/invite/cancel", {"invitedUserId": b.uid})
    b.call("guild/invite/accept", {"guildId": gid}, expect_code=G.ERR_INVITE_GONE)
    a.call("guild/invite", {"invitedUserId": b.uid})
    b.call("guild/invite/reject", {"guildId": gid})
    b.call("guild/join/request", {"guildId": gid})
    b.call("guild/join/request/cancel", {"guildId": gid})
    a.call("guild/join/request/accept", {"requestUserId": b.uid}, expect_code=G.ERR_REQUEST_GONE)


def test_invites_received_show_the_guild(h):
    """The invitation screen reads the inviting guild's UserGuild row: without it, opening the invitations crashed
    the game (Discord 2026-10-30). Same for a pending join request."""
    a, b, c = h.new_player(), h.new_player(), h.new_player()
    gid = _form(h, a)
    a.call("guild/invite", {"invitedUserId": b.uid})
    b.call("guild/invite/list")
    h.expect(any(r.get("guildId") == gid for r in b.t("guildMemberInviteReceive")), "invitation reçue absente")
    h.expect(any(r.get("guildId") == gid for r in b.t("guild")), f"guilde invitante absente de UserGuild : {b.t('guild')}")
    h.expect(any(r.get("guestUserId") == a.uid for r in b.t("guest")), "profil de l'inviteur absent")
    h.expect(b.row("info").get("guildId", 0) == 0, "une invitation ne doit pas rendre membre")
    c.call("guild/join/request", {"guildId": gid})
    h.expect(any(r.get("guildId") == gid for r in c.t("guild")), "guilde demandée absente de UserGuild")
    b.call("guild/invite/accept", {"guildId": gid})
    h.expect(b.row("info").get("guildId") == gid, "invitation acceptée : membre")


def test_other_guild_routes(h):
    a, b = h.new_player(), h.new_player()
    gid = _form(h, a)
    a.call("guild/info/update", {"name": "Test 2", "comment": "maj", "atmosphereType": 2, "iconId": 0,
                                 "isRequiredApproval": 1, "raidEventDefeatPointBonusHour": 20})
    h.expect(a.row("guild").get("name") == "Test 2", "guild/info/update n'a pas renommé la guilde")
    b.call("guild/search/recommend")
    b.call("guild/search/friend")
    b.call("guild/join/request", {"guildId": gid})
    a.call("guild/join/request/reject", {"requestUserId": b.uid})
    b.call("guild/top")
    h.expect(not b.row("guild").get("guildId") or b.row("guild").get("guildId") != gid, "demande refusée : pas membre")
    a.call("guild/chat/send", {"message": "x", "fixedMessageId": 0, "battleLogType": 0, "battleLog": "", "stampId": 0})
    a.call("guild/chat/receive", {"thresholdChatId": 0, "getCount": 20})
    a.call("guild/board/send", {"message": "y", "guildId": gid})
    board = a.t("guildBoard")
    a.call("guild/board/receive", {"guildId": gid, "thresholdBoardId": 0, "getCount": 20})
    a.call("guild/board/delete", {"guildId": gid, "boardId": board[-1]["id"] if board else 0})
    a.call("guild/battle/log", {"battleLogId": 999, "speakerUserId": a.uid}, expect_code=28122818)
    shared = {"battleLogId": 1, "battleLogType": 1, "unitLog": []}  # shape guessed: no real trace yet
    b.call("guild/chat/send", {"message": "", "fixedMessageId": 0, "battleLogType": 1, "battleLog": shared,
                               "stampId": 0}, expect_code=28122813)  # b was refused: not a member
    a.call("guild/chat/send", {"message": "", "fixedMessageId": 0, "battleLogType": 1, "battleLog": shared,
                               "stampId": 0})
    log_id = a.t("guildChat")[-1]["battleLogId"]
    h.expect(log_id, "un message avec combat doit avoir un battleLogId")
    a.call("guild/battle/log", {"battleLogId": log_id, "speakerUserId": a.uid})


def test_guild_mission_counts_runs(h):
    """A taken guild mission counts the runs made after it was taken (they stayed at 0/N, シンキラ 2026-10-21)."""
    import master
    a = h.new_player()
    gid = _form(h, a)
    a.call("guild/mission/refresh", {"guildId": gid})
    gm = next(k for k, v in master.GUILD["missions"].items()
              if master.MISSIONS.get(str(v["missionId"]), [0])[0] == 1008)  # "complete the main quest N times"
    target = master.MISSIONS[str(master.GUILD["missions"][gm]["missionId"])][2]
    g = G.load(gid)
    g["missions"][0].update(guildMissionId=int(gm), chargeUserId=0, state=G.MISSION_OPEN)
    G.save(g)
    a.call("quest/start", {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001,
                           "supporterPcId": 0, "supporterPcSlotId": 0, "supporterUserId": 0})
    a.call("quest/clear")  # before the mission is taken: does not count
    a.call("guild/mission/accept", {"guildId": gid, "index": 0, "guildMissionId": int(gm)})
    for _ in range(target):
        a.call("quest/start", {"boostItemIds": [], "eventId": 0, "level": 1, "partyId": 1, "questId": 8011000001,
                               "supporterPcId": 0, "supporterPcSlotId": 0, "supporterUserId": 0})
        a.call("quest/clear")
    a.call("guild/mission/refresh", {"guildId": gid})
    mine = next(r for r in a.t("guildMission") if r["index"] == 0)
    h.expect(mine["state"] == G.MISSION_DONE, f"mission de guilde après {target} quêtes principales : {mine}")
