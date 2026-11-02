"""Test bench: a fake game client that calls the server in-process, on a throwaway database.

Every response is checked the way the real client would consume it:
  - "code" (0 unless the test expects an error), no server exception;
  - every table sent back (table.replace / put / delete) is a known client table;
  - field names and order match the client's format (from the archived account, extracted/sample_user, or from
    server/table_fields_extra.json for tables it never had) -- the client re-hashes the rows in its own field order,
    so a different order means hashes that never match;
  - value types match the archived account (int / float / bool / str / list / dict);
  - single-row tables are never replaced by an empty list (the client ignores it);
  - the "hash" sent for a table is the md5 the client will compute on those rows;
  - userId of every row is the player's.
The Client keeps its own copy of the tables, as the game does, so scenarios can read the state back.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="saoub_test_")
os.environ.setdefault("SAOUB_DB", os.path.join(TMP, "test.sqlite3"))
os.environ["SAOUB_LOG_DIR"] = os.path.join(TMP, "logs")
os.makedirs(os.environ["SAOUB_LOG_DIR"])
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

with contextlib.redirect_stdout(io.StringIO()):
    import api  # noqa: E402
    import config  # noqa: E402
    import db  # noqa: E402
    import master  # noqa: E402
    import userdata  # noqa: E402

TABLE_NAMES = userdata.TABLE_NAMES
# client tables the server never seeds but the client pushes (UserFunctionLockStatus is in the archived account)
KNOWN_TABLES = set(TABLE_NAMES) | {"functionLockStatus"}
SAMPLE_DIR = os.path.join(config.DATA_DIR, "extracted", "sample_user")


def cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


# ---------------------------------------------------------------- reference formats

def _kind(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if v is None:
        return "nil"
    return {str: "str", list: "list", dict: "dict"}.get(type(v), type(v).__name__)


def _schema(row: dict) -> dict:
    """Field order + types of a reference row, recursing into nested objects (first element of lists)."""
    out = {"fields": list(row), "types": {}, "nested": {}}
    for k, v in row.items():
        out["types"][k] = _kind(v)
        if isinstance(v, dict) and v:
            out["nested"][k] = _schema(v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            out["nested"][k] = _schema(v[0])
    return out


def _load_schemas() -> dict:
    schemas = {}
    for short, long_name in TABLE_NAMES.items():
        path = os.path.join(SAMPLE_DIR, long_name + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                value = json.load(f)
            rows = value if isinstance(value, list) else [value]
            if rows:
                schemas[short] = dict(_schema(rows[0]), source="sample_user")
    with open(os.path.join(ROOT, "server", "table_fields_extra.json"), encoding="utf-8") as f:
        extra = json.load(f)
    for name, fields in extra.items():
        if not name.startswith("_") and name not in schemas:
            schemas[name] = {"fields": fields, "types": {}, "nested": {}, "source": "serializer"}
    return schemas


SCHEMAS = _load_schemas()


def _compare(where: str, row: dict, schema: dict, out: list):
    fields = list(row)
    if fields != schema["fields"]:
        missing = [f for f in schema["fields"] if f not in row]
        extra = [f for f in fields if f not in schema["fields"]]
        if missing or extra:
            out.append(("error", f"{where}: champs manquants {missing} / en trop {extra}"))
        else:
            out.append(("error", f"{where}: ordre des champs différent\n      attendu {schema['fields']}\n"
                                 f"      reçu    {fields}"))
    for k, want in schema["types"].items():
        if k not in row or want == "nil":
            continue
        got = _kind(row[k])
        if got != want and not (want == "float" and got == "int"):
            out.append(("warn", f"{where}.{k}: type {got}, attendu {want} (valeur {str(row[k])[:60]})"))
        sub = schema["nested"].get(k)
        if sub:
            if isinstance(row[k], dict) and row[k]:
                _compare(f"{where}.{k}", row[k], sub, out)
            elif isinstance(row[k], list) and row[k] and isinstance(row[k][0], dict):
                _compare(f"{where}.{k}[0]", row[k][0], sub, out)


def check_tables(uid: int, payload: dict) -> list:
    """Problems in the tables of one response: [(level, message)]."""
    out = []
    table = payload.get("table") or {}
    hashes = payload.get("hash") or {}
    for op in ("replace", "put", "delete"):
        for name, rows in (table.get(op) or {}).items():
            where = f"{op} {name}"
            if name not in KNOWN_TABLES:
                out.append(("error", f"{where}: table inconnue du client (ignorée par le jeu)"))
                continue
            if not isinstance(rows, list):
                out.append(("error", f"{where}: doit être une liste, reçu {_kind(rows)}"))
                continue
            if op == "replace" and name in userdata.SINGLE_ROW and not rows:
                out.append(("error", f"{where}: replace vide sur une table à ligne unique (ignoré par le jeu)"))
            if op == "replace" and name in userdata.SINGLE_ROW and len(rows) > 1:
                out.append(("error", f"{where}: {len(rows)} lignes pour une table à ligne unique"))
            schema = SCHEMAS.get(name)
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    out.append(("error", f"{where}[{i}]: ligne {_kind(row)}, attendu un objet"))
                    continue
                if "userId" in row and row["userId"] != uid:
                    out.append(("error", f"{where}[{i}].userId = {row['userId']}, attendu {uid}"))
                if schema:
                    _compare(f"{where}[{i}]", row, schema, out)
            if rows and not schema:
                out.append(("info", f"{where}: pas de format de référence, non vérifié"))
            if op == "replace" and name in hashes and hashes[name] != userdata.table_hash(name, rows):
                out.append(("error", f"{where}: hash envoyé ≠ md5 des lignes (le client redemandera sans fin)"))
    for name in hashes:
        if name not in KNOWN_TABLES:
            out.append(("warn", f"hash {name}: table inconnue du client"))
    return out


# ---------------------------------------------------------------- client

class Client:
    """One player. Keeps the tables like the game does and checks every response."""

    def __init__(self, bench, uid: int):
        self.bench, self.uid = bench, uid
        self.tables, self.hashes = {}, {}
        self.seq = 0

    def call(self, route: str, body=None, expect_code=0) -> dict:
        self.seq += 1
        if body is None:
            body = {}
        body = dict(body)
        body.setdefault("lc", 5)
        headers = {"SAOAL-USER-ID": str(self.uid), "SAOAL-PLATFORM-USER-ID": f"test-device-{self.uid}",
                   "SAOAL-REQUEST-ID": "0", "SAOAL-REQUEST-SEQUENCE": str(self.seq),
                   "SAOAL-ENCRYPTION": "0", "CONTENT-TYPE": "application/json"}
        lines = []
        t0 = time.perf_counter()
        status, out, _, resp_headers = api.handle_api("POST", "/api/" + route, headers,
                                                      json.dumps(body).encode(), lines.append)
        ms = (time.perf_counter() - t0) * 1000
        payload = json.loads(out)
        problems = check_tables(self.uid, payload)
        code = payload.get("code", 0)
        if expect_code is not None and code != expect_code:
            problems.insert(0, ("error", f"code {code}, attendu {expect_code}"))
        if str(code) != resp_headers.get("SAOAL-SERVER-RESPONSE-CODE"):
            problems.append(("error", "en-tête SERVER-RESPONSE-CODE ≠ code"))
        self.bench.record(self, route, body, payload, problems, ms)
        self._apply(payload)
        return payload

    def _apply(self, payload: dict):
        table = payload.get("table") or {}
        for name, rows in (table.get("replace") or {}).items():
            self.tables[name] = list(rows)
        for name, rows in (table.get("put") or {}).items():
            self._put(name, rows)
        for name, rows in (table.get("delete") or {}).items():
            gone = {userdata._pk(name, r) for r in rows}
            self.tables[name] = [r for r in self.tables.get(name, []) if userdata._pk(name, r) not in gone]
        self.hashes.update(payload.get("hash") or {})

    def _put(self, name, rows):
        cur = self.tables.setdefault(name, [])
        if name in userdata.SINGLE_ROW:
            self.tables[name] = rows[-1:] or cur
            return
        index = {userdata._pk(name, r): i for i, r in enumerate(cur)}
        for r in rows:
            k = userdata._pk(name, r)
            if k in index:
                cur[index[k]] = r
            else:
                index[k] = len(cur)
                cur.append(r)

    # ---- the client's sync requests

    def pull(self, names=None) -> dict:
        names = names or list(TABLE_NAMES)
        return self.call("user/pull", {"hash": {n: self.hashes.get(n, "") for n in names}})

    def push(self, put=None, delete=None, trigger="test") -> dict:
        """What the game does after computing something itself (battle results, items, missions...)."""
        put, delete = put or {}, delete or {}
        before = {n: self.hashes.get(n, "") for n in set(put) | set(delete)}
        for name, rows in put.items():
            self._put(name, rows)
        for name, rows in delete.items():
            gone = {userdata._pk(name, r) for r in rows}
            self.tables[name] = [r for r in self.tables.get(name, []) if userdata._pk(name, r) not in gone]
        after = {n: userdata.table_hash(n, self.tables.get(n, [])) for n in before}
        self.hashes.update(after)
        return self.call("user/push", {"table": [{"trigger": trigger, "put": put, "delete": delete, "signature": 0}],
                                       "hash": {"before": before, "after": after}})

    # ---- helpers

    def t(self, name: str) -> list:
        return self.tables.get(name, [])

    def row(self, name: str) -> dict:
        rows = self.t(name)
        return rows[0] if rows else {}

    def item(self, item_id: int) -> int:
        return next((r["amount"] for r in self.t("item") if r["itemId"] == item_id), 0)

    def server_table(self, name: str) -> list:
        """What the server has stored (to compare with what the client believes)."""
        return userdata.table(self.uid, name)


# ---------------------------------------------------------------- bench

class Failed(Exception):
    pass


class Bench:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.calls = []  # (scenario, route, problems)
        self.scenario = None
        self.routes = set()

    def new_player(self, tutorial_done=True) -> Client:
        uid = db.create_user()
        c = Client(self, uid)
        c.pull()
        if tutorial_done:
            c.call("tutorial/skip/first")
            c.call("tutorial/skip/second")
        return c

    def record(self, client, route, body, payload, problems, ms):
        self.routes.add(route)
        self.calls.append((self.scenario, route, problems))
        if self.verbose or any(level == "error" for level, _ in problems):
            mark = "✗" if any(level == "error" for level, _ in problems) else "·"
            print(f"    {mark} [{client.uid}] {route} ({ms:.0f} ms) "
                  f"{json.dumps({k: v for k, v in body.items() if k != 'lc'}, ensure_ascii=False)[:120]}")
            for level, msg in problems:
                if level != "info" or self.verbose:
                    print(f"        {level}: {msg}")

    def note(self, cond, msg: str):
        """Doubtful behaviour worth a warning, without failing the scenario."""
        if cond:
            self.calls.append((self.scenario, "(remarque)", [("warn", msg)]))
            print(f"    ! {msg}")

    def expect(self, cond, msg: str):
        """Game-logic assertion inside a scenario."""
        if not cond:
            raise Failed(msg)
