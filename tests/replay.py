"""Replay a player's real requests (server/logs/api_requests.jsonl) on a local copy, with the bench's checks.

usage: python tests/replay.py <api_requests.jsonl> --user UID [--db saoub.sqlite3] [--since HH:MM] [--until HH:MM] [-v]
  --db     start from a copy of that database (e.g. a server's): the account is exactly as it was;
           without it, the requests run on a brand-new account (state may differ from the real one)
  --since/--until  only requests in that time window (local time of the log, as in server.log)
Nothing is written to the given database or to the real logs: everything runs on a temporary copy.

Inputs: a server's logs/api_requests.jsonl and, optionally, its saoub.sqlite3 copied to a local folder.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--user", type=int, required=True)
    ap.add_argument("--db")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()

    if args.db:
        copy = os.path.join(tempfile.mkdtemp(prefix="saoub_replay_"), "replay.sqlite3")
        shutil.copy(args.db, copy)
        os.environ["SAOUB_DB"] = copy
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import harness  # noqa: PLC0415 (after SAOUB_DB is set)

    def hhmm(t):
        return time.strftime("%H:%M", time.localtime(t))

    entries = []
    with open(args.log, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not e.get("path", "").startswith("/api/"):
                continue
            uid = int((e.get("headers") or {}).get("SAOAL-USER-ID") or 0)
            if uid != args.user:
                continue
            t = hhmm(e["t"])
            if (args.since and t < args.since) or (args.until and t > args.until):
                continue
            entries.append(e)
    if not entries:
        print("aucune requête pour ce joueur dans cette plage")
        return

    bench = harness.Bench(verbose=args.v)
    bench.scenario = f"replay {args.user}"
    if args.db and harness.db.get_user(args.user):
        client = harness.Client(bench, args.user)
        print(f"compte {args.user} repris de {args.db}")
    else:
        uid = harness.db.create_user()
        client = harness.Client(bench, uid)
        print(f"compte {args.user} absent : rejoué sur un compte neuf ({uid})")
    # the client's view of its tables: what the server had before the first replayed request
    data = harness.userdata.load(client.uid)
    client.tables = {k: list(v) for k, v in data["tables"].items()}
    client.hashes = dict(data["hash"])

    print(f"{len(entries)} requêtes de {hhmm(entries[0]['t'])} à {hhmm(entries[-1]['t'])}")
    crashes = 0
    for e in entries:
        route = e["path"][len("/api/"):]
        body = harness.userdata._rekey(e.get("body") or {}, client.uid) if isinstance(e.get("body"), dict) else {}
        try:
            client.call(route, body, expect_code=None)
        except Exception as exc:  # noqa: BLE001
            crashes += 1
            print(f"    ✗ {hhmm(e['t'])} {route}: exception {type(exc).__name__}: {exc}")
    errors = [(r, m) for _, r, probs in bench.calls for lvl, m in probs if lvl == "error"]
    warns = [(r, m) for _, r, probs in bench.calls for lvl, m in probs if lvl == "warn"]
    print(f"\n{len(bench.calls)} requêtes rejouées : {crashes} exception(s), {len(errors)} erreur(s), "
          f"{len(warns)} avertissement(s)")
    seen = set()
    for route, msg in errors + warns:
        if (route, msg) not in seen:
            seen.add((route, msg))
            print(f"  {route}: {msg}")
    harness.cleanup()


if __name__ == "__main__":
    main()
