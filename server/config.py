import json
import os
import time
from datetime import datetime

# 127.0.0.1 = this PC only (default). Set SAOUB_HOST=0.0.0.0 to accept phones / emulators on the network.
HOST = os.environ.get("SAOUB_HOST", "127.0.0.1")
PORT = int(os.environ.get("SAOUB_PORT", "8080"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# offline XAPK: game files packed in split APKs (assets/saoub_pack/hashed/...), read in place
PACK_APKS = [p for p in os.environ.get("SAOUB_PACK_APKS", "").split(os.pathsep) if p]
# files only found in an APK are copied once to server_cache/apk_hashed to be streamed with sendfile; off on the
# phone (it would duplicate GBs), they are read from the APK on each request instead
APK_FILE_CACHE = os.environ.get("SAOUB_APK_FILE_CACHE", "1") != "0"
# Game data (built from your own copy of the game, see README.md): SAOUB_DATA if set -> <repo>/dev_data when present
# -> <repo>/data.
_DEV_DATA = os.path.join(ROOT, "dev_data")
DATA_DIR = os.environ.get("SAOUB_DATA") or (_DEV_DATA if os.path.isdir(os.path.join(_DEV_DATA, "server_cache"))
                                            else os.path.join(ROOT, "data"))
# the game's APK assets: SAOUB_APK, else <data>/game.apk (tools/setup_data.py), else the original APK at the root
APK_PATH = os.environ.get("SAOUB_APK") or next(
    (p for p in (os.path.join(DATA_DIR, "game.apk"), os.path.join(ROOT, "SAO+Unleash+Blading_3.7.0_APKPure.apk"))
     if os.path.exists(p)), os.path.join(ROOT, "SAO+Unleash+Blading_3.7.0_APKPure.apk"))
# BRAND goes out in every game-server response (X-SAOUB-Server) and in the welcome announcement of a new database;
# INSTANCE identifies this install (server_cache/instance.json, absent on the VPS), sent as X-SAOUB-Instance.
BRAND = os.environ.get("SAOUB_BRAND", "Re:Rising - SAO Unleash Blading revival server")


def _instance() -> str:
    try:
        with open(os.path.join(DATA_DIR, "server_cache", "instance.json"), encoding="utf-8") as f:
            return str(json.load(f).get("instance", ""))[:64]
    except (OSError, ValueError, AttributeError):
        return ""


INSTANCE = _instance()
DUMP_CONTENTS = os.environ.get("SAOUB_DUMP") or os.path.join(DATA_DIR,
                                                            "dump/data/data/com.bandainamcoent.saoars/files/contents")
# the asset manifests live with the game files, except in the offline APK (manifests in the data, game files in a
# separate pack the player copies)
MANIFEST_DIR = os.environ.get("SAOUB_MANIFEST_DIR") or os.path.join(DUMP_CONTENTS, "manifest")
REMOTE_LOG_LEVEL = int(os.environ.get("SAOUB_REMOTE_LOG_LEVEL", "0"))
START_FREE_GEMS = int(os.environ.get("SAOUB_FREE_GEMS", "0"))
# new accounts: "fresh" = start of the game (tutorials, prologue); "template" = the archived early-game account
NEW_ACCOUNT = os.environ.get("SAOUB_NEW_ACCOUNT", "fresh")
# step-up banner bonuses (boosted rates / guaranteed last draw); "0" disables them (debugging)
LOTTERY_STEP_BONUS = os.environ.get("SAOUB_LOTTERY_STEP_BONUS", "1") != "0"
# base.bin release windows kept open until the end of service (comma-separated labels; "" = original file).
# Season 9 ended on 2022/10/28, before the game clock: no Season Pass at all without it.
EXTEND_WINDOWS = tuple(l.strip() for l in os.environ.get(
    "SAOUB_EXTEND_WINDOWS", "release.season_9,release.season_9_reward").split(",") if l.strip())
# game files the archived dump lacks, restored from a decrypted iOS IPA (tools/restore_from_ipa.py)
RESTORED_DIR = os.environ.get("SAOUB_RESTORED_DIR") or os.path.join(DATA_DIR, "restored")
# files only the iOS app bundles (tools/restore_from_ipa.py --ios-only), listed in the iOS manifest only
IOS_ONLY_DIR = os.environ.get("SAOUB_IOS_ONLY_DIR") or os.path.join(DATA_DIR, "ios_only")
LOG_DIR = os.environ.get("SAOUB_LOG_DIR", os.path.join(ROOT, "server", "logs"))
LOG_MAX_BYTES = int(os.environ.get("SAOUB_LOG_MAX_BYTES", "0"))  # 0 = unlimited; else keep one rotated copy


def log_file(name: str) -> str:
    """Path of a log file in LOG_DIR, rotated to <name>.1 once it exceeds LOG_MAX_BYTES."""
    path = os.path.join(LOG_DIR, name)
    if LOG_MAX_BYTES and os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
        os.replace(path, path + ".1")
    return path
DB_PATH = os.environ.get("SAOUB_DB", os.path.join(ROOT, "server", "saoub.sqlite3"))


# Game clock. The client opens banners/events from release windows that all ended with the service (2023-01-16),
# so the server reports a time inside the last window instead of the real date, looping over it:
#   game time = WINDOW_START + ((real time - anchor) mod window length)
# SAOUB_GAME_WINDOW="<start>/<end>" (ISO 8601); empty disables it (real time).
GAME_WINDOW = os.environ.get("SAOUB_GAME_WINDOW", "2022-11-20T06:00:00+09:00/2023-01-16T05:00:00+09:00")
# real time that maps to the middle of the window (so the first loop leaves weeks before wrapping)
GAME_ANCHOR = float(os.environ.get("SAOUB_GAME_ANCHOR", "1791590400"))  # 2026-10-10T08:00:00Z


def _window():
    if not GAME_WINDOW:
        return None
    start, end = (datetime.fromisoformat(x).timestamp() for x in GAME_WINDOW.split("/"))
    return start, end - start


_WINDOW = _window()
DEFAULT_GAME_WINDOW = GAME_WINDOW  # the environment's (or built-in) value, for the admin panel's "default" button


def set_game_window(spec: str) -> None:
    """"<start>/<end>" in ISO 8601, or "" for real time (admin panel, issue #29); same parser as at startup."""
    global GAME_WINDOW, _WINDOW
    old = GAME_WINDOW
    GAME_WINDOW = spec
    try:
        _WINDOW = _window()
    except ValueError:
        GAME_WINDOW = old
        raise


# "Raid mode" (admin panel, server page): the raid of the last service weeks (War of Underworld 18, event
# 601101000200) was only open 2022-11-20 06:00 -> 11-24 05:59 JST, 4 days of the ~57-day loop above. While raid mode is
# on, the game clock loops over those 4 days instead (the other events shown are then the ones of that week).
RAID_WINDOW = os.environ.get("SAOUB_RAID_WINDOW", "2022-11-20T06:00:00+09:00/2022-11-24T05:59:00+09:00")
RAID_MODE = False


def set_raid_mode(on: bool):
    global RAID_MODE
    RAID_MODE = bool(on)


def now() -> float:
    """Current game time (epoch seconds) as reported to the client; see GAME_WINDOW and RAID_MODE."""
    real = time.time()
    if RAID_MODE and RAID_WINDOW:
        start, end = (datetime.fromisoformat(x).timestamp() for x in RAID_WINDOW.split("/"))
        # one hour in: some event code treats "exactly at the opening" as not yet open
        return start + 3600 + ((real - GAME_ANCHOR) % (end - start - 7200))
    if _WINDOW is None:
        return real
    start, length = _WINDOW
    return start + ((real - GAME_ANCHOR + length / 2) % length)
