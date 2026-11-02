"""Bring a player's original account back from their install of the official game (the local copy the client kept
in its LevelDB, /data/data/com.bandainamcoent.saoars/files/ldb, AES + MessagePack: see docs/PROTOCOL.md).

1. extract (anywhere, on the received dump): keeps only the account tables, never the login data of the folder
   (GREE / Adjust / Firebase ids stay out of the output):
       python tools/import_account.py extract <app folder | files folder | ldb folder> [-o account.json]
2. apply (with the server's environment, player NOT in the game, else the client pushes its tables back): replaces
   the tables of an existing server account (the player creates it first with a normal first launch), snapshot first
   (restorable from the admin panel):
       python tools/import_account.py apply account.json <userId>

The free diamond balance was kept by GREE, not in the client: the account keeps its server balance. Friends,
followers and guild tables point to players of the official servers and are emptied.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

PREFIX = b"user_data/data/"
SOCIAL = ("follow", "follower", "guest")  # + every guild* table


def find_ldb(path: str) -> str:
    for candidate in (path, os.path.join(path, "ldb"), os.path.join(path, "files", "ldb")):
        if os.path.exists(os.path.join(candidate, "CURRENT")):
            return candidate
    for root, dirs, files in os.walk(path):
        if os.path.basename(root) == "ldb" and "CURRENT" in files:
            return root
    sys.exit(f"no LevelDB (ldb/CURRENT) under {path}")


def extract(path: str) -> dict:
    import msgpack
    import saoub_crypto
    from leveldb_read import read_db

    names = json.load(open(os.path.join(os.path.dirname(HERE), "server", "table_names.json"), encoding="utf-8"))
    short = {long_name: s for s, long_name in names.items()}
    db = read_db(find_ldb(path))
    iv = db.get(b"encryption/aes_iv")
    if not iv:
        sys.exit("encryption/aes_iv missing: not a SAO Unleash Blading install")
    tables, unknown = {}, []
    for key, value in sorted(db.items()):
        if not key.startswith(PREFIX):
            continue
        long_name = key[len(PREFIX):].decode()
        rows = msgpack.unpackb(saoub_crypto.decrypt(value, iv=iv, inflate=False), strict_map_key=False)
        rows = rows if isinstance(rows, list) else [rows]
        if long_name in short:
            tables[short[long_name]] = rows
        else:
            unknown.append(long_name)
    info = (tables.get("info") or [{}])[0]
    return {
        "source": {"userId": info.get("userId"), "name": info.get("name"),
                   "clientVersion": (db.get(b"asset/client_version") or b"").decode(), "unknownTables": unknown},
        "tables": tables,
    }


def _rekey(obj, uid: int, old: int):
    if isinstance(obj, dict):
        return {k: (uid if k == "userId" and v == old else _rekey(v, uid, old)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rekey(v, uid, old) for v in obj]
    return obj


def apply(account: dict, uid: int) -> str:
    import admin
    import db
    import userdata

    if db.get_user(uid) is None:
        sys.exit(f"account {uid} does not exist on this server: the player launches the game once first")
    old = account["source"]["userId"]
    tables = {}
    for name, rows in account["tables"].items():
        if name in SOCIAL or name.startswith("guild"):
            rows = []
        tables[name] = _rekey(rows, uid, old)
    admin.snapshot(uid, f"avant import du compte original {account['source'].get('name')} ({old})")
    userdata.set_tables(uid, tables)
    clamped = userdata.clamp_times(uid)
    info = userdata.table(uid, "info")[0]
    return (f"account {uid}: {len(tables)} tables imported from {account['source'].get('name')} ({old}), "
            f"{len(userdata.table(uid, 'pc'))} characters, {len(userdata.table(uid, 'item'))} item rows, "
            f"{len(userdata.table(uid, 'questProgress'))} quest levels, name {info.get('name')}, {clamped} times clamped")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    e = sub.add_parser("extract")
    e.add_argument("dump")
    e.add_argument("-o", "--output", default="account.json")
    a = sub.add_parser("apply")
    a.add_argument("account")
    a.add_argument("user_id", type=int)
    args = parser.parse_args()
    if args.mode == "extract":
        account = extract(args.dump)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(account, f, ensure_ascii=False)
        s = account["source"]
        print(f"{s['name']} ({s['userId']}, client {s['clientVersion']}): {len(account['tables'])} tables -> {args.output}"
              + (f"; unknown tables skipped: {', '.join(s['unknownTables'])}" if s["unknownTables"] else ""))
    else:
        with open(args.account, encoding="utf-8") as f:
            print(apply(json.load(f), args.user_id))


if __name__ == "__main__":
    main()
