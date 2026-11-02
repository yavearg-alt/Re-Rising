"""SQLite storage for accounts."""
import json
import sqlite3
import threading
import time

import config

_lock = threading.Lock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
CREATE TABLE IF NOT EXISTS devices (
    uuid        TEXT PRIMARY KEY,      -- GREE gamelib device uuid (issued by /v1.0/auth/initialize)
    device_id   TEXT,
    x_uid       TEXT,                  -- game user id bound to this device
    x_app_id    TEXT,
    created_at  INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER,
    data        TEXT NOT NULL DEFAULT '{}'
);
""")
# start user ids in a realistic 12-digit range (the archived account used 358924210706)
if _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
    _conn.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES ('users', 100000000000)")
_conn.commit()


def create_device(uuid: str, device_id: str):
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO devices(uuid, device_id, created_at) VALUES (?,?,?)",
                      (uuid, device_id, int(time.time())))
        _conn.commit()


def get_device(uuid: str):
    with _lock:
        return _conn.execute("SELECT * FROM devices WHERE uuid=?", (uuid,)).fetchone()


def bind_xuid(uuid: str, x_uid: str, x_app_id: str):
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO devices(uuid, created_at) VALUES (?,?)", (uuid, int(time.time())))
        _conn.execute("UPDATE devices SET x_uid=?, x_app_id=? WHERE uuid=?", (x_uid, x_app_id, uuid))
        _conn.commit()


def create_user() -> int:
    with _lock:
        cur = _conn.execute("INSERT INTO users(created_at) VALUES (?)", (int(time.time()),))
        _conn.commit()
        return cur.lastrowid


def get_user(user_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return (row["user_id"], json.loads(row["data"])) if row else None


def save_user(user_id: int, data: dict):
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?,?)", (user_id, int(time.time())))
        _conn.execute("UPDATE users SET data=? WHERE user_id=?", (json.dumps(data), user_id))
        _conn.commit()


# Guilds are shared between accounts: one JSON document per guild (see guild.py).
_conn.executescript("""
CREATE TABLE IF NOT EXISTS guilds (
    guild_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER,
    data        TEXT NOT NULL DEFAULT '{}'
);
""")
if _conn.execute("SELECT COUNT(*) FROM guilds").fetchone()[0] == 0:
    _conn.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES ('guilds', 10000000)")
_conn.commit()


def create_guild() -> int:
    with _lock:
        cur = _conn.execute("INSERT INTO guilds(created_at) VALUES (?)", (int(time.time()),))
        _conn.commit()
        return cur.lastrowid


def get_guild(guild_id: int):
    with _lock:
        row = _conn.execute("SELECT data FROM guilds WHERE guild_id=?", (guild_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def save_guild(guild_id: int, data: dict):
    with _lock:
        _conn.execute("UPDATE guilds SET data=? WHERE guild_id=?", (json.dumps(data), guild_id))
        _conn.commit()


def delete_guild(guild_id: int):
    with _lock:
        _conn.execute("DELETE FROM guilds WHERE guild_id=?", (guild_id,))
        _conn.commit()


def list_guilds() -> list:
    with _lock:
        rows = _conn.execute("SELECT data FROM guilds").fetchall()
    return [g for g in (json.loads(r["data"]) for r in rows) if g.get("id")]  # skip ones still being formed


def user_ids() -> list:
    with _lock:
        return [r[0] for r in _conn.execute("SELECT user_id FROM users").fetchall()]


def list_devices() -> list:
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM devices ORDER BY created_at DESC").fetchall()]


# Admin panel (admin.py): server settings, in-game announcements, account snapshots taken before every admin edit.
_conn.executescript("""
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL              -- JSON
);
CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    image_url   TEXT NOT NULL DEFAULT '',
    visible     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER,
    updated_at  INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER,
    label       TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_user ON snapshots(user_id, created_at);
""")
_conn.commit()
SNAPSHOTS_KEPT = 30  # per account


def get_setting(key: str, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value):
    with _lock:
        _conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))
        _conn.commit()


def list_announcements(visible_only=False) -> list:
    with _lock:
        sql = "SELECT * FROM announcements" + (" WHERE visible=1" if visible_only else "") + " ORDER BY id DESC"
        return [dict(r) for r in _conn.execute(sql).fetchall()]


def get_announcement(ann_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    return dict(row) if row else None


def save_announcement(ann_id, title: str, body: str, image_url: str, visible: bool) -> int:
    now = int(time.time())
    with _lock:
        if ann_id:
            _conn.execute("UPDATE announcements SET title=?, body=?, image_url=?, visible=?, updated_at=? WHERE id=?",
                          (title, body, image_url, int(visible), now, ann_id))
        else:
            ann_id = _conn.execute("INSERT INTO announcements(title, body, image_url, visible, created_at, updated_at)"
                                   " VALUES (?,?,?,?,?,?)", (title, body, image_url, int(visible), now, now)).lastrowid
        _conn.commit()
    return ann_id


def delete_announcement(ann_id: int):
    with _lock:
        _conn.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
        _conn.commit()


def add_snapshot(user_id: int, label: str, data: dict) -> int:
    with _lock:
        sid = _conn.execute("INSERT INTO snapshots(user_id, created_at, label, data) VALUES (?,?,?,?)",
                            (user_id, int(time.time()), label, json.dumps(data))).lastrowid
        _conn.execute("DELETE FROM snapshots WHERE user_id=? AND id NOT IN (SELECT id FROM snapshots WHERE user_id=?"
                      " ORDER BY id DESC LIMIT ?)", (user_id, user_id, SNAPSHOTS_KEPT))
        _conn.commit()
    return sid


def list_snapshots(user_id: int) -> list:
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT id, user_id, created_at, label FROM snapshots WHERE user_id=?"
                                               " ORDER BY id DESC", (user_id,)).fetchall()]


def get_snapshot(snap_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    return dict(row, data=json.loads(row["data"])) if row else None


# Account <-> device binding (security audit 2026-10-10): the game identifies itself with SAOAL-USER-ID only, and ids
# are sequential, so any request could act on any account. Each account is bound to the device it plays from (the
# SAOAL-PLATFORM-USER-ID header, a per-device id other players don't know); see api.authorize.
_conn.executescript("""
CREATE TABLE IF NOT EXISTS user_platforms (
    user_id     INTEGER PRIMARY KEY,
    platform_id TEXT NOT NULL,
    bound_at    INTEGER
);
""")
_conn.commit()


# second identifier: the advertising id (SAOAL-AD-ID) survives a reinstall, while the platform id changes with it
if "ad_id" not in [r[1] for r in _conn.execute("PRAGMA table_info(user_platforms)")]:
    _conn.execute("ALTER TABLE user_platforms ADD COLUMN ad_id TEXT NOT NULL DEFAULT ''")
    _conn.commit()


def binding(user_id: int):
    """(platform_id, ad_id) the account is bound to, or None."""
    with _lock:
        row = _conn.execute("SELECT platform_id, ad_id FROM user_platforms WHERE user_id=?", (user_id,)).fetchone()
    return (row["platform_id"], row["ad_id"]) if row else None


def set_binding(user_id: int, platform_id: str, ad_id: str):
    with _lock:
        _conn.execute("INSERT INTO user_platforms(user_id, platform_id, ad_id, bound_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET platform_id=excluded.platform_id, ad_id=excluded.ad_id",
                      (user_id, platform_id, ad_id, int(time.time())))
        _conn.commit()


def bindings_without_ad() -> list:
    with _lock:
        return [(r["user_id"], r["platform_id"]) for r in
                _conn.execute("SELECT user_id, platform_id FROM user_platforms WHERE ad_id=''").fetchall()]


def user_platform(user_id: int):
    with _lock:
        row = _conn.execute("SELECT platform_id FROM user_platforms WHERE user_id=?", (user_id,)).fetchone()
    return row["platform_id"] if row else None


def bind_platform(user_id: int, platform_id: str) -> str:
    """Binds the account to this device unless it already is bound; returns the bound device."""
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO user_platforms(user_id, platform_id, bound_at) VALUES (?,?,?)",
                      (user_id, platform_id, int(time.time())))
        _conn.commit()
        return _conn.execute("SELECT platform_id FROM user_platforms WHERE user_id=?", (user_id,)).fetchone()[0]


def clear_platform(user_id: int):
    """The next device that plays this account becomes its device (admin panel: account moved to a new device)."""
    with _lock:
        _conn.execute("DELETE FROM user_platforms WHERE user_id=?", (user_id,))
        _conn.commit()


def platform_count() -> int:
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM user_platforms").fetchone()[0]


def devices_of_xuid(x_uid: str) -> list:
    with _lock:
        return [r["uuid"] for r in _conn.execute("SELECT uuid FROM devices WHERE x_uid=?", (x_uid,)).fetchall()]


# Discord bot (botapi.py, discord_bot/): Discord user -> game account links, the short codes players put in their
# profile comment to prove they own the account, and the ledger of every gift claim.
_conn.executescript("""
CREATE TABLE IF NOT EXISTS discord_links (
    discord_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL UNIQUE,   -- one Discord user per game account
    linked_at   INTEGER
);
CREATE TABLE IF NOT EXISTS discord_link_codes (
    discord_id  TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    created_at  INTEGER
);
CREATE TABLE IF NOT EXISTS discord_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  TEXT NOT NULL,
    discord_name TEXT NOT NULL DEFAULT '',
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,             -- character | cubes | item
    reward_type INTEGER NOT NULL,          -- api.add_gift rewardType
    target      INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    label       TEXT NOT NULL DEFAULT '',  -- display name of the reward
    status      TEXT NOT NULL,             -- pending | approved | refused | failed
    auto        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER,
    decided_at  INTEGER,
    decided_by  TEXT NOT NULL DEFAULT '',  -- Discord id of the staff member ("rules" when auto-approved)
    decided_by_name TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    snapshot_id INTEGER
);
CREATE INDEX IF NOT EXISTS discord_claims_user ON discord_claims(discord_id, created_at);
""")
_conn.commit()


def discord_link(discord_id: str):
    with _lock:
        row = _conn.execute("SELECT * FROM discord_links WHERE discord_id=?", (discord_id,)).fetchone()
    return dict(row) if row else None


def discord_link_of_user(user_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM discord_links WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def set_discord_link(discord_id: str, user_id: int):
    """Links (or re-links) a Discord user; the account must not belong to another Discord user (UNIQUE)."""
    with _lock:
        _conn.execute("INSERT INTO discord_links(discord_id, user_id, linked_at) VALUES (?,?,?) "
                      "ON CONFLICT(discord_id) DO UPDATE SET user_id=excluded.user_id, linked_at=excluded.linked_at",
                      (discord_id, user_id, int(time.time())))
        _conn.execute("DELETE FROM discord_link_codes WHERE discord_id=?", (discord_id,))
        _conn.commit()


def delete_discord_link(discord_id: str) -> bool:
    with _lock:
        n = _conn.execute("DELETE FROM discord_links WHERE discord_id=?", (discord_id,)).rowcount
        _conn.commit()
    return n > 0


def discord_link_code(discord_id: str):
    with _lock:
        row = _conn.execute("SELECT * FROM discord_link_codes WHERE discord_id=?", (discord_id,)).fetchone()
    return dict(row) if row else None


def set_discord_link_code(discord_id: str, code: str):
    with _lock:
        _conn.execute("INSERT INTO discord_link_codes(discord_id, code, created_at) VALUES (?,?,?) "
                      "ON CONFLICT(discord_id) DO UPDATE SET code=excluded.code, created_at=excluded.created_at",
                      (discord_id, code, int(time.time())))
        _conn.commit()


def add_discord_claim(claim: dict) -> int:
    cols = ("discord_id", "discord_name", "user_id", "kind", "reward_type", "target", "amount", "label", "status",
            "auto")
    with _lock:
        cid = _conn.execute(f"INSERT INTO discord_claims({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
                            tuple(claim[c] for c in cols) + (int(time.time()),)).lastrowid
        _conn.commit()
    return cid


def get_discord_claim(claim_id: int):
    with _lock:
        row = _conn.execute("SELECT * FROM discord_claims WHERE id=?", (claim_id,)).fetchone()
    return dict(row) if row else None


def decide_discord_claim(claim_id: int, status: str, by: str, by_name: str, note: str = "", snapshot_id=None,
                         from_status: str = "pending") -> bool:
    """Moves a claim out of `from_status`; False when it already left it (two staff members clicking at once)."""
    with _lock:
        n = _conn.execute("UPDATE discord_claims SET status=?, decided_at=?, decided_by=?, decided_by_name=?, note=?, "
                          "snapshot_id=COALESCE(?, snapshot_id) WHERE id=? AND status=?",
                          (status, int(time.time()), by, by_name, note, snapshot_id, claim_id, from_status)).rowcount
        _conn.commit()
    return n > 0


def list_discord_claims(discord_id: str = "", status: str = "", since: int = 0, limit: int = 50) -> list:
    sql, args = "SELECT * FROM discord_claims WHERE created_at>=?", [since]
    if discord_id:
        sql, args = sql + " AND discord_id=?", args + [discord_id]
    if status:
        sql, args = sql + " AND status=?", args + [status]
    with _lock:
        return [dict(r) for r in _conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()]


# Device transfers (botapi.transfer_*): a player moves an account to a new device. The one-time code goes into the
# profile comment of both accounts; the log keeps every transfer (cooldown, staff review).
_conn.executescript("""
CREATE TABLE IF NOT EXISTS transfer_codes (
    discord_id  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,          -- the account to move (linked to discord_id when the code was issued)
    code        TEXT NOT NULL UNIQUE,
    created_at  INTEGER
);
CREATE TABLE IF NOT EXISTS transfers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  TEXT NOT NULL,
    user_id     INTEGER NOT NULL,          -- the account that moved
    other_id    INTEGER NOT NULL,          -- the new device's fresh account (now on the old device)
    created_at  INTEGER
);
""")
_conn.commit()


def transfer_code(discord_id: str):
    with _lock:
        row = _conn.execute("SELECT * FROM transfer_codes WHERE discord_id=?", (discord_id,)).fetchone()
    return dict(row) if row else None


def set_transfer_code(discord_id: str, user_id: int, code: str):
    with _lock:
        _conn.execute("INSERT INTO transfer_codes(discord_id, user_id, code, created_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(discord_id) DO UPDATE SET user_id=excluded.user_id, code=excluded.code, "
                      "created_at=excluded.created_at", (discord_id, user_id, code, int(time.time())))
        _conn.commit()


def delete_transfer_code(discord_id: str):
    with _lock:
        _conn.execute("DELETE FROM transfer_codes WHERE discord_id=?", (discord_id,))
        _conn.commit()


def last_transfer_at(user_id: int) -> int:
    with _lock:
        row = _conn.execute("SELECT MAX(created_at) FROM transfers WHERE user_id=? OR other_id=?",
                            (user_id, user_id)).fetchone()
    return row[0] or 0


def user_created_at(user_id: int) -> int:
    with _lock:
        row = _conn.execute("SELECT created_at FROM users WHERE user_id=?", (user_id,)).fetchone()
    return (row[0] or 0) if row else 0


def swap_devices(a: int, b: int, discord_id: str):
    """Account `a` goes to the devices of `b` and `b` to those of `a`, in one transaction: GREE device bindings
    (devices.x_uid) and the request bindings (user_platforms, api.authorize). No moment where an account is unbound
    (an unbound account binds to the first device that sends its id)."""
    with _lock:
        try:
            _conn.execute("UPDATE devices SET x_uid = CASE x_uid WHEN ? THEN ? ELSE ? END WHERE x_uid IN (?, ?)",
                          (str(a), str(b), str(a), str(a), str(b)))
            rows = {r["user_id"]: (r["platform_id"], r["ad_id"], r["bound_at"]) for r in _conn.execute(
                "SELECT * FROM user_platforms WHERE user_id IN (?, ?)", (a, b)).fetchall()}
            _conn.execute("DELETE FROM user_platforms WHERE user_id IN (?, ?)", (a, b))
            for uid, other in ((a, b), (b, a)):
                if other in rows:
                    p, ad, _ = rows[other]
                    _conn.execute("INSERT INTO user_platforms(user_id, platform_id, ad_id, bound_at) VALUES (?,?,?,?)",
                                  (uid, p, ad, int(time.time())))
            _conn.execute("INSERT INTO transfers(discord_id, user_id, other_id, created_at) VALUES (?,?,?,?)",
                          (discord_id, a, b, int(time.time())))
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
