"""Serves the original CDN content (manifests + hashed asset files) from the APK and the /data/data dump."""
import hashlib
import io
import json
import os
import re
import sys
import threading
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from saoub_crypto import decrypt, encrypt, hashed_name  # noqa: E402
from base_windows import set_windows_enc  # noqa: E402
from enemy_battle_areas import TARGETS, mapping_tag, move_battle_areas_enc  # noqa: E402

import config  # noqa: E402

HASHED_RE = re.compile(r"hashed/([0-9a-f]{2})/([0-9a-f]{40})$")
ZIP_RE = re.compile(r"/([A-Za-z0-9_]+\.zip)$")
# Categories the client downloads lazily (scene images, BGM, ...) through the per-group zip packages.
LAZY_ZIP_CATEGORIES = {0, 1}
# Served version differs from the original so clients that cached the old manifest download the fixed one.
SERVED_VERSION_PREFIX = "6"  # bump to make clients re-read the served manifest
# Lazily loaded categories served as initial-download content (5) instead. 7 = on-demand content (character
# atlases, voices...: fetched by the client when needed); 2 = scene images, BGM (Play Asset Delivery pack in
# the original, fetched like 0/1 through the group zips). SAOUB_RELABEL="2,7" puts everything back in the
# initial download.
RELABEL_CATEGORIES = {int(c) for c in os.environ.get("SAOUB_RELABEL", "").split(",") if c.strip()}
if 2 not in RELABEL_CATEGORIES:
    LAZY_ZIP_CATEGORIES = LAZY_ZIP_CATEGORIES | {2}


def window_states() -> dict:
    """{release label: "open" | "closed"}: config.EXTEND_WINDOWS open, then the admin panel's Banners tab choices
    (settings "release_windows"), which win."""
    import db  # noqa: PLC0415
    states = {label: "open" for label in config.EXTEND_WINDOWS}
    states.update({k: v for k, v in (db.get_setting("release_windows", {}) or {}).items() if v in ("open", "closed")})
    return states


CURRENT = None  # the AssetStore serving the game (main.py), for the admin panel's refresh_windows


def _ios_urls(manifest: dict) -> dict:
    out = dict(manifest)
    for key in ("remoteManifestUrl", "remoteVersionUrl"):
        if out.get(key):
            out[key] = (out[key].replace("android/production/project_android_zip", "ios/production/project_ios")
                        .replace("android/production/version_android_zip", "ios/production/version_ios"))
    return out


class AssetStore:
    def __init__(self):
        # The APK is optional: a developer's local server (dev data pack, no APK) does not serve the game files,
        # the developer APK downloads them from the VPS CDN.
        self._apk = zipfile.ZipFile(config.APK_PATH) if os.path.exists(config.APK_PATH) else None
        self._apk_lock = threading.Lock()
        self._apk_names = {n.split("/")[-1]: n for n in self._apk.namelist()
                           if n.startswith("assets/hashed/")} if self._apk else {}
        # offline XAPK data splits: hashed name -> (zip, entry)
        self._packs = []
        self._pack_names = {}
        for path in config.PACK_APKS:
            z = zipfile.ZipFile(path)
            self._packs.append((z, threading.Lock()))
            for n in z.namelist():
                if n.startswith("assets/saoub_pack/hashed/"):
                    self._pack_names[n.split("/")[-1]] = (len(self._packs) - 1, n)
        manifest_dir = config.MANIFEST_DIR
        self.project_manifest_raw = open(os.path.join(manifest_dir, "project_android_zip.manifestenc"), "rb").read()
        self.version_manifest_raw = open(os.path.join(manifest_dir, "version_android_zip.manifestenc"), "rb").read()
        self.project = json.loads(decrypt(self.project_manifest_raw))
        self.version = self.project["version"]
        self._zip_cache = {}
        self.aliases = {}  # hashed name -> hashed name actually served (missing translation -> English file)
        self.overrides = {}  # hashed name -> path of a file served instead of the original (patched base.bin)
        self._rewrite_manifest()
        global CURRENT
        CURRENT = self

    def _rewrite_manifest(self):
        """Make the manifest match the files we really have.

        - APK files that are older than the final manifest get their real md5/size (otherwise the client
          rejects them forever).
        - Non-English localized files the archived player never downloaded are aliased to the English file
          with the same logical path.
        The result is cached in DATA_DIR/server_cache.
        """
        cache_dir = os.path.join(config.DATA_DIR, "server_cache")
        os.makedirs(cache_dir, exist_ok=True)
        # files restored from the iOS IPA (tools/restore_from_ipa.py): their count keys the cache, so the missing
        # list is recomputed when they are added
        self.restored = sum(len(fs) for _, _, fs in os.walk(os.path.join(config.RESTORED_DIR, "hashed")))
        cache = os.path.join(cache_dir, f"manifest_{self.version}_v2" + (f"_r{self.restored}" if self.restored else "")
                             + ".json")
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                c = json.load(f)
            self.project, self.aliases, self.missing = c["project"], c["aliases"], c.get("missing", [])
        else:
            resolved = {}
            rp = os.path.join(config.DATA_DIR, "extracted/manifest/resolved_paths.json")
            if os.path.exists(rp):
                with open(rp, encoding="utf-8") as f:
                    resolved = json.load(f)
            by_path = {p: k for k, p in resolved.items()}
            stats = {"ok": 0, "fixed": 0, "aliased": 0, "aliased_by_size": 0, "missing": 0}
            # English files available, per category and size: fallback pairing for translations whose
            # logical path is unknown (localized movies/images often have identical sizes across languages)
            en_by_size = {}
            for key, info in self.project["assets"].items():
                if info.get("lang") == "en" and key.startswith("hashed/") and \
                        self.hashed(key.split("/")[-1]) is not None:
                    en_by_size.setdefault((info["category"], info["size"]), []).append(key)
            self.missing = []
            for key, info in self.project["assets"].items():
                if not key.startswith("hashed/"):
                    continue
                name = key.split("/")[-1]
                if os.path.exists(os.path.join(config.DUMP_CONTENTS, "hashed", name[:2], name)):
                    stats["ok"] += 1  # dump files were verified to match the final manifest
                    continue
                data = self.hashed(name)
                if data is None:
                    m = re.match(r"localization/(\w+)/(.*)$", resolved.get(key, ""))
                    en_key = by_path.get(f"localization/en/{m.group(2)}") if m else None
                    if en_key:
                        data = self.hashed(en_key.split("/")[-1])
                    if data is None and info.get("lang") and info["lang"] != "en":
                        same = en_by_size.get((info["category"], info["size"]), [])
                        if len(same) == 1:
                            en_key = same[0]
                            data = self.hashed(en_key.split("/")[-1])
                            stats["aliased_by_size"] += 1
                    if data is None:
                        stats["missing"] += 1
                        self.missing.append(key)
                        continue
                    self.aliases[name] = en_key.split("/")[-1]
                    stats["aliased"] += 1
                md5 = hashlib.md5(data).hexdigest()
                if md5 != info["md5"] or len(data) != info["size"]:
                    if name not in self.aliases:
                        stats["fixed"] += 1
                    info["md5"], info["size"] = md5, len(data)
                else:
                    stats["ok"] += 1
            print("manifest rewrite:", stats, flush=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"project": self.project, "aliases": self.aliases, "missing": self.missing}, f)
        gone = set(self.missing)
        by_order = self._alias_lazy_by_order(gone)
        zips = self._build_lazy_zips(gone)
        served = dict(self.project)
        # another prefix once files are restored: clients that cached the manifest read it again and fetch them
        served["version"] = ("7" if self.restored else SERVED_VERSION_PREFIX) + self.version
        if by_order:
            # its own character (kept by the iOS prefix, before the tags): cached manifests are read again
            served["version"] = served["version"][:1] + "l" + served["version"][2:]
        assets = {}
        for k, v in self.project["assets"].items():
            if k in gone:
                continue  # files nobody archived: one 404 aborts the whole download in the client
            if v.get("compressed"):
                # Bulk zips can't be rebuilt byte-identical; keep (with our md5/size) only those of the lazily
                # loaded categories -- the client fetches scene images/BGM through them, and waits forever if
                # the zip is absent. The big categories are fetched file by file instead.
                if k not in zips:
                    continue
                v = dict(v, md5=zips[k]["md5"], size=zips[k]["size"])
            # Our manifest is the archived player's *local* copy, where the client wrote "downloadState": 2 on
            # every file it had fetched. The CDN original had no such key. Served as is, it tells a new client
            # that ~46,000 files are already on disk: the on-demand loader (category 7) then never asks for
            # them, and a resumed download skips files it never got -> "asset is nullptr" crashes.
            v = {kk: vv for kk, vv in v.items() if kk != "downloadState"}
            if v.get("category") in RELABEL_CATEGORIES:
                # Re-label as core content (category 5) so they're part of the initial download.
                if v.get("compressed"):
                    continue
                v = dict(v, category=5)
            if "assetPack" in v:
                # Play Asset Delivery pack (categories 0/1/2: scene images, BGM, the sound bank...): fetched
                # through Google Play, which a sideloaded APK can't do -> serve them from our CDN instead
                v = {kk: vv for kk, vv in v.items() if kk != "assetPack"}
            assets[k] = v
        served["assets"] = assets
        # kept unpatched: the admin panel's Banners tab rebuilds the served manifests from it (refresh_windows)
        self._served_base = dict(served, assets=dict(assets))
        self._version_base = json.loads(decrypt(self.version_manifest_raw))
        self._finish_served(served)

    def _alias_lazy_by_order(self, gone: set) -> int:
        """Localized files of the on-demand category 7 that nobody archived (the newest ~50 per language: French,
        German, Spanish, Korean, Chinese players never fetched them) -> the English file at the same rank: in this
        category every language lists its files in the English order (457 of the 458 known aliases match it).
        Without an entry the client has no file to load for them. Removes them from `gone`; returns the count."""
        assets = self.project["assets"]
        ranked = {}
        for key, info in assets.items():
            if key.startswith("hashed/") and info.get("category") == 7 and info.get("lang"):
                ranked.setdefault(info["lang"], []).append(key)
        english = ranked.pop("en", [])
        count = 0
        for keys in ranked.values():
            for key, en_key in zip(keys, english):
                if key not in gone or en_key in gone:
                    continue
                en_name = en_key.split("/")[-1]
                data = self.hashed(self.aliases.get(en_name, en_name))
                if data is None:
                    continue
                self.aliases[key.split("/")[-1]] = self.aliases.get(en_name, en_name)
                assets[key] = dict(assets[key], md5=hashlib.md5(data).hexdigest(), size=len(data))
                gone.discard(key)
                count += 1
        if count:
            print(f"localized on-demand files served in English: {count}", flush=True)
        return count

    def refresh_windows(self):
        """Rebuild the served base.bin and manifests after the admin panel forced release windows open / closed:
        clients read the manifest again (its version changes with the window states) and download the new base.bin
        at their next start; no restart."""
        self.overrides.pop(hashed_name("master/divided/base.enc"), None)
        self._finish_served(dict(self._served_base, assets=dict(self._served_base["assets"])))

    def _finish_served(self, served: dict):
        self._extend_windows(served)
        self._move_battle_areas(served)
        self._refresh_override_zips(served)
        self.project_manifest_raw = encrypt(json.dumps(served, indent=2).encode())
        version_manifest = dict(self._version_base)
        version_manifest["version"] = served["version"]
        self.version_manifest_raw = encrypt(json.dumps(version_manifest, indent=2).encode())
        # iOS client (IPA 3.7.0, tools/patch_ipa.py): it reads {cdn}/asset/.../ios/production/version_ios.manifestenc
        # then project_ios.manifestenc (no "_zip"). Same files as Android (the bundled iOS ones are mostly
        # identical, the others older PNG versions): the Android manifest with the iOS URLs.
        # + the files only the iOS app bundles (tools/restore_from_ipa.py --ios-only, config.IOS_ONLY_DIR): without
        # them the light IPA crashed at every boot after its first download; their own version prefix makes
        # iPhones read the manifest again ("8": IPA files, "9": + the .pvrenc built by tools/pkm_to_pvr.py, "a": + the
        # battle backgrounds of tools/battle_bg_placeholders.py, "b": + the 3D area textures of
        # tools/area_textures_ios.py)
        ios_served, ios_version = _ios_urls(served), _ios_urls(version_manifest)
        index_path = os.path.join(config.IOS_ONLY_DIR, "index.json")
        if os.path.exists(index_path):
            with open(index_path, encoding="utf-8") as f:
                extra = json.load(f)
            ios_served["assets"] = dict(ios_served["assets"], **{k: v for k, v in extra.items()
                                                                 if k not in ios_served["assets"]})
            ios_served["version"] = ios_version["version"] = "b" + served["version"][1:]
        self.ios_project_manifest_raw = encrypt(json.dumps(ios_served, indent=2).encode())
        self.ios_version_manifest_raw = encrypt(json.dumps(ios_version, indent=2).encode())

    def _extend_windows(self, served: dict):
        """Serve base.bin with release windows forced open / closed (tools/base_windows.py set_windows): the
        config.EXTEND_WINDOWS ones open until the end of service (Season 9), then the admin panel's choices
        (window_states). Its manifest entry gets the patched md5/size, and the served version changes with the
        states so clients that cached the manifest read it again and download the new base.bin."""
        states = window_states()
        if not states:
            return
        name = hashed_name("master/divided/base.enc")
        key = f"hashed/{name[:2]}/{name}"
        raw = self.hashed(name)
        if raw is None or key not in served["assets"]:
            return
        tag = hashlib.md5(json.dumps(sorted(states.items())).encode()).hexdigest()[:6]
        path = os.path.join(config.DATA_DIR, "server_cache", "overrides", f"{name}_w{tag}")
        if not os.path.exists(path):
            data, done = set_windows_enc(raw, states)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
            print(f"base.bin release windows: {done}", flush=True)
        with open(path, "rb") as f:
            data = f.read()
        self.overrides[name] = path
        served["assets"][key] = dict(served["assets"][key], md5=hashlib.md5(data).hexdigest(), size=len(data))
        served["version"] = served["version"][:-len(tag)] + tag

    def _move_battle_areas(self, served: dict):
        """Serve enemy.bin and quest.bin with the parties and stages of the 11 background-less areas moved to a
        similar area (tools/enemy_battle_areas.py): the iOS client crashed when such a battle or story stage loaded
        (main quests M-1-2 / M-1-3 / M-1-4). Same override mechanism as base.bin; its own 6 characters of the served
        version (before base.bin's)."""
        tag = mapping_tag()
        patched = False
        for master_path in TARGETS:
            name = hashed_name(master_path)
            key = f"hashed/{name[:2]}/{name}"
            raw = self.hashed(name)
            if raw is None or key not in served["assets"]:
                continue
            path = os.path.join(config.DATA_DIR, "server_cache", "overrides", f"{name}_{tag}")
            if not os.path.exists(path):
                data, moved = move_battle_areas_enc(raw, master_path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
                print(f"{master_path} areas moved: {moved}", flush=True)
            with open(path, "rb") as f:
                data = f.read()
            self.overrides[name] = path
            served["assets"][key] = dict(served["assets"][key], md5=hashlib.md5(data).hexdigest(), size=len(data))
            patched = True
        if patched:
            served["version"] = served["version"][:-12] + tag + served["version"][-6:]

    def _refresh_override_zips(self, served: dict):
        """The group zips holding a patched file (base.bin, enemy.bin, quest.bin): their md5 / size in the zip index
        came from the original files, while the zip is built from the patched ones, so a client fetching the zip
        (Android install, categories 0/1/2) rejected it ("Unable to download file", error 7). Rebuilt here, kept in
        memory and served with their real md5 / size."""
        self._zip_overrides = {}
        if not self.overrides:
            return
        patched = set(self.overrides)
        for key, v in list(served["assets"].items()):
            if key.startswith("hashed/") or not v.get("compressed"):
                continue
            members = self._zip_members(key, self._gone)
            if not any(m.split("/")[-1] in patched for m in members):
                continue
            data = self._make_zip(key)
            self._zip_overrides[key] = data
            served["assets"][key] = dict(v, md5=hashlib.md5(data).hexdigest(), size=len(data))

    def _zip_members(self, zip_key: str, gone: set):
        info = self.project["assets"][zip_key]
        group = (info["category"], info["subCategory"])
        return [k for k, a in self.project["assets"].items()
                if k.startswith("hashed/") and k not in gone and (a["category"], a["subCategory"]) == group]

    def _make_zip(self, key: str) -> bytes:
        """Deterministic rebuild (fixed dates, manifest order): same bytes, hence same md5, every time."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for member in self._zip_members(key, self._gone):
                data = self.hashed(member.split("/")[-1])
                if data is not None:
                    info = zipfile.ZipInfo(member, date_time=(2023, 1, 1, 0, 0, 0))
                    info.create_system = 0  # the index md5s were computed on Windows; Linux would write 3
                    z.writestr(info, data)
        return buf.getvalue()

    def _build_lazy_zips(self, gone: set) -> dict:
        """Rebuild the group zips of the lazily-loaded categories once, cached on disk with their md5/size."""
        zdir = os.path.join(config.DATA_DIR, "server_cache", f"zips_{self.version}")
        os.makedirs(zdir, exist_ok=True)
        index_path = os.path.join(zdir, "index.json")
        index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {}
        todo = [k for k, v in self.project["assets"].items()
                if v.get("compressed") and v["category"] in LAZY_ZIP_CATEGORIES and k not in index]
        self._gone, self._zip_index = gone, index
        for i, key in enumerate(todo):
            data = self._make_zip(key)
            with open(os.path.join(zdir, key), "wb") as f:
                f.write(data)
            index[key] = {"md5": hashlib.md5(data).hexdigest(), "size": len(data)}
            if i % 50 == 0:
                print(f"zips: {i}/{len(todo)}", flush=True)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f)
        self._zip_dir = zdir
        return index

    def hashed(self, name: str) -> bytes | None:
        if name in getattr(self, "overrides", {}):
            with open(self.overrides[name], "rb") as f:
                return f.read()
        name = self.aliases.get(name, name) if hasattr(self, "aliases") else name
        for root in (config.DUMP_CONTENTS, config.RESTORED_DIR, config.IOS_ONLY_DIR):
            p = os.path.join(root, "hashed", name[:2], name)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read()
        entry = self._apk_names.get(name)
        if entry:
            with self._apk_lock:
                return self._apk.read(entry)
        packed = self._pack_names.get(name)
        if packed:
            z, lock = self._packs[packed[0]]
            with lock:
                return z.read(packed[1])
        return None

    def group_zip(self, zip_name: str) -> bytes | None:
        """Rebuild a 'hashed_XX_N.zip' bulk-download package from the individual files it contains.

        The originals were deleted by the client after extraction; we rebuild them from the manifest
        (same category/subCategory group) so the bulk download path still works.
        """
        if zip_name in getattr(self, "_zip_overrides", {}):
            return self._zip_overrides[zip_name]  # holds a patched file: see _refresh_override_zips
        cached = os.path.join(getattr(self, "_zip_dir", ""), zip_name)
        if os.path.exists(cached):
            with open(cached, "rb") as f:
                return f.read()
        if zip_name in getattr(self, "_zip_index", {}):
            # only index.json was deployed (no disk space for the zips): rebuild on demand, keep a few in memory
            if zip_name not in self._zip_cache:
                while len(self._zip_cache) >= 4:
                    self._zip_cache.pop(next(iter(self._zip_cache)))
                self._zip_cache[zip_name] = self._make_zip(zip_name)
            return self._zip_cache[zip_name]
        if zip_name in self._zip_cache:
            return self._zip_cache[zip_name]
        info = self.project["assets"].get(zip_name)
        if info is None:
            return None
        group = (info["category"], info["subCategory"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for key, a in self.project["assets"].items():
                if (a["category"], a["subCategory"]) != group or not key.startswith("hashed/"):
                    continue
                data = self.hashed(key.split("/")[-1])
                if data is not None:
                    z.writestr(key, data)
        data = buf.getvalue()
        self._zip_cache[zip_name] = data
        return data

    def hashed_file(self, name: str) -> str | None:
        """Path on disk of a hashed file, to stream it without loading it in memory. Files only found in the APK
        are extracted once to server_cache/apk_hashed (reading whole files into memory for ~20 parallel
        downloads made the process grow to its 1 GB limit, leaving no page cache: every request hit the disk)."""
        if name in self.overrides:
            return self.overrides[name]
        name = self.aliases.get(name, name)
        for root in (config.DUMP_CONTENTS, config.RESTORED_DIR, config.IOS_ONLY_DIR):
            p = os.path.join(root, "hashed", name[:2], name)
            if os.path.exists(p):
                return p
        entry = self._apk_names.get(name)
        if not entry or not config.APK_FILE_CACHE:
            return None  # served from memory by resolve() (APK without cache, XAPK data splits)
        cached = os.path.join(config.DATA_DIR, "server_cache", "apk_hashed", name[:2], name)
        if not os.path.exists(cached):
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            tmp = f"{cached}.{threading.get_ident()}.tmp"
            with self._apk_lock, self._apk.open(entry) as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            os.replace(tmp, cached)
        return cached

    def resolve_file(self, path: str) -> str | None:
        """Disk path for a hashed asset request (streamed with sendfile), None for everything else."""
        m = HASHED_RE.search(path)
        return self.hashed_file(m.group(2)) if m else None

    def resolve(self, path: str) -> tuple[bytes, str] | None:
        if path.endswith("version_android_zip.manifestenc"):
            return self.version_manifest_raw, "application/octet-stream"
        if path.endswith("project_android_zip.manifestenc"):
            return self.project_manifest_raw, "application/octet-stream"
        if path.endswith("version_ios.manifestenc"):
            return self.ios_version_manifest_raw, "application/octet-stream"
        if path.endswith("project_ios.manifestenc"):
            return self.ios_project_manifest_raw, "application/octet-stream"
        m = HASHED_RE.search(path)
        if m:
            data = self.hashed(m.group(2))
            return (data, "application/octet-stream") if data is not None else None
        m = ZIP_RE.search(path)
        if m:
            data = self.group_zip(m.group(1))
            return (data, "application/zip") if data is not None else None
        return None
