"""Build a SAO UB APK whose API / CDN URLs point to a private server.

usage: patch_apk.py <original.apk> <out.apk> <server_base_url> [cdn_base_url] [--arm64 <split or .apkm>]
  cdn_base_url: where the game files are downloaded (default <server_base_url>), e.g. a local API server with
  another server as CDN: patch_apk.py in.apk out.apk http://10.0.2.2:8080 http://<cdn-host>:<port>
   ex: patch_apk.py "SAO+Unleash+Blading_3.7.0_APKPure.apk" saoub_private.apk http://10.0.2.2:8080

The URLs live in .rodata and are copied with an explicit length (`movs r2, #len` right before the
std::string assign at 0xa26dcc), so both the string bytes and the length immediates are patched.

--arm64: the 3.7.0 (4728) build sold on Google Play also had a 64-bit version, shipped as the split
config.arm64_v8a.apk (in an APKMirror .apkm bundle: split_config.arm64_v8a.apk, signed by NAMCO BANDAI Games Inc.
like the base). Its native libraries are added under lib/arm64-v8a/, so the APK also runs on 64-bit-only devices
and emulators (Android picks arm64-v8a there, armeabi-v7a elsewhere). The arm64 libapp.so is patched the same
way: same strings, length loaded by `mov w2, #len` right before the std::string assign (bl 0x11ec294).
Signing (zipalign + apksigner) is done by build_apk.ps1 / the caller.
"""
import os
import struct
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LIB = "lib/armeabi-v7a/libapp.so"
LIB64 = "lib/arm64-v8a/libapp.so"
ARM64_SPLIT = "split_config.arm64_v8a.apk"

# original string -> addresses of the Thumb `movs r2, #len` that load its length
URLS = {
    "https://api.saoab.wfs.games": [0xA26732],
    "https://api.saoab-ap.wfs.games": [0xA26740],
    "https://api.saoab-eu.wfs.games": [0xA2674E],
    "https://api.saoab-us.wfs.games": [0xA2675C],
    "https://saoab-assets-jp.akamaized.net": [0xA2676A],
    "https://saoab-assets-ap.akamaized.net": [0xA26778, 0xA26A78],
    "https://saoab-assets-eu.akamaized.net": [0xA26786, 0xA26A84],
    "https://saoab-assets-us.akamaized.net": [0xA26794, 0xA26A6C, 0xA26A90],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/jp": [0xA267A2],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/ap": [0xA267B0],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/us": [0xA267BE],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/eu": [0xA267CC],
}
RODATA = 0x223CD60

# arm64 libapp.so (BuildID 858534fc...): `mov w2, #len` (movz, 0x52800002 | len << 5) for the same strings
URLS64 = {
    "https://api.saoab.wfs.games": [0x11EB95C],
    "https://api.saoab-ap.wfs.games": [0x11EB970],
    "https://api.saoab-eu.wfs.games": [0x11EB984],
    "https://api.saoab-us.wfs.games": [0x11EB998],
    "https://saoab-assets-jp.akamaized.net": [0x11EB9AC],
    "https://saoab-assets-ap.akamaized.net": [0x11EB9C0, 0x11EBE10],
    "https://saoab-assets-eu.akamaized.net": [0x11EB9D4, 0x11EBE24],
    "https://saoab-assets-us.akamaized.net": [0x11EB9E8, 0x11EBDFC, 0x11EBE38],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/jp": [0x11EB9FC],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/ap": [0x11EBA10],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/us": [0x11EBA24],
    "https://storage.googleapis.com/alice-assets-6ef2e5a0/eu": [0x11EBA38],
}
RODATA64 = 0x2C8D000

# Strings read with strlen (std::string from a C string), so only the bytes need patching.
# GREE gamelib account/payment server, passed to the Java SDK (0xa26940, region table 0x27589e8).
CSTRINGS = {
    "https://bn-payment-us.wrightflyer.net": "/gree",
    "https://bn-payment.wrightflyer.net": "/gree",
}


def replacement(orig: str, base: str, cdn: str = "") -> str:
    if "api.saoab" in orig:
        return base
    if "akamaized" in orig:
        return (cdn or base) + "/cdn"
    return base + "/gcs/" + orig[-2:]


def _length_insn(arm64: bool, length: int) -> tuple:
    """(struct format, encoding) of the instruction that loads a string length."""
    return ("<I", 0x52800002 | length << 5) if arm64 else ("<H", 0x2200 | length)


def patch_lib(lib: bytes, base: str, cdn: str = "", arm64: bool = False) -> bytes:
    lib = bytearray(lib)
    urls, rodata = (URLS64, RODATA64) if arm64 else (URLS, RODATA)
    for orig, sites in urls.items():
        new = replacement(orig, base, cdn)
        if len(new) > len(orig) or len(new) > 0xFF:
            raise SystemExit(f"URL too long: {new!r} (max {len(orig)} chars)")
        needle = orig.encode() + b"\0"
        off = lib.find(needle, rodata)
        while off != -1 and lib[off - 1] != 0:
            off = lib.find(needle, off + 1)
        if off == -1:
            raise SystemExit(f"string not found: {orig}")
        lib[off:off + len(orig)] = new.encode().ljust(len(orig), b"\0")
        for site in sites:
            fmt, expected = _length_insn(arm64, len(orig))
            insn = struct.unpack_from(fmt, lib, site)[0]
            if insn != expected:
                raise SystemExit(f"unexpected instruction at {site:#x}: {insn:#x} (not the 3.7.0 libapp.so?)")
            struct.pack_into(fmt, lib, site, _length_insn(arm64, len(new))[1])
        print(f"{orig} -> {new}")
    for orig, suffix in CSTRINGS.items():
        new = base + suffix
        if len(new) > len(orig):
            raise SystemExit(f"URL too long: {new!r} (max {len(orig)} chars)")
        needle = orig.encode() + b"\0"
        off = lib.find(needle, rodata)
        while off != -1 and lib[off - 1] != 0:
            off = lib.find(needle, off + 1)
        if off == -1:
            raise SystemExit(f"string not found: {orig}")
        lib[off:off + len(orig)] = new.encode().ljust(len(orig), b"\0")
        print(f"{orig} -> {new}")
    return bytes(lib)


def arm64_libs(path: str) -> dict:
    """lib/arm64-v8a/*.so from config.arm64_v8a.apk, or from the split inside an APKMirror .apkm bundle."""
    import io
    z = zipfile.ZipFile(path)
    if ARM64_SPLIT in z.namelist():
        z = zipfile.ZipFile(io.BytesIO(z.read(ARM64_SPLIT)))
    libs = {n: z.read(n) for n in z.namelist() if n.startswith("lib/arm64-v8a/") and n.endswith(".so")}
    if LIB64 not in libs:
        raise SystemExit(f"{path}: no {LIB64} (expected config.arm64_v8a.apk or an .apkm bundle)")
    return libs


CLEARTEXT_ATTRS = {"usesCleartextTraffic", "cleartextTrafficPermitted"}


def allow_cleartext(axml: bytes) -> bytes:
    """Set every boolean usesCleartextTraffic / cleartextTrafficPermitted attribute of a binary XML to true.

    targetSdk is 31, so Android blocks plain-HTTP by default and the Java HTTP stack used by Cocos fails.
    """
    d = bytearray(axml)
    strings = []
    off = 8
    while off < len(d):
        ctype, _, csize = struct.unpack_from("<HHI", d, off)
        if ctype == 0x0001:  # string pool
            count, _, flags, sstart, _ = struct.unpack_from("<IIIII", d, off + 8)
            offsets = struct.unpack_from(f"<{count}I", d, off + 28)
            for o in offsets:
                p = off + sstart + o
                if flags & 0x100:  # utf-8
                    p += 2 if d[p] & 0x80 else 1
                    n = d[p]
                    p += 1
                    if n & 0x80:
                        n = ((n & 0x7F) << 8) | d[p]
                        p += 1
                    strings.append(bytes(d[p:p + n]).decode("utf-8"))
                else:
                    n = struct.unpack_from("<H", d, p)[0]
                    strings.append(bytes(d[p + 2:p + 2 + n * 2]).decode("utf-16le"))
        elif ctype == 0x0102:  # start tag
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", d, off + 24)
            for i in range(attr_count):
                ao = off + 16 + attr_start + i * attr_size
                name = struct.unpack_from("<I", d, ao + 4)[0]
                vtype = d[ao + 15]
                if strings[name] in CLEARTEXT_ATTRS and vtype == 0x12:
                    struct.pack_into("<I", d, ao + 16, 0xFFFFFFFF)
                    print(f"cleartext enabled: {strings[name]}")
        off += csize
    return bytes(d)


BUNDLED_MANIFESTS = ("assets/manifest/bundled.manifestenc", "assets/manifest/bundled_zip.manifestenc")


def unpack_bundled(manifest: bytes) -> bytes:
    """Empty the bundled manifests.

    They only list Play Asset Delivery files (AssetPack1: scene images, BGM, sound bank...). APKPure merged that
    pack into the base APK, but the client can't read those entries on a sideloaded install (logcat "asset is
    nullptr", in-game "Asset Download Error", client error 7, no HTTP request) -- even untagged. With no bundled
    entries the client fetches them from our CDN like every other file (the server serves them from the APK).
    """
    import json
    from saoub_crypto import decrypt, encrypt
    m = json.loads(decrypt(manifest))
    print(f"bundled manifest: {len(m['assets'])} entries removed")
    m["assets"] = {}
    return encrypt(json.dumps(m, indent=2).encode())


def main():
    args = sys.argv[1:]
    extra = {}
    if "--arm64" in args:
        i = args.index("--arm64")
        extra = arm64_libs(args[i + 1])
        del args[i:i + 2]
    src, dst, base = args[:3]
    base = base.rstrip("/")
    cdn = args[3].rstrip("/") if len(args) > 3 else ""
    zin = zipfile.ZipFile(src)
    with zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            if info.filename.startswith("META-INF/") and info.filename.upper().endswith((".SF", ".RSA", ".DSA", ".EC", ".MF")):
                continue
            data = zin.read(info.filename)
            if info.filename == LIB:
                data = patch_lib(data, base, cdn)
            elif info.filename in ("AndroidManifest.xml", "res/xml/network_security_config.xml"):
                data = allow_cleartext(data)
            elif info.filename in BUNDLED_MANIFESTS:
                data = unpack_bundled(data)
            out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            out.compress_type = info.compress_type
            out.external_attr = info.external_attr
            zout.writestr(out, data)
        for name, data in sorted(extra.items()):
            if name == LIB64:
                print("arm64-v8a:")
                data = patch_lib(data, base, cdn, arm64=True)
            # stored, like the game's own libraries (zipalign -p page-aligns them)
            zout.writestr(zipfile.ZipInfo(name, (2022, 10, 30, 0, 0, 0)), data, compress_type=zipfile.ZIP_STORED)
    print("written", dst)


if __name__ == "__main__":
    main()
