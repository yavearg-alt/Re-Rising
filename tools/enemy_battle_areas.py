"""Move the battles and stages of the background-less areas to a similar area that has one (master patch).

usage: enemy_battle_areas.py <enemy.enc|quest.enc> <out.enc>   (server/assets.py applies it to the served files)

An enemy party (enemy.bin sub 2) is fought in its battle area, field 26 (a map.5 area): the client draws the 2D
`files/bg/battle_bg/<area>.png` or the 3D `files/areaAtlas/<map.5 field 19>/` of that area. 11 battle areas have
neither in the 3.7.0 data; Android fights them on an empty background, the iOS client crashes as soon as the battle
loads (main quests M-1-2 / M-1-3 in sao_field, 2026-10-20; docs/qa/ios-crash-1-M1-2-2026-10-20.md). A 2D
background for them alone was not enough (tools/battle_bg_placeholders.py): their parties now point at the area
whose background they borrow. Same in-place XXTEA rewrite as tools/base_windows.py (8-byte encrypted int32, same
file size).
Story stages set in those areas crashed the same way (M-1-4 opens on a story stage in sao_field): quest.bin
sub 10 (stages) field 6 (the stage's area) is moved too.
"""
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from battle_bg_placeholders import PLACEHOLDERS  # noqa: E402  area without background -> similar area
from saoub_crypto import decrypt, encrypt, xxtea_decrypt, xxtea_encrypt  # noqa: E402

# master file -> (sub-table, area field): enemy parties' battle area, quest stages' area
TARGETS = {"master/divided/enemy.enc": (2, 26), "master/divided/quest.enc": (10, 6)}


def _u32(d, p):
    return struct.unpack_from("<I", d, p)[0]


def _fields(d, table):
    vt = table - struct.unpack_from("<i", d, table)[0]
    vlen = struct.unpack_from("<H", d, vt)[0]
    return [struct.unpack_from("<H", d, vt + 4 + 2 * i)[0] for i in range((vlen - 4) // 2)]


def move_battle_areas(flat: bytes, mapping: dict = PLACEHOLDERS, sub: int = 2, field: int = 26) -> tuple[bytes, dict]:
    """(patched bytes, {area: rows moved}) for the rows of sub-table `sub` whose area `field` is a key of `mapping`."""
    out = bytearray(flat)
    root = _u32(out, 0)
    offs = _fields(out, root)
    vec = root + offs[sub] + _u32(out, root + offs[sub])
    moved = {}
    for i in range(_u32(out, vec)):
        q = vec + 4 + 4 * i
        table = q + _u32(out, q)
        foffs = _fields(out, table)
        if field >= len(foffs) or not foffs[field]:
            continue
        p = table + foffs[field]
        plain = xxtea_decrypt(bytes(out[p:p + 8]))
        if plain is None or len(plain) != 4:
            continue
        area = struct.unpack("<i", plain)[0]
        if area not in mapping:
            continue
        enc = xxtea_encrypt(struct.pack("<i", mapping[area]), words=2)
        assert len(enc) == 8 and xxtea_decrypt(enc) == struct.pack("<i", mapping[area])
        out[p:p + 8] = enc
        moved[area] = moved.get(area, 0) + 1
    assert len(out) == len(flat)
    return bytes(out), moved


def move_battle_areas_enc(raw: bytes, master_path: str, mapping: dict = PLACEHOLDERS) -> tuple[bytes, dict]:
    """Same on the downloaded (AES + zlib) file of `master_path` (a key of TARGETS)."""
    sub, field = TARGETS[master_path]
    flat, moved = move_battle_areas(decrypt(raw), mapping, sub, field)
    return encrypt(flat), moved


def mapping_tag(mapping: dict = PLACEHOLDERS) -> str:
    """Changes with the mapping and the files patched (clients then read the manifest again)."""
    return hashlib.md5(repr((sorted(mapping.items()), sorted(TARGETS.items()))).encode()).hexdigest()[:6]


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2])
        return 1
    raw = open(argv[1], "rb").read()
    target = "master/divided/quest.enc" if "quest" in os.path.basename(argv[1]) else "master/divided/enemy.enc"
    out, moved = move_battle_areas_enc(raw, target)
    open(argv[2], "wb").write(out)
    print(f"moved {moved}; md5 {hashlib.md5(raw).hexdigest()} -> {hashlib.md5(out).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
