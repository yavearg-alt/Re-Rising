"""Decrypt the bundled manifest and master data tables from the APK."""
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from saoub_crypto import decrypt, hashed_name  # noqa: E402

MASTER_TABLES = [
    "area_enemy", "asset", "base", "battle", "custom", "editor", "emblem", "enemy", "environment",
    "equipment", "event", "guild", "home", "imagination", "item", "localization", "login_bonus",
    "lottery", "map", "message", "mission", "novel", "player", "pvp", "quest", "raid_event",
    "reward", "scenario", "season", "skill", "spine", "treasure", "tutorial",
]

if __name__ == "__main__":
    apk_path, out_dir = sys.argv[1], sys.argv[2]
    z = zipfile.ZipFile(apk_path)
    names = set(z.namelist())

    for m in ("bundled.manifestenc", "bundled_zip.manifestenc"):
        data = decrypt(z.read(f"assets/manifest/{m}"))
        os.makedirs(f"{out_dir}/manifest", exist_ok=True)
        with open(f"{out_dir}/manifest/{m.replace('.manifestenc', '.json')}", "wb") as f:
            f.write(data)
        print(m, len(data), "bytes")

    os.makedirs(f"{out_dir}/master_apk", exist_ok=True)
    for t in MASTER_TABLES:
        h = hashed_name(f"master/divided/{t}.enc")
        entry = f"assets/hashed/{h[:2]}/{h}"
        if entry not in names:
            print("missing", t)
            continue
        data = decrypt(z.read(entry))
        with open(f"{out_dir}/master_apk/{t}.bin", "wb") as f:
            f.write(data)
        print(f"{t:14} {len(data):>10} bytes")
