"""Admin web panel (English UI), served on its own port (SAOUB_ADMIN_PORT, default 8081) inside the game server process.

On a hosted server it can be reached through a reverse proxy at https://<host>/saoub-admin/ (SAOUB_ADMIN_BASE); the port itself is not
published. Login with SAOUB_ADMIN_PASSWORD (panel disabled when unset), session cookie + per-session CSRF token,
login attempts rate-limited per IP. Every edit of an account first stores a snapshot of it (db.snapshots), which the
player page can restore.

Also renders the in-game announcement pages (/announcement, /announcement/<id>): the client opens them in a WebView.
"""
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import api
import config
import db
import master
import userdata

BASE = os.environ.get("SAOUB_ADMIN_BASE", "/saoub-admin").rstrip("/")
PASSWORD = os.environ.get("SAOUB_ADMIN_PASSWORD", "")
PORT = int(os.environ.get("SAOUB_ADMIN_PORT", "8081"))
HOST = os.environ.get("SAOUB_ADMIN_HOST", "127.0.0.1")
SESSION_SECONDS = 12 * 3600
COOKIE = "saoub_admin"

_sessions = {}  # token -> {"expires", "csrf"}
_failures = {}  # ip -> [timestamps of failed logins]
MAX_FAILURES, FAILURE_WINDOW = 5, 600

# gift box reasons (reward.bin sub 2, Japanese in the data; the game shows its own translation)
REASONS_EN = {
    "400100090000": "Gift from the team", "400100090001": "Apology for a bug", "400100090004": "Apology",
    "400100090005": "Apology for the maintenance", "400100090002": "Compensation for an outage",
    "400100090012": "Thanks for the update", "400100090019": "Campaign reward",
    "400100000010": "Event reward", "400100002001": "Mission reward",
    "400100001001": "Login bonus", "400100004001": "Obtained from a scout",
}
DEFAULT_REASON = "400100090000"
REWARD_TYPES = {1: "Item", 2: "Equipment", 3: "Diamond Cubes", 4: "Gold (Col)", 5: "Character"}
GOLD_ITEM = 205000001


def apply_settings():
    """Settings changed from the panel survive restarts."""
    bonus = db.get_setting("lottery_step_bonus")
    if bonus is not None:
        config.LOTTERY_STEP_BONUS = bool(bonus)
    config.set_raid_mode(bool(db.get_setting("raid_mode")))
    window = db.get_setting("game_window")
    if window:
        try:
            config.set_game_window(window)
        except ValueError:
            pass  # a stored value the parser refuses: the default window stays


JST = timezone(timedelta(hours=9))


def parse_game_window(start: str, end: str) -> str:
    """Two datetime-local values of the panel (Japan time, the game's) -> "<start>/<end>" ISO 8601."""
    try:
        a, b = (datetime.fromisoformat(v.strip()) for v in (start, end))
    except ValueError:
        raise ValueError("invalid dates (format YYYY-MM-DD HH:MM)")
    a, b = (d.replace(tzinfo=JST) if d.tzinfo is None else d for d in (a, b))
    if b <= a:
        raise ValueError("the end must be after the start")
    return f"{a.isoformat()}/{b.isoformat()}"


def maintenance_json() -> dict:
    """maintenance.json for the client (same shape as the original bucket file)."""
    m = db.get_setting("maintenance") or {}
    if not m.get("enabled"):
        return {"valid": False, "opened_at": 0, "closed_at": 0, "detail": "", "finished_at": 0}
    now = int(time.time())
    end = int(m.get("until") or now + 30 * 86400)
    return {"valid": True, "opened_at": int(m.get("since") or now - 60), "closed_at": end,
            "detail": m.get("message", ""), "finished_at": end}


# ---------------------------------------------------------------- in-game announcement pages

GAME_CSS = """
table{width:100%;border-collapse:collapse;margin:0 0 14px}td{padding:4px 6px;border-bottom:1px solid #2a4566}
td.r{text-align:right;white-space:nowrap}h2{font-size:15px;margin:16px 0 6px;color:#ffd36a}
body{margin:0;background:#0d1626;color:#e8eef8;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 16px;background:linear-gradient(90deg,#16304f,#0d1626);font-weight:700;font-size:17px;
letter-spacing:.04em;border-bottom:2px solid #3f8fd8}
main{padding:12px 16px 40px}
a.card{display:block;text-decoration:none;color:inherit;background:#15243a;border:1px solid #2a4566;border-radius:6px;
padding:12px 14px;margin:0 0 10px}
.date{color:#8fb3d9;font-size:12px}
h1{font-size:18px;margin:4px 0 10px}
img{max-width:100%;border-radius:4px;margin:8px 0}
.back{display:inline-block;margin-bottom:12px;color:#7cc0ff}
.empty{color:#8fb3d9;text-align:center;margin-top:40px}
"""


def _render_text(text: str) -> str:
    """Escaped text with line breaks, **bold** and bare https links (no raw HTML from the panel)."""
    out = html.escape(text)
    parts = out.split("**")
    out = "".join(p if i % 2 == 0 else f"<b>{p}</b>" for i, p in enumerate(parts))
    words = []
    for w in out.split(" "):
        if w.startswith("https://"):
            w = f'<a href="{w}" style="color:#7cc0ff">{w}</a>'
        words.append(w)
    return " ".join(words).replace("\n", "<br>")


def _game_doc(title: str, body: str, header: str = "NEWS") -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f"<style>{GAME_CSS}</style></head><body><header>{html.escape(header)}</header><main>{body}</main></body></html>").encode()


def _date(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or 0))


def lottery_page(lottery_id: int):
    """Scout details / rates page (the banner's "Details" and "Rates" buttons open /announcement/<lotteryId>00 and
    <lotteryId>08), or None when the id is not a scout. They showed the news list instead: the patch notes
    (tester report, Discord 2026-10-28). Built from the game data: headline, rate per rarity, rate of each item."""
    price = master.LOTTERY["prices"].get(str(lottery_id))
    if price and str(lottery_id) not in master.LOTTERY["rates"]:  # a price id: its scout
        lottery_id = int(price["lottery"])
    rates = master.LOTTERY["rates"].get(str(lottery_id))
    pool = master.LOTTERY["items"].get(str(lottery_id))
    if not rates or not pool:
        return None
    title = next((b[2] or b[1] for banners in master.BANNERS.values() for b in banners if b[0] == lottery_id), "")
    title = title or "Scout"
    total = sum(w for _, w in rates) or 1
    sections = []
    for rarity, weight in sorted(rates, reverse=True):
        items = [x for x in pool if x["rarity"] == rarity and x["weight"]]
        if not weight or not items:
            continue
        share = weight / total
        inner = sum(x["weight"] for x in items) or 1
        lines = "".join(
            f'<tr><td>{html.escape(reward_name(x["rewardType"], x["target"]) or str(x["target"]))}</td>'
            f'<td class="r">{share * x["weight"] / inner * 100:.3f}%</td></tr>'
            for x in sorted(items, key=lambda x: -x["weight"]))
        sections.append(f'<h2>★{rarity + 1} — {share * 100:.2f}%</h2><table>{lines}</table>')
    body = (f'<h1>{html.escape(title.split("!")[0] + ("!" if "!" in title else ""))}</h1>'
            f'<p>{html.escape(title)}</p>' + "".join(sections))
    return _game_doc(title[:60], body, "SCOUT DETAILS")


def game_page(path: str) -> bytes:
    """/announcement (list) or /announcement/<id> (detail); a scout's /announcement/<lotteryId>00 or 08 (details,
    rates) shows that scout; other unknown ids show the list."""
    rest = path[len("/announcement"):].strip("/")
    if rest.isdigit() and len(rest) > 2 and rest[-2:] in ("00", "08"):
        page = lottery_page(int(rest[:-2]))
        if page:
            return page
    ann = db.get_announcement(int(rest)) if rest.isdigit() else None
    if ann and ann["visible"]:
        img = f'<img src="{html.escape(ann["image_url"])}" alt="">' if ann["image_url"] else ""
        body = (f'<a class="back" href="/announcement">‹ Back</a><div class="date">{_date(ann["created_at"])}</div>'
                f'<h1>{html.escape(ann["title"])}</h1>{img}<div>{_render_text(ann["body"])}</div>')
        return _game_doc(ann["title"], body)
    items = db.list_announcements(visible_only=True)
    if not items:
        return _game_doc("News", '<p class="empty">No news for now.</p>')
    cards = "".join(f'<a class="card" href="/announcement/{a["id"]}"><div class="date">{_date(a["created_at"])}</div>'
                    f'<div>{html.escape(a["title"])}</div></a>' for a in items)
    return _game_doc("News", cards)


# ---------------------------------------------------------------- panel helpers

CSS = """
:root{--bg:#f4f6fa;--fg:#1b2330;--card:#fff;--line:#d9dee8;--muted:#667085;--acc:#2f6fd6;--bad:#c4372c;--ok:#1f8a4c}
@media (prefers-color-scheme:dark){:root{--bg:#11151c;--fg:#e6e9ef;--card:#1a2029;--line:#2c3440;--muted:#98a2b3;
--acc:#6aa0ff;--bad:#ff7b6e;--ok:#4cc483}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif}
nav{display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center;padding:10px 16px;background:var(--card);
border-bottom:1px solid var(--line)}nav b{margin-right:8px}nav a{color:var(--fg);text-decoration:none}
nav a.on{color:var(--acc);font-weight:600}nav form{margin-left:auto}
main{max-width:1150px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 14px}
h1{font-size:20px;margin:4px 0 14px}h2{font-size:15px;margin:0 0 10px}
table{border-collapse:collapse;width:100%}td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;
vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.stats{display:flex;flex-wrap:wrap;gap:12px}.stat{flex:1 1 140px}.stat .n{font-size:24px;font-weight:700}
.muted{color:var(--muted)}a{color:var(--acc)}
input,select,textarea{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:5px;background:var(--bg);
color:var(--fg);max-width:100%}textarea{width:100%;min-height:120px}
button{font:inherit;padding:5px 12px;border:0;border-radius:5px;background:var(--acc);color:#fff;cursor:pointer}
button.bad{background:var(--bad)}button.ghost{background:transparent;color:var(--acc);border:1px solid var(--line)}
form.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}
.flash{padding:9px 12px;border-radius:6px;margin:0 0 14px;background:var(--card);border-left:4px solid var(--ok)}
.flash.err{border-left-color:var(--bad)}pre{white-space:pre-wrap;word-break:break-all;font-size:12px;margin:0;
max-height:480px;overflow:auto}.tag{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--line)}
.scroll{max-height:420px;overflow:auto}
"""

NAV = [("", "Dashboard"), ("players", "Players"), ("gifts", "Gifts"), ("announcements", "Announcements"),
       ("banners", "Banners"), ("server", "Server")]


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def ts(t) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(t)) if t else "—"


class Ctx:
    def __init__(self, session, flash=None):
        self.session, self.flash = session, flash

    def csrf(self) -> str:
        return f'<input type="hidden" name="csrf" value="{self.session["csrf"]}">'

    def action(self, name: str, fields: str, label: str, cls="", confirm="") -> str:
        onsubmit = f' onsubmit="return confirm({json.dumps(confirm)})"' if confirm else ""
        return (f'<form class="row" method="post" action="{BASE}/action/{name}"{onsubmit}>{self.csrf()}{fields}'
                f'<button class="{cls}">{esc(label)}</button></form>')


def page(ctx: Ctx, current: str, title: str, content: str) -> bytes:
    links = "".join(f'<a class="{"on" if current == p else ""}" href="{BASE}/{p}">{esc(t)}</a>' for p, t in NAV)
    flash = ""
    if ctx.flash:
        kind, msg = ctx.flash
        flash = f'<div class="flash {"err" if kind == "err" else ""}">{esc(msg)}</div>'
    logout = f'<form method="post" action="{BASE}/logout">{ctx.csrf()}<button class="ghost">Log out</button></form>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{esc(title)} · SAOUB admin</title>'
            f"<style>{CSS}</style></head><body><nav><b>SAOUB admin</b>{links}{logout}</nav><main>{flash}"
            f"<h1>{esc(title)}</h1>{content}</main>{AUTOCOMPLETE_JS}</body></html>").encode()


AUTOCOMPLETE_JS = """<script>
document.querySelectorAll('input[data-names]').forEach(function(inp){
  var list=document.createElement('datalist');list.id='dl'+Math.random().toString(36).slice(2);
  document.body.appendChild(list);inp.setAttribute('list',list.id);var t;
  inp.addEventListener('input',function(){clearTimeout(t);t=setTimeout(function(){
    if(inp.value.length<2)return;
    fetch('""" + BASE + """/names?kind='+inp.dataset.names+'&q='+encodeURIComponent(inp.value))
      .then(function(r){return r.json()}).then(function(rows){list.innerHTML='';rows.forEach(function(r){
        var o=document.createElement('option');o.value=r[0];o.label=r[1];list.appendChild(o);});});},200);});
});
</script>"""


def reason_options() -> str:
    """<option>s of the gift reasons: the translated ones first, the team gift selected."""
    known = master.NAMES["reasons"]
    order = list(REASONS_EN) + sorted(k for k in known if k not in REASONS_EN)
    return "".join(f'<option value="{k}"{" selected" if k == DEFAULT_REASON else ""}>'
                   f'{esc(REASONS_EN.get(k, ""))}{" — " if k in REASONS_EN else ""}{esc(known.get(k, ""))}</option>'
                   for k in order if k in known)


def name_of(kind: str, ident) -> str:
    return master.NAMES.get(kind, {}).get(str(ident), "")


def reward_name(reward_type: int, target) -> str:
    kind = {1: "items", 2: "equipment", 5: "pcs"}.get(int(reward_type))
    if int(reward_type) == 3:
        return "Diamond Cubes"
    if int(reward_type) == 4:
        return "Gold (Col)"
    return name_of(kind, target) if kind else ""


def player_summary(uid: int) -> dict:
    data = userdata.load(uid)
    info = (data["tables"].get("info") or [{}])[0]
    return {"uid": uid, "name": info.get("name", ""), "rank": master.player_rank(info.get("totalExp", 0)),
            "gems": userdata.gems(uid), "updated": data.get("updated_at", 0), "pcs": len(data["tables"].get("pc", [])),
            "seeded": data.get("seeded_at", 0)}


def snapshot(uid: int, label: str):
    db.add_snapshot(uid, label, userdata.load(uid))


def active_since(seconds: int) -> list:
    now = time.time()
    return [u for u in db.user_ids() if now - userdata.load(u).get("updated_at", 0) < seconds]


def tail(path: str, lines: int) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400 * lines))
        return f.read().decode("utf-8", "replace").splitlines()[-lines:]


# ---------------------------------------------------------------- pages

def p_dashboard(ctx, q):
    users = db.user_ids()
    m = db.get_setting("maintenance") or {}
    stats = [("Accounts", len(users)), ("Active (15 min)", len(active_since(900))), ("Active (24 h)", len(active_since(86400))),
             ("Guilds", len(db.list_guilds())), ("Visible announcements", len(db.list_announcements(True)))]
    cards = "".join(f'<div class="card stat"><div class="muted">{esc(k)}</div><div class="n">{esc(v)}</div></div>'
                    for k, v in stats)
    game_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(config.now()))
    state = ('<span class="tag" style="color:var(--bad)">MAINTENANCE ACTIVE</span>' if m.get("enabled")
             else '<span class="tag">open</span>')
    recent = sorted((player_summary(u) for u in users), key=lambda p: -p["updated"])[:10]
    rows = "".join(f'<tr><td><a href="{BASE}/player/{p["uid"]}">{p["uid"]}</a></td><td>{esc(p["name"])}</td>'
                   f'<td>{esc(p["rank"])}</td><td>{ts(p["updated"])}</td></tr>' for p in recent)
    errors = "".join(f"<tr><td>{ts(e.get('t'))}</td><td>{esc(e.get('user'))}</td><td>{esc(json.dumps(e.get('log'), ensure_ascii=False))[:160]}</td></tr>"
                     for e in reversed(_client_errors(8)))
    return page(ctx, "", "Dashboard", f"""
<div class="stats">{cards}</div>
<div class="grid">
<div class="card"><h2>Server</h2><table>
<tr><td>State</td><td>{state}</td></tr>
<tr><td>Game clock</td><td>{game_time}</td></tr>
<tr><td>Scout steps</td><td>{"on" if config.LOTTERY_STEP_BONUS else "off"}</td></tr>
<tr><td>Asset version</td><td class="muted">{esc(getattr(_store(), "version", "?"))}</td></tr></table></div>
<div class="card"><h2>Last active players</h2><table><tr><th>Id</th><th>Name</th><th>Rank</th><th>Last sync</th></tr>{rows}</table></div>
</div>
<div class="card"><h2>Last errors reported by the clients</h2><table>{errors or '<tr><td class="muted">none</td></tr>'}</table></div>""")


def _client_errors(n):
    out = []
    for line in tail(os.path.join(config.LOG_DIR, "client_log.jsonl"), n):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _store():
    import sys  # noqa: PLC0415
    main = sys.modules.get("__main__")
    return getattr(main, "STORE", None)


def p_players(ctx, q):
    search = (q.get("q") or [""])[0].strip().lower()
    devices = {}
    for d in db.list_devices():
        if d.get("x_uid"):
            devices.setdefault(int(d["x_uid"]), []).append(d)
    rows = []
    for p in sorted((player_summary(u) for u in db.user_ids()), key=lambda p: -p["uid"]):
        if search and search not in str(p["uid"]) and search not in p["name"].lower():
            continue
        rows.append(f'<tr><td><a href="{BASE}/player/{p["uid"]}">{p["uid"]}</a></td><td>{esc(p["name"])}</td>'
                    f'<td>{esc(p["rank"])}</td><td>{p["pcs"]}</td><td>{esc(p["gems"])}</td><td>{len(devices.get(p["uid"], []))}</td>'
                    f'<td>{ts(p["updated"])}</td></tr>')
    return page(ctx, "players", "Players", f"""
<form class="row" method="get" action="{BASE}/players"><input name="q" value="{esc(search)}" placeholder="id or name">
<button>Search</button></form>
<div class="card scroll"><table><tr><th>Id</th><th>Name</th><th>Rank</th><th>Characters</th><th>Cubes</th><th>Devices</th>
<th>Last sync</th></tr>{''.join(rows)}</table></div>""")


def p_player(ctx, q, uid):
    if not db.get_user(uid):
        return page(ctx, "players", "Player not found", f'<p>No account {uid}.</p>')
    p = player_summary(uid)
    t = userdata.load(uid)["tables"]
    info = (t.get("info") or [{}])[0]
    status = (t.get("questStatus") or [{}])[0]
    in_quest = status.get("type") == 1
    a = ctx.action
    hid = f'<input type="hidden" name="uid" value="{uid}">'
    pcs = "".join(f'<tr><td>{esc(r["pcId"])}</td><td>{esc(name_of("pcs", r["pcId"]))}</td><td>{esc(master.pc_level(r.get("totalExp", 0)))}</td>'
                  f'<td>{esc(r.get("hp"))}/{esc(r.get("mp"))}</td><td>{esc(r.get("weaponEquipmentStockId") or "—")}</td></tr>'
                  for r in t.get("pc", []))
    items = "".join(f'<tr><td>{esc(r["itemId"])}</td><td>{esc(name_of("items", r["itemId"]))}</td><td>{esc(r.get("amount"))}</td></tr>'
                    for r in sorted(t.get("item", []), key=lambda r: r["itemId"]))
    stock = "".join(f'<tr><td>{esc(r["id"])}</td><td>{esc(r["equipmentId"])}</td><td>{esc(name_of("equipment", r["equipmentId"]))}</td></tr>'
                    for r in t.get("equipmentStock", []))
    gifts = "".join(f'<tr><td>{esc(g["id"])}</td><td>{esc(REWARD_TYPES.get(g.get("rewardType"), g.get("rewardType")))}</td>'
                    f'<td>{esc(reward_name(g.get("rewardType", 0), g.get("itemId")))} ×{esc(g.get("amount"))}</td>'
                    f'<td>{"opened" if g.get("state") else "<b>not opened</b>"}</td><td>'
                    + ("" if g.get("state") else a("gift_delete", hid + f'<input type="hidden" name="gift" value="{esc(g["id"])}">',
                                                   "Delete", "ghost", "Delete this unopened gift?"))
                    + "</td></tr>"
                    for g in sorted(t.get("gift", []), key=lambda g: -g["id"])[:30])
    devs = "".join(f'<tr><td>{esc(d["uuid"])}</td><td>{ts(d.get("created_at"))}</td></tr>'
                   for d in db.list_devices() if d.get("x_uid") == str(uid))
    snaps = "".join(f'<tr><td>{ts(s["created_at"])}</td><td>{esc(s["label"])}</td><td>'
                    + a("restore", hid + f'<input type="hidden" name="snapshot" value="{s["id"]}">', "Restore", "ghost",
                        "Put the account back in this state? (the current state is saved first)") + "</td></tr>"
                    for s in db.list_snapshots(uid))
    reasons = reason_options()
    types = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in REWARD_TYPES.items())
    quest_line = (f'in quest <b>{esc(status.get("questId"))}</b> (stage {esc(status.get("stageId"))})' if in_quest
                  else "idle")
    return page(ctx, "players", f"Player {uid} — {p['name']}", f"""
<p class="muted">Changes made here apply at the game's next exchange with the server; to see them at once, the
player restarts the game. Every change first saves the account (see "Account snapshots").</p>
<div class="grid">
<div class="card"><h2>Account</h2><table>
<tr><td>Rank</td><td>{esc(p['rank'])} <span class="muted">(exp {esc(info.get('totalExp', 0))})</span></td></tr>
<tr><td>Diamond Cubes</td><td>{esc(p['gems'])}</td></tr>
<tr><td>Quest</td><td>{quest_line}</td></tr>
<tr><td>Created</td><td>{ts(p['seeded'])}</td></tr><tr><td>Last sync</td><td>{ts(p['updated'])}</td></tr></table>
<br>{a("rename", hid + f'<input name="name" value="{esc(p["name"])}" maxlength="24">', "Rename")}
{a("totalexp", hid + f'<input name="exp" type="number" min="0" value="{esc(info.get("totalExp", 0))}">', "Set player exp")}
{a("retire", hid, "Leave the current quest", "ghost") if in_quest else ""}
{a("heal", hid, "Heal the party (full HP/MP)", "ghost")}
{a("snapshot", hid + '<input name="label" placeholder="snapshot name">', "Snapshot now", "ghost")}</div>
<div class="card"><h2>Give directly</h2>
{a("gems", hid + '<input name="amount" type="number" placeholder="+/- cubes" required>', "Cubes")}
{a("item", hid + '<input name="id" data-names="items" placeholder="item (id or name)" required><input name="amount" type="number" value="1" style="width:90px">', "Item")}
{a("pc", hid + '<input name="id" data-names="pcs" placeholder="character (id or name)" required>', "Character")}
{a("equipment", hid + '<input name="id" data-names="equipment" placeholder="equipment (id or name)" required><input name="amount" type="number" value="1" min="1" max="50" style="width:70px">', "Equipment")}
<p class="muted">A negative amount removes items. A character already owned is not duplicated.</p></div>
<div class="card"><h2>Send a gift (gift box)</h2>
{a("gift", hid + f'<select name="type">{types}</select><input name="id" data-names="items" placeholder="id (not needed for cubes/gold)"><input name="amount" type="number" value="1" min="1" style="width:90px"><select name="reason">{reasons}</select>', "Send")}
<p class="muted">For equipment or a character, enter its id (the autocomplete suggests items; equipment and character
names are in the tables below or on the Gifts page).</p></div>
<div class="card"><h2>Linked devices</h2><table>{devs or '<tr><td class="muted">none</td></tr>'}</table><br>
{a("link", hid + '<input name="uuid" placeholder="GREE device uuid" required size="24">', "Link this device to this account", "", "This device will now play this account. Continue?")}
<p class="muted">Unlinked devices are listed on the Server page.</p></div>
</div>
<div class="grid">
<div class="card scroll"><h2>Characters ({len(t.get('pc', []))})</h2><table><tr><th>Id</th><th>Name</th><th>Lv.</th><th>HP/MP</th><th>Weapon</th></tr>{pcs}</table></div>
<div class="card scroll"><h2>Items ({len(t.get('item', []))})</h2><table><tr><th>Id</th><th>Name</th><th>Qty</th></tr>{items}</table></div>
<div class="card scroll"><h2>Equipment ({len(t.get('equipmentStock', []))})</h2><table><tr><th>No.</th><th>Id</th><th>Name</th></tr>{stock}</table></div>
<div class="card scroll"><h2>Gifts (last 30)</h2><table>{gifts or '<tr><td class="muted">none</td></tr>'}</table></div>
</div>
<div class="card scroll"><h2>Account snapshots</h2><table>{snaps or '<tr><td class="muted">none</td></tr>'}</table></div>""")


def p_gifts(ctx, q):
    a = ctx.action
    reasons = reason_options()
    types = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in REWARD_TYPES.items())
    log = "".join(f'<tr><td>{ts(e["t"])}</td><td>{esc(e["to"])}</td><td>{esc(REWARD_TYPES.get(e["type"]))}</td>'
                  f'<td>{esc(e["name"])} ×{e["amount"]}</td><td>{e["count"]}</td></tr>'
                  for e in reversed(db.get_setting("gift_log", [])))
    return page(ctx, "gifts", "Gifts", f"""
<div class="card"><h2>Send</h2>
{a("gift", '<input name="uid" placeholder="player id, or all" required style="width:170px">'
             f'<select name="type">{types}</select>'
             '<input name="id" data-names="items" placeholder="item / equipment / character id">'
             '<input name="amount" type="number" value="1" min="1" style="width:90px">'
             f'<select name="reason">{reasons}</select>', "Send", "", "Send this gift?")}
<p class="muted">"all" = every account. The gift shows up in the gift box at the next sync (game restart or
back to the home screen). For Diamond Cubes and gold the id is ignored.</p></div>
<div class="grid">
<div class="card"><h2>Find a name</h2>
<form class="row" onsubmit="return false"><select id="k"><option value="items">Items</option><option value="equipment">Equipment</option>
<option value="pcs">Characters</option></select><input id="s" placeholder="name or id"><button onclick="find()">Search</button></form>
<table id="res"></table></div>
<div class="card scroll"><h2>Last gifts sent</h2><table><tr><th>Date</th><th>To</th><th>Type</th><th>What</th><th>Accounts</th></tr>{log}</table></div>
</div>
<script>function find(){{fetch('{BASE}/names?kind='+document.getElementById('k').value+'&q='+encodeURIComponent(document.getElementById('s').value))
.then(function(r){{return r.json()}}).then(function(rows){{document.getElementById('res').innerHTML=rows.map(function(r){{
return '<tr><td>'+r[0]+'</td><td>'+r[1].replace(/</g,'&lt;')+'</td></tr>'}}).join('')}})}}</script>""")


def p_announcements(ctx, q):
    edit_id = int((q.get("edit") or ["0"])[0] or 0)
    ann = db.get_announcement(edit_id) if edit_id else None
    a = ctx.action
    rows = "".join(f'<tr><td>{x["id"]}</td><td>{esc(x["title"])}</td><td>{"visible" if x["visible"] else "hidden"}</td>'
                   f'<td>{ts(x["updated_at"])}</td><td><a href="{BASE}/announcements?edit={x["id"]}">edit</a></td><td>'
                   + a("announcement_delete", f'<input type="hidden" name="id" value="{x["id"]}">', "Delete", "bad",
                       "Delete this announcement?") + "</td></tr>" for x in db.list_announcements())
    form = a("announcement_save",
             f'<input type="hidden" name="id" value="{ann["id"] if ann else ""}">'
             f'<div style="width:100%"><input name="title" placeholder="Title" required style="width:100%" value="{esc(ann["title"] if ann else "")}"></div>'
             f'<div style="width:100%"><input name="image_url" placeholder="Image URL (https://…, optional)" style="width:100%" value="{esc(ann["image_url"] if ann else "")}"></div>'
             f'<textarea name="body" placeholder="Text (line breaks kept, **bold**, https://… links)">{esc(ann["body"] if ann else "")}</textarea>'
             f'<label><input type="checkbox" name="visible" {"checked" if not ann or ann["visible"] else ""}> visible in game</label>',
             "Save" if ann else "Publish")
    return page(ctx, "announcements", "Announcements", f"""
<div class="grid"><div class="card"><h2>{"Edit announcement " + str(ann["id"]) if ann else "New announcement"}</h2>{form}
<p class="muted">Shown in game (the "News" / announcements button, in-game web page), newest first.
<a href="/announcement" target="_blank">Preview</a> (on the game port).</p></div>
<div class="card scroll"><h2>Announcements</h2><table><tr><th>Id</th><th>Title</th><th>State</th><th>Edited</th><th></th><th></th></tr>{rows}</table></div></div>""")


# ---------------------------------------------------------------- banners (release windows)

STATE_LABELS = {"open": "forced open", "closed": "forced closed"}
LOOP_START, LOOP_END = "2022/11/20 06:00:00", "2023/01/16 05:59:59"  # the default game clock window (JST)


def _natural_open(label: str) -> bool:
    """Open at the game clock by its own dates (base.bin, JST), without the panel's choice."""
    import missions  # noqa: PLC0415
    start, end = (master.RELEASES.get(label) or ["", ""])[:2]
    s_ts, e_ts = missions._ts(start), missions._ts(end)
    if label in config.EXTEND_WINDOWS:
        e_ts = missions._ts(LOOP_END)
    now = config.now()
    return (s_ts is None or s_ts <= now) and (e_ts is None or now <= e_ts)


def banner_image(lottery_id: str):
    """PNG bytes of a scout banner's picture (English when the server has it, else Japanese), or None."""
    import base64  # noqa: PLC0415

    import assets  # noqa: PLC0415
    from saoub_crypto import decrypt, hashed_name  # noqa: PLC0415
    if assets.CURRENT is None:
        return None
    for path, key in master.BANNER_IMAGES.get(str(lottery_id), []):
        raw = assets.CURRENT.hashed(hashed_name(path))
        if raw is not None:
            try:
                data = decrypt(raw, base64.b64decode(key))
            except Exception:  # noqa: BLE001
                continue
            if data[:4] == b"\x89PNG":
                return data
    return None


def _state_select(ctx, label: str, current: str) -> str:
    opts = "".join(f'<option value="{v}"{" selected" if v == current else ""}>{t}</option>'
                   for v, t in (("auto", "Auto (game dates)"), ("open", "Enable"), ("closed", "Disable")))
    return ctx.action("release_window", f'<input type="hidden" name="label" value="{esc(label)}">'
                                        f'<select name="state">{opts}</select>', "OK", "ghost")


def p_banners(ctx, q):
    """Scout banners: activate / deactivate them in game. A banner is shown while its release window (base.bin)
    contains the game clock; the panel forces the window open or closed in the served base.bin (rebuilt at once,
    players get it at their next game start)."""
    import missions  # noqa: PLC0415
    forced = db.get_setting("release_windows", {}) or {}
    needle = (q.get("q") or [""])[0].strip().lower()
    which = (q.get("show") or ["current"])[0]
    rows = []
    for label, banners in master.BANNERS.items():
        start, end = (master.RELEASES.get(label) or ["", ""])[:2]
        in_loop = (not start or start <= LOOP_END) and (not end or end >= LOOP_START)
        text = " ".join([label] + [f"{b[0]} {b[1]} {b[2]}" for b in banners]).lower()
        if needle and needle not in text:
            continue
        now_open = missions.is_open(label)
        if not needle and which == "current" and not (in_loop or label in forced or now_open):
            continue
        if not needle and which == "open" and not now_open:
            continue
        rows.append((label in forced, now_open, start or "", label, banners, end or ""))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    body = []
    for is_forced, now_open, start, label, banners, end in rows[:400]:
        state = forced.get(label, "auto")
        pic = next((b[0] for b in banners if str(b[0]) in master.BANNER_IMAGES), None)
        names = (f'<img src="{BASE}/banner_img/{pic}" loading="lazy" alt="" '
                 f'style="display:block;max-width:240px;height:auto;border-radius:6px;margin:0 0 4px">' if pic else "")
        names += "<br>".join(f'{esc(b[1])} <span class="muted">#{b[0]}{(" — " + esc(b[2])) if b[2] else ""}</span>'
                             for b in banners[:3])
        if len(banners) > 3:
            names += f'<br><span class="muted">+{len(banners) - 3} more</span>'
        tag = ('<span class="tag" style="border-color:var(--ok);color:var(--ok)">visible</span>' if now_open
               else '<span class="tag">hidden</span>')
        if state in STATE_LABELS:
            tag += f' <span class="tag">{STATE_LABELS[state]}</span>'
        body.append(f'<tr><td>{names}<div class="muted">{esc(label.replace("release.", ""))}</div></td>'
                    f'<td>{esc(start or "—")}<br>{esc(end or "—")}</td><td>{tag}</td>'
                    f'<td>{_state_select(ctx, label, state)}</td></tr>')
    tabs = " · ".join(f'<a href="{BASE}/banners?show={k}"{" style=\'font-weight:600\'" if which == k else ""}>{t}</a>'
                      for k, t in (("current", "end of service"), ("open", "visible now"), ("all", "all")))
    search = (f'<form class="row" method="get" action="{BASE}/banners"><input name="q" value="{esc(needle)}" '
              f'placeholder="name, id or label (edith, climax, 171100501...)" style="min-width:320px">'
              f'<button>Search</button></form>')
    other = ctx.action("release_window", '<input name="label" placeholder="release.202211_climax_exchange" '
                                         'required size="34"><select name="state"><option value="open">Enable'
                                         '</option><option value="closed">Disable</option><option value="auto">'
                                         'Auto</option></select>', "Apply to this window")
    forced_list = "".join(
        f'<li>{esc(lbl)} — {STATE_LABELS.get(st, st)} '
        + ctx.action("release_window", f'<input type="hidden" name="label" value="{esc(lbl)}">'
                                       '<input type="hidden" name="state" value="auto">', "Back to auto", "ghost")
        + "</li>" for lbl, st in sorted(forced.items()))
    now = time.strftime("%d/%m/%Y %H:%M", time.gmtime(config.now() + 9 * 3600))
    raid = ('<div class="flash err">Raid mode on: the game clock loops over the raid week (2022-11-20 to 24), '
            'banners starting later are hidden. Turn it off on the Server page to go back to the end of '
            'service.</div>' if config.RAID_MODE else "")
    return page(ctx, "banners", "Banners", f"""{raid}
<div class="card"><p class="muted">A banner is visible in game during its <b>release window</b> (the original
game's dates, Japan time), compared to the game clock (now: <b>{now}</b>, set on the Server page).
<b>Enable</b> makes it visible until the end of service, <b>Disable</b> hides it, <b>Auto</b> follows its dates.
The change is served at once; players see it at their <b>next game start</b>.</p>
<p>{tabs}</p>{search}</div>
<div class="card"><h2>Active settings</h2>{f"<ul>{forced_list}</ul>" if forced_list else '<p class="muted">None: everything follows the game dates.</p>'}
<h2>Other dated content (exchange, login bonus, event...)</h2>{other}</div>
<div class="card"><h2>Scout banners ({len(rows)})</h2>
<table><tr><th>Banner</th><th>Window (Japan)</th><th>In game</th><th>Setting</th></tr>{"".join(body)}</table></div>""")


def clock_card(ctx) -> str:
    """Game clock window (issue #29): the period the server's clock loops over, which decides the banners and events
    open in game (release windows are compared to it)."""
    def local(iso):  # ISO 8601 -> datetime-local value in Japan time
        return datetime.fromisoformat(iso).astimezone(JST).strftime("%Y-%m-%dT%H:%M") if iso else ""
    start, _, end = (config.GAME_WINDOW or "/").partition("/")
    shown = (f"{local(start).replace('T', ' ')} → {local(end).replace('T', ' ')} (Japan)" if config.GAME_WINDOW
             else "real time")
    now = time.strftime("%d/%m/%Y %H:%M", time.gmtime(config.now() + 9 * 3600))
    fields = (f'<label>start <input type="datetime-local" name="start" value="{local(start)}" required></label>'
              f'<label>end <input type="datetime-local" name="end" value="{local(end)}" required></label>')
    default = ctx.action("game_window", '<input type="hidden" name="default" value="1">',
                         "Back to the default window", "ghost", "Reset the clock to the default for everyone?")
    return f"""<div class="card"><h2>Game clock</h2>
<p>Window: <b>{esc(shown)}</b>{" (default)" if config.GAME_WINDOW == config.DEFAULT_GAME_WINDOW else ""}<br>
Game time now: <b>{now}</b> (Japan){" — <b>raid mode on, it takes priority</b>" if config.RAID_MODE else ""}</p>
<p class="muted">The server loops the game time over this period (Japan time): it decides which banners and events
are open. Players see the new time at their next game start.</p>
{ctx.action("game_window", fields, "Apply", "", "Change the game clock for every player?")}
{default}</div>"""


def p_server(ctx, q):
    a = ctx.action
    m = db.get_setting("maintenance") or {}
    guilds = "".join(f'<tr><td>{g["id"]}</td><td>{esc(g.get("name"))}</td><td>{len(g.get("members", {}))}</td>'
                     f'<td>{esc(g.get("leader"))}</td><td>'
                     + a("guild_delete", f'<input type="hidden" name="id" value="{g["id"]}">', "Disband", "bad",
                         "Disband this guild?") + "</td></tr>" for g in db.list_guilds())
    unlinked = "".join(f'<tr><td>{esc(d["uuid"])}</td><td>{ts(d.get("created_at"))}</td></tr>'
                       for d in db.list_devices() if not d.get("x_uid"))
    log = "\n".join(tail(os.path.join(config.LOG_DIR, "server.log"), 150))
    return page(ctx, "server", "Server", f"""
<div class="grid">
<div class="card"><h2>Maintenance</h2>
<p>State: {"<b style='color:var(--bad)'>on</b>" if m.get("enabled") else "off"}</p>
{a("maintenance", f'<input name="message" placeholder="message shown to players" value="{esc(m.get("message", ""))}" style="width:100%">'
   f'<label><input type="checkbox" name="enabled" {"checked" if m.get("enabled") else ""}> maintenance on</label>', "Apply")}
<p class="muted">The game reads this state at start (title screen).</p></div>
<div class="card"><h2>Scouts</h2>
<p>Banner steps (discount, boosted rates, guaranteed 11th): <b>{"on" if config.LOTTERY_STEP_BONUS else "off"}</b></p>
{a("lottery_bonus", f'<input type="hidden" name="on" value="{0 if config.LOTTERY_STEP_BONUS else 1}">',
   "Disable" if config.LOTTERY_STEP_BONUS else "Enable", "ghost")}</div>
<div class="card"><h2>Raid mode</h2>
<p>State: <b>{"ON" if config.RAID_MODE else "off"}</b> — game time: {time.strftime("%d/%m/%Y %H:%M", time.gmtime(config.now() + 9 * 3600))} (Japan)</p>
<p class="muted">On: the game clock loops over the week of raid 18 (2022-11-20 to 24), the only period the raid is
open; the other events shown are those of that week. Players restart the game after the change.</p>
{a("raid_mode", f'<input type="hidden" name="on" value="{0 if config.RAID_MODE else 1}">',
   "Turn raid mode off" if config.RAID_MODE else "Turn raid mode on", "ghost",
   "Change the game clock for every player?")}</div>
{clock_card(ctx)}
<div class="card scroll"><h2>Unlinked devices</h2><table>{unlinked or '<tr><td class="muted">none</td></tr>'}</table></div>
</div>
<div class="card scroll"><h2>Guilds</h2><table><tr><th>Id</th><th>Name</th><th>Members</th><th>Leader</th><th></th></tr>{guilds}</table></div>
<div class="card"><h2>Server log (last 150 lines)</h2><pre>{esc(log)}</pre></div>""")


def names_json(q) -> bytes:
    kind = (q.get("kind") or ["items"])[0]
    needle = (q.get("q") or [""])[0].strip().lower()
    table = master.NAMES.get(kind, {})
    out = []
    for k, v in table.items():
        if needle and needle not in k and needle not in v.lower():
            continue
        out.append([k, v])
        if len(out) >= 40:
            break
    return json.dumps(out, ensure_ascii=False).encode()


# ---------------------------------------------------------------- actions

def _int(form, key, default=None):
    try:
        return int(str(form.get(key, "")).strip())
    except ValueError:
        if default is None:
            raise ValueError(f"\"{key}\" must be a number")
        return default


def _check_target(kind: str, target: int):
    """An id the game does not know for that type (a character id sent as an item) crashes the gift box."""
    if master.NAMES.get(kind) and not name_of(kind, target):
        label = {"items": "item", "equipment": "equipment", "pcs": "character"}[kind]
        raise ValueError(f"{target} is not a known {label} id (check the chosen type)")


def _target_id(form, key="id") -> int:
    """'246000011' or '246000011 EXP Water' (datalist) -> 246000011."""
    raw = str(form.get(key, "")).strip().split(" ")[0]
    if not raw.isdigit():
        raise ValueError("missing or invalid id")
    return int(raw)


def do_action(name: str, form: dict) -> tuple[str, str]:
    """Returns (redirect path, flash message)."""
    if name in ("gems", "item", "pc", "equipment", "rename", "totalexp", "retire", "heal", "snapshot", "restore", "link",
                "gift_delete"):
        uid = _int(form, "uid")
        if not db.get_user(uid):
            raise ValueError(f"account {uid} not found")
        back = f"player/{uid}"
        if name == "snapshot":
            snapshot(uid, form.get("label") or "manual")
            return back, "Snapshot saved."
        if name == "restore":
            snap = db.get_snapshot(_int(form, "snapshot"))
            if not snap or snap["user_id"] != uid:
                raise ValueError("snapshot not found")
            snapshot(uid, f"before restoring \"{snap['label']}\"")
            with userdata._lock:
                db.save_user(uid, snap["data"])
            return back, f"Account restored to its state of {ts(snap['created_at'])} ({snap['label']})."
        if name == "link":
            uuid = str(form.get("uuid", "")).strip()
            dev = db.get_device(uuid)
            if not dev:
                raise ValueError("unknown device (it must have connected at least once)")
            db.bind_xuid(uuid, str(uid), dev["x_app_id"] or "")
            # the account follows the new device: its first request binds it (see api.authorize)
            db.clear_platform(uid)
            return back, f"Device {uuid} linked to account {uid}."
        snapshot(uid, f"before: {name}")
        if name == "gift_delete":
            gift_id = _int(form, "gift")
            gifts = list(userdata.table(uid, "gift"))
            kept = [g for g in gifts if not (g.get("id") == gift_id and not g.get("state"))]
            if len(kept) == len(gifts):
                raise ValueError("gift not found or already opened")
            userdata.set_tables(uid, {"gift": kept})
            return back, f"Gift {gift_id} deleted."
        if name == "gems":
            amount = _int(form, "amount")
            return back, f"{amount:+} cubes (balance {userdata.add_gems(uid, amount)})."
        if name in ("item", "pc", "equipment"):
            target = _target_id(form)
            _check_target({"item": "items", "pc": "pcs", "equipment": "equipment"}[name], target)
            grant = api.Grant(uid)
            if name == "item":
                grant.item(target, _int(form, "amount", 1))
            elif name == "pc":
                if not grant.pc(target):
                    return back, "This character is already owned."
            else:
                grant.equipment(target, max(1, min(50, _int(form, "amount", 1))))
            userdata.set_tables(uid, grant.changes())
            return back, f"Given: {name_of({'item': 'items', 'pc': 'pcs', 'equipment': 'equipment'}[name], target) or target}."
        if name in ("rename", "totalexp"):
            info = dict((userdata.table(uid, "info") or [{}])[0])
            if name == "rename":
                info["name"] = str(form.get("name", "")).strip()[:24] or info.get("name", "")
            else:
                info["totalExp"] = max(0, _int(form, "exp"))
            userdata.set_tables(uid, {"info": [info]})
            return back, "Account updated."
        if name == "retire":
            api.ep_quest_retire(uid, {"isUserRetire": True})
            return back, "Quest abandoned on the server."
        if name == "heal":
            rows = api.healed_pcs(uid)
            if rows:
                userdata.set_tables(uid, {"pc": rows})
            return back, "Party healed." if rows else "Nobody was hurt."
    if name == "gift":
        to = str(form.get("uid", "")).strip().lower()
        kind = _int(form, "type")
        if kind not in REWARD_TYPES:
            raise ValueError("unknown gift type")
        target = GOLD_ITEM if kind == 4 else (0 if kind == 3 else _target_id(form))
        if kind in (1, 2, 5):
            _check_target({1: "items", 2: "equipment", 5: "pcs"}[kind], target)
        amount = max(1, _int(form, "amount", 1))
        reason = _int(form, "reason", 0)
        uids = db.user_ids() if to == "all" else [int(to)] if to.isdigit() else []
        uids = [u for u in uids if db.get_user(u)]
        if not uids:
            raise ValueError("recipient not found")
        for u in uids:
            api.add_gift(u, kind, target, amount, 1, reason)
        log = db.get_setting("gift_log", [])[-49:] + [{"t": time.time(), "to": to, "type": kind, "count": len(uids),
                                                       "name": reward_name(kind, target) or str(target), "amount": amount}]
        db.set_setting("gift_log", log)
        back = "gifts" if form.get("from") != "player" and to == "all" else (f"player/{uids[0]}" if len(uids) == 1 else "gifts")
        return back, f"Gift sent to {len(uids)} account(s)."
    if name == "announcement_save":
        title = str(form.get("title", "")).strip()
        if not title:
            raise ValueError("a title is required")
        url = str(form.get("image_url", "")).strip()
        if url and not url.startswith("https://"):
            raise ValueError("the image must be an https:// URL")
        ann_id = db.save_announcement(_int(form, "id", 0) or None, title, str(form.get("body", "")), url,
                                      bool(form.get("visible")))
        return "announcements", f"Announcement {ann_id} saved."
    if name == "announcement_delete":
        db.delete_announcement(_int(form, "id"))
        return "announcements", "Announcement deleted."
    if name == "maintenance":
        enabled = bool(form.get("enabled"))
        db.set_setting("maintenance", {"enabled": enabled, "message": str(form.get("message", "")),
                                       "since": int(time.time()) - 60})
        return "server", "Maintenance on." if enabled else "Maintenance off."
    if name == "release_window":
        label = str(form.get("label", "")).strip()
        state = str(form.get("state", "auto"))
        if label not in master.RELEASES:
            raise ValueError(f"unknown window: {label}")
        if state not in ("auto", "open", "closed"):
            raise ValueError("unknown setting")
        forced = dict(db.get_setting("release_windows", {}) or {})
        if state == "auto":
            forced.pop(label, None)
        else:
            forced[label] = state
        db.set_setting("release_windows", forced)
        import assets  # noqa: PLC0415
        if assets.CURRENT is not None:
            assets.CURRENT.refresh_windows()  # served base.bin + manifests rebuilt now, no restart
        msg = {"auto": "follows its dates", "open": "enabled", "closed": "disabled"}[state]
        return "banners", f"{label}: {msg}. Players see it at their next game start."
    if name == "game_window":
        if form.get("default"):
            spec = config.DEFAULT_GAME_WINDOW
            db.set_setting("game_window", None)
        else:
            spec = parse_game_window(str(form.get("start", "")), str(form.get("end", "")))
            db.set_setting("game_window", spec)
        config.set_game_window(spec)
        jst = time.strftime("%d/%m/%Y %H:%M", time.gmtime(config.now() + 9 * 3600))
        return "server", f"Game clock: {spec or 'real time'} — game time now: {jst} (Japan)."
    if name == "raid_mode":
        config.set_raid_mode(form.get("on") == "1")
        db.set_setting("raid_mode", config.RAID_MODE)
        return "server", "Raid mode " + ("on." if config.RAID_MODE else "off.")
    if name == "lottery_bonus":
        config.LOTTERY_STEP_BONUS = form.get("on") == "1"
        db.set_setting("lottery_step_bonus", config.LOTTERY_STEP_BONUS)
        return "server", "Scout steps " + ("on." if config.LOTTERY_STEP_BONUS else "off.")
    if name == "guild_delete":
        gid = _int(form, "id")
        g = db.get_guild(gid)
        db.delete_guild(gid)
        return "server", f"Guild {gid} ({(g or {}).get('name', '?')}) disbanded."
    raise ValueError("unknown action")


# ---------------------------------------------------------------- HTTP

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Log in · SAOUB admin</title><style>{css}
main{{max-width:360px;margin:12vh auto}}</style></head><body><main><div class="card"><h1>SAOUB admin</h1>{msg}
<form method="post" action="{base}/login"><p><input type="password" name="password" placeholder="Password"
autofocus required style="width:100%"></p><button>Log in</button></form></div></main></body></html>"""


def _cookie_header(token: str, max_age: int) -> str:
    return f"{COOKIE}={token}; Path={BASE or '/'}; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"


def _session(headers: dict):
    raw = headers.get("COOKIE", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            s = _sessions.get(v)
            if s and s["expires"] > time.time():
                return v, s
    return None, None


def _client_ip(headers: dict, peer: str) -> str:
    # Caddy (the only way in) sets X-Forwarded-For; its last hop is the real client
    fwd = headers.get("X-FORWARDED-FOR", "")
    return fwd.split(",")[-1].strip() if fwd else peer


def _redirect(path: str, cookie=None) -> tuple:
    h = {"Location": f"{BASE}/{path}".rstrip("/") or "/"}
    if cookie:
        h["Set-Cookie"] = cookie
    return 303, b"", "text/plain", h


def handle(method: str, path: str, headers: dict, body: bytes, peer: str = "") -> tuple:
    """(status, body, content type, extra headers). headers: upper-case names."""
    headers = {k.upper(): v for k, v in headers.items()}
    path, _, query = path.partition("?")
    if not PASSWORD:
        return 503, b"admin panel disabled (SAOUB_ADMIN_PASSWORD not set)", "text/plain", {}
    if not path.startswith(BASE):
        return 404, b"not found", "text/plain", {}
    route = path[len(BASE):].strip("/")
    q = urllib.parse.parse_qs(query)
    form = {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
    ip = _client_ip(headers, peer)
    sec = {"X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", "Cache-Control": "no-store",
           "X-Content-Type-Options": "nosniff"}

    if route == "login":
        if method == "POST":
            recent = [t for t in _failures.get(ip, []) if time.time() - t < FAILURE_WINDOW]
            if len(recent) >= MAX_FAILURES:
                msg = '<div class="flash err">Too many attempts, try again in 10 minutes.</div>'
            elif hmac.compare_digest(form.get("password", "").encode(), PASSWORD.encode()):
                _failures.pop(ip, None)
                token = secrets.token_urlsafe(32)
                _sessions[token] = {"expires": time.time() + SESSION_SECONDS, "csrf": secrets.token_urlsafe(24)}
                return _redirect("", _cookie_header(token, SESSION_SECONDS))
            else:
                _failures[ip] = recent + [time.time()]
                msg = '<div class="flash err">Wrong password.</div>'
        else:
            msg = ""
        return 200, LOGIN_PAGE.format(css=CSS, msg=msg, base=BASE).encode(), "text/html; charset=utf-8", sec

    token, session = _session(headers)
    if not session:
        return _redirect("login")
    if method == "POST" and not hmac.compare_digest(form.get("csrf", ""), session["csrf"]):
        return 403, b"invalid form token (reload the page)", "text/plain", sec
    if route == "logout" and method == "POST":
        _sessions.pop(token, None)
        return _redirect("login", _cookie_header("", 0))

    flash = None
    raw_flash = (q.get("ok") or q.get("err") or [None])[0]
    if raw_flash:
        flash = ("err" if "err" in q else "ok", raw_flash)
    ctx = Ctx(session, flash)
    try:
        if route.startswith("action/") and method == "POST":
            try:
                back, msg = do_action(route[len("action/"):], form)
                sep = "&" if "?" in back else "?"
                return _redirect(f"{back}{sep}ok={urllib.parse.quote(msg)}")
            except (ValueError, KeyError) as e:
                back = f"player/{form['uid']}" if str(form.get("uid", "")).isdigit() else (
                    route[len("action/"):].split("_")[0] if route.startswith("action/announcement") else "")
                back = {"announcement": "announcements"}.get(back, back)
                return _redirect(f"{back}?err={urllib.parse.quote(str(e))}")
        if route == "names":
            return 200, names_json(q), "application/json", sec
        if route.startswith("banner_img/") and route[11:].isdigit():
            data = banner_image(route[11:])
            if data is None:
                return 404, b"no picture", "text/plain", sec
            return 200, data, "image/png", dict(sec, **{"Cache-Control": "private, max-age=86400"})
        if route == "":
            out = p_dashboard(ctx, q)
        elif route == "players":
            out = p_players(ctx, q)
        elif route.startswith("player/") and route[7:].isdigit():
            out = p_player(ctx, q, int(route[7:]))
        elif route == "gifts":
            out = p_gifts(ctx, q)
        elif route == "announcements":
            out = p_announcements(ctx, q)
        elif route == "banners":
            out = p_banners(ctx, q)
        elif route == "server":
            out = p_server(ctx, q)
        else:
            return 404, b"not found", "text/plain", sec
    except Exception:  # noqa: BLE001
        import traceback  # noqa: PLC0415
        return 500, ("<pre>" + esc(traceback.format_exc()) + "</pre>").encode(), "text/html; charset=utf-8", sec
    return 200, out, "text/html; charset=utf-8", sec


def serve():
    """Run the panel in a background thread (called by main.py when SAOUB_ADMIN_PASSWORD is set)."""
    import threading  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        timeout = 60  # idle connections don't hold a thread
        server_version, sys_version = "SAOUB", ""  # no software versions in the Server header

        def _do(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 1024 * 1024:  # panel forms are a few KB
                self.close_connection = True
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.rfile.read(length) if length else b""
            code, out, ctype, extra = handle(self.command, self.path, dict(self.headers), body, self.client_address[0])
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _do

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="admin").start()
    return srv
