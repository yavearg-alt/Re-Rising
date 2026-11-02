"""Build the server's game data folder from the public archives of SAO Unleash Blading 3.7.0, in one command.

usage: python tools/setup_data.py [--data DIR] [--dump ZIP] [--game XAPK|APKM|APK] [--keep-downloads] [--all-tables]

  --data  the folder to fill (default: $SAOUB_DATA, else <repo>/data)
  --dump  the /data/data dump of a player's device, com.bandainamcoent.saoars-3.7.0.4728.zip (4.2 GB)
  --game  the game: the 3.7.0 (4728) XAPK of the archive, an APKMirror .apkm bundle, or a single merged APK
  Without --dump / --game, both are downloaded from the Internet Archive (item gacha-archive, ~5 GB, resumable)
  into <data>/downloads and checked against their MD5. --keep-downloads keeps them afterwards (default: deleted
  once everything is extracted). Needs ~10 GB free during the run, ~5.5 GB after.

What it builds (the server reads it through SAOUB_DATA, see README.md):
  dump/data/data/com.bandainamcoent.saoars/files/  game files (contents/hashed, contents/manifest) + LevelDB (ldb)
  game.apk                     the game assets of the APK (base + AssetPack1), served for files the dump lacks
  apk/config.arm64_v8a.apk     the 64-bit split, for `ARM64=... tools/build_apk.sh` (when the source has it)
  extracted/master_final       decrypted master data             (tools/extract_dump.py)
  extracted/master_json        the master tables the server reads, as JSON (tools/dump_masters.py; --all-tables: all)
  extracted/manifest           manifests as JSON + resolved_paths.json (hashed name -> logical path)
  extracted/sample_user        new-account template: the archived account of the dump (tools/import_account.py)
The server builds server_cache/ by itself at its first start.

Nothing here is redistributed by this project: the files come from the public archive (or your own copies).
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

ARCHIVE = "https://archive.org/download/gacha-archive/"
ARCHIVE_DIR = "Online/Sword Art Online - Unleash Blading/"
DUMP = ("com.bandainamcoent.saoars-3.7.0.4728.zip", "bdfef9d0cd946dbd7b99d16bd0bf52c3")
XAPK = ("com.bandainamcoent.saoars-3.7.0.4728.xapk", "86de5d8228709d1038836c3866fc2d18")
APP = "data/data/com.bandainamcoent.saoars/files/"
KEEP = (APP + "contents/hashed/", APP + "contents/manifest/", APP + "ldb/")
# split names in the archive's XAPK / in an APKMirror .apkm
BASE = ("com.bandainamcoent.saoars.apk", "base.apk")
ASSET_PACK = ("AssetPack1.apk", "split_AssetPack1.apk")
ARM64 = ("config.arm64_v8a.apk", "split_config.arm64_v8a.apk")
# master tables the server reads as JSON (master.py _json_rows / JSON_DIR, rows("<table>.<n>.json") in master.py and
# idspage.py); it decodes the others from extracted/master_final when needed. --all-tables converts every table.
JSON_TABLES = ("asset", "base", "enemy", "equipment", "event", "guild", "item", "localization", "login_bonus",
               "lottery", "mission", "pc", "player", "pvp", "quest", "raid_event", "reward", "season")
# account tables the client keeps as one row (sample_user stores them as an object, not a list)
SINGLE_ROW = {"anotherForce", "battleLog", "boostSetting", "homeSetting", "info", "lotteryResult", "position",
              "pvpResult", "pvpStatus", "questResult", "questSetting", "questStatus", "questSupporterSlot",
              "raidEventWorldBossDailyResult", "raidEventWorldBossDefeatResult", "rushEventStatus", "setting",
              "towerEventStatus"}


def step(text):
    print(f"\n== {text}", flush=True)


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def download(name: str, md5: str, folder: str) -> str:
    """The archive file `name` in `folder`, downloaded (resumed) when missing or incomplete, MD5-checked."""
    dest = os.path.join(folder, name)
    if os.path.exists(dest) and md5_of(dest) == md5:
        print(f"{name}: already downloaded")
        return dest
    os.makedirs(folder, exist_ok=True)
    url = ARCHIVE + urllib.parse.quote(ARCHIVE_DIR + name)
    print(f"{name}: downloading from the Internet Archive")
    # curl first: resumes (-C -) and uses the system certificate store (Python's own store rejects some
    # archive.org storage nodes)
    if shutil.which("curl"):
        subprocess.run(["curl", "-L", "--fail", "-C", "-", "--retry", "5", "-o", dest, url], check=True)
    else:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 22)
    got = md5_of(dest)
    if got != md5:
        sys.exit(f"{name}: MD5 {got}, expected {md5} (incomplete or different file: delete it and run again)")
    return dest


def unpack_dump(dump: str, data: str):
    """The game files, manifests and LevelDB of the dump; already unpacked files (same size) are skipped."""
    with zipfile.ZipFile(dump) as z:
        entries = [i for i in z.infolist() if i.filename.startswith(KEEP) and not i.is_dir()]
        done = 0
        for k, info in enumerate(entries, 1):
            dest = os.path.join(data, "dump", info.filename)
            if os.path.exists(dest) and os.path.getsize(dest) == info.file_size:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(info) as src, open(dest + ".part", "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.replace(dest + ".part", dest)
            done += 1
            if k % 5000 == 0:
                print(f"  {k}/{len(entries)}", flush=True)
    print(f"{len(entries)} files ({done} unpacked now)")


def _split(bundle: zipfile.ZipFile, names) -> zipfile.ZipFile | None:
    name = next((n for n in names if n in bundle.namelist()), None)
    return zipfile.ZipFile(io.BytesIO(bundle.read(name))) if name else None


def build_game_apk(game: str, data: str):
    """<data>/game.apk: every assets/ entry of the game (bundled manifests, game files of the APK and of its
    AssetPack1). Not installable: the server only reads files from it. Also keeps the 64-bit split."""
    out = os.path.join(data, "game.apk")
    with zipfile.ZipFile(game) as bundle:
        sources = [s for s in (_split(bundle, BASE), _split(bundle, ASSET_PACK)) if s]
        if not sources:  # a single merged APK (e.g. APKPure)
            sources = [bundle]
        arm64 = _split(bundle, ARM64)
        if arm64:
            os.makedirs(os.path.join(data, "apk"), exist_ok=True)
            name = next(n for n in ARM64 if n in bundle.namelist())
            with open(os.path.join(data, "apk", "config.arm64_v8a.apk"), "wb") as f:
                f.write(bundle.read(name))
            print("64-bit split kept: apk/config.arm64_v8a.apk")
        count = 0
        with zipfile.ZipFile(out + ".part", "w", zipfile.ZIP_STORED) as z:
            seen = set()
            for src in sources:
                for info in src.infolist():
                    if info.filename.startswith("assets/") and info.filename not in seen and not info.is_dir():
                        seen.add(info.filename)
                        with src.open(info) as r, z.open(zipfile.ZipInfo(info.filename, info.date_time), "w") as w:
                            shutil.copyfileobj(r, w, 1 << 20)
                        count += 1
    os.replace(out + ".part", out)
    if count < 1000:
        sys.exit(f"{game}: only {count} assets found, is it SAO Unleash Blading 3.7.0?")
    print(f"game.apk: {count} assets")


def run_tool(*args):
    subprocess.run([sys.executable, *args], check=True, cwd=ROOT)


def resolved_paths(data: str):
    """hashed name -> logical path for every manifest file whose path the asset table knows."""
    from saoub_crypto import hashed_name
    ext = os.path.join(data, "extracted")
    with open(os.path.join(ext, "master_json", "asset.0.json"), encoding="utf-8") as f:
        paths = [r["0"] for r in json.load(f)]
    with open(os.path.join(ext, "manifest", "project_android_zip.json"), encoding="utf-8") as f:
        assets = json.load(f)["assets"]
    out = {}
    for p in paths:
        h = hashed_name(p)
        key = f"hashed/{h[:2]}/{h}"
        if key in assets:
            out[key] = p
    with open(os.path.join(ext, "manifest", "resolved_paths.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"resolved_paths.json: {len(out)} paths")


def sample_user(data: str):
    """The archived account of the dump, one file per client table (the server's new-account template)."""
    import import_account
    account = import_account.extract(os.path.join(data, "dump"))
    with open(os.path.join(ROOT, "server", "table_names.json"), encoding="utf-8") as f:
        names = json.load(f)
    out = os.path.join(data, "extracted", "sample_user")
    os.makedirs(out, exist_ok=True)
    for short, rows in account["tables"].items():
        value = rows[0] if short in SINGLE_ROW and len(rows) == 1 else rows
        with open(os.path.join(out, names[short] + ".json"), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
    for long_name in account["source"]["unknownTables"]:  # kept empty: the server has no rule for them
        with open(os.path.join(out, long_name + ".json"), "w", encoding="utf-8") as f:
            json.dump([], f)
    print(f"sample_user: {len(account['tables'])} tables ({account['source']['name']}, client "
          f"{account['source']['clientVersion']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("SAOUB_DATA") or os.path.join(ROOT, "data"))
    ap.add_argument("--dump")
    ap.add_argument("--game")
    ap.add_argument("--keep-downloads", action="store_true")
    ap.add_argument("--all-tables", action="store_true")
    args = ap.parse_args()
    data = os.path.abspath(args.data)
    os.makedirs(data, exist_ok=True)
    downloads = os.path.join(data, "downloads")

    step("1/6 game files (dump)")
    dump = args.dump or download(*DUMP, downloads)
    unpack_dump(dump, data)
    step("2/6 game assets (APK)")
    game = args.game or download(*XAPK, downloads)
    build_game_apk(game, data)
    step("3/6 manifests and master data")
    run_tool("tools/extract_dump.py", os.path.join(data, "game.apk"), os.path.join(data, "dump"),
             os.path.join(data, "extracted"))
    step("4/6 master tables as JSON")
    # a few workers: each holds a whole table in memory, one per core can exhaust the RAM
    run_tool("tools/dump_masters.py", os.path.join(data, "extracted", "master_final"),
             os.path.join(data, "extracted", "master_json"), str(max(1, min(os.cpu_count() or 1, 6))),
             "" if args.all_tables else ",".join(JSON_TABLES))
    step("5/6 resolved paths")
    resolved_paths(data)
    step("6/6 new-account template")
    sample_user(data)
    os.makedirs(os.path.join(data, "server_cache"), exist_ok=True)  # filled by the server at its first start

    if not args.keep_downloads and os.path.isdir(downloads):
        shutil.rmtree(downloads)
        print("\ndownloads deleted (--keep-downloads to keep them)")
    print(f"\nDone: {data}\nStart the server with SAOUB_DATA={data} (default when it is <repo>/data), "
          "or run the tests: python tests/run.py")


if __name__ == "__main__":
    main()
