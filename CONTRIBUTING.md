# Contributing to Re:Rising

Thanks for helping bring SAO Unleash Blading back! Bugs are plentiful, so every fix counts.

## Ground rules

1. **No game content, ever.** Do not commit or attach to issues/PRs: APKs, IPAs, XAPKs, game files (`hashed/`,
   manifests), decoded master data (`master_json`, `server_cache`), extracted images/sounds, or decompiled game
   code. Describe the behaviour in words, with ids and field names instead.
2. **No personal data or secrets.** No player databases (`*.sqlite3`), request logs (`*.jsonl`), logcats, device
   ids, GREE uuids, passwords, tokens or keys. Redact before you paste a log.
3. **Local-first.** The server must keep working on a single PC with no internet and no third-party service.
   Network exposure stays opt-in (`SAOUB_HOST=0.0.0.0`).

## Workflow

1. Open an issue (bug: what you did, what the game showed, what the server log says) or comment on an existing one.
2. Fork, branch from `main`, keep the change focused.
3. Add or update a scenario in `tests/scenarios/` when you change server behaviour; run `python tests/run.py`.
4. Open a pull request explaining *why*, and how you checked it in the game (emulator or phone).

## Code style

- Python 3.11+, standard library first (`msgpack` and `pycryptodome` are the only dependencies).
- Match the surrounding code: comments explain *why* and cite the client behaviour that justifies a choice
  (e.g. a master table field, a native function address).
- Configuration goes through environment variables in `server/config.py` (and `.env.example`), never hard-coded
  paths or hosts.
