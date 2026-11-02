"""Battle backgrounds for the battle areas that have none, for the iOS manifest.

usage: battle_bg_placeholders.py [--check]   (writes into <data>/ios_only and merges <data>/ios_only/index.json)

A battle is drawn over `files/bg/battle_bg/<area>.png` (enemy.bin sub 2 field 26 = the party's battle area,
map.5 row of that area; event quests name theirs in event.bin) or over the area's 3D model
(`files/areaAtlas/<map.5 field 19>/*.c3benc`). 11 battle areas have neither in the 3.7.0 data (sao_field of main
quest M-1-2, sao_forest, ...). Android draws the battle without a background; the iOS client crashes on the missing
image (SIGSEGV 0x8 in toybox::AsyncSprite::createTextureWithDictAsync on wfs.async.io: every iPhone at the first
battle of M-1-2, 2026-10-20; see docs/qa/ios-crash-1-M1-2-2026-10-20.md). Each one gets the background of a
similar area: the .pngenc files use the common key (no per-file key in asset.0), so a copy of the bytes is a valid
file under the other name. iOS manifest only (assets.py adds <data>/ios_only/index.json to it).
Run again after restore_from_ipa.py --ios-only / pkm_to_pvr.py, which rewrite index.json.
"""
import contextlib
import hashlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "server"))
from saoub_crypto import decrypt, hashed_name  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    import assets  # noqa: E402
    import config  # noqa: E402

# battle area without a background -> area whose background it borrows (map.5 field 19 names in the comments)
PLACEHOLDERS = {
    511101001: 511004001,  # sao_field <- uw_field_battle (main quest M-1-2, Town of Beginnings surroundings)
    511106001: 511004001,  # sao_memory_hill <- uw_field_battle
    511102001: 511002001,  # sao_forest <- uw_forest_battle_afternoon
    511127001: 511002001,  # sao_misty_forest <- uw_forest_battle_afternoon
    511002003: 511002001,  # uw_forest_battle_evening <- uw_forest_battle_afternoon
    511109001: 511035001,  # sao_west_mountain_top <- uw_dark_rocky_mountain
    511003001: 511004005,  # uw_rulid_night <- uw_field_night_battle
    511013002: 511013001,  # uw_cathedral_96th_hallway_to_chief <- uw_cathedral_96th_hallway_to_senate
    511017001: 511010002,  # uw_inner_spirit <- uw_cathedral_80th_cloudtop_garden_none
    511006004: 511006001,  # uw_arena_school_senior_noon <- uw_arena_school_senior_evening
    511121001: 511114001,  # ggo_labyrinth <- ggo_underground
}


def key(path: str) -> str:
    h = hashed_name(path)
    return f"hashed/{h[:2]}/{h}"


def main():
    check = "--check" in sys.argv
    store = assets.AssetStore()
    served = store.project["assets"]
    index_path = os.path.join(config.IOS_ONLY_DIR, "index.json")
    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)
    made = 0
    for area, source in sorted(PLACEHOLDERS.items()):
        dst, src = f"files/bg/battle_bg/{area}.pngenc", f"files/bg/battle_bg/{source}.pngenc"
        if key(dst) in served:
            print(f"{area}: has its own background now, skipped")
            continue
        raw = store.hashed(hashed_name(src))
        entry = served.get(key(src))
        if raw is None or entry is None:
            raise SystemExit(f"source background missing: {src}")
        if decrypt(raw)[:4] != b"\x89PNG":
            raise SystemExit(f"source is not a PNG: {src}")
        name = hashed_name(dst)
        if check:
            print(f"{area} <- {source}: {'in index' if key(dst) in index else 'NOT in index'}")
            continue
        out = os.path.join(config.IOS_ONLY_DIR, "hashed", name[:2], name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(raw)
        new = {k: v for k, v in entry.items() if k not in ("assetPack", "downloadState")}
        new.update(md5=hashlib.md5(raw).hexdigest(), size=len(raw),
                   path=entry["path"].rsplit("/hashed/", 1)[0] + f"/hashed/{name[:2]}/{name}")
        index[key(dst)] = new
        made += 1
    if not check:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f)
        print(f"{made} battle backgrounds added -> {config.IOS_ONLY_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
