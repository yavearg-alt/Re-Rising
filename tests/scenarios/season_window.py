"""Season 9 kept open: base.bin served with its release windows extended (tools/base_windows.py)."""


def test_season9_window_served(h):
    """No Season Pass: Season 9 ended on 2022/10/28, before the server's game clock. The served base.bin must keep
    release.season_9 / release.season_9_reward open until 2023/01/16, with a manifest entry matching its bytes."""
    import hashlib
    import json
    import assets
    import config
    from flatmaster import Master
    from saoub_crypto import decrypt, hashed_name
    store = assets.AssetStore()
    name = hashed_name("master/divided/base.enc")
    entry = json.loads(decrypt(store.project_manifest_raw))["assets"][f"hashed/{name[:2]}/{name}"]
    data, _ = store.resolve(f"/cdn/v/contents/hashed/{name[:2]}/{name}")
    h.expect(entry["md5"] == hashlib.md5(data).hexdigest() and entry["size"] == len(data),
             "le manifest doit annoncer le base.bin modifié (md5/taille)")
    ends = {r.get(1): r.get(6) for r in Master(decrypt(data)).rows(15)}
    for label in config.EXTEND_WINDOWS:
        h.expect(ends.get(label) == "2023/01/16 05:59:59", f"{label} se termine le {ends.get(label)}")
    h.expect(ends.get("release.season_9_homeop") == "2022/09/30 05:59:59", "les autres fenêtres ne bougent pas")


def test_ios_manifests_served(h):
    """iOS client (tools/patch_ipa.py): version_ios / project_ios manifests, Android files with iOS URLs."""
    import json
    import assets
    from saoub_crypto import decrypt
    store = assets.AssetStore()
    v = json.loads(decrypt(store.resolve("/cdn/asset/production/ios/production/version_ios.manifestenc")[0]))
    h.expect("ios/production/project_ios.manifestenc" in v["remoteManifestUrl"], f"version_ios : {v['remoteManifestUrl']}")
    p = json.loads(decrypt(store.resolve(f"/cdn/{v['version']}/ios/production/project_ios.manifestenc")[0]))
    android = json.loads(decrypt(store.project_manifest_raw))
    # the Android files, plus the files only the iOS app bundles (config.IOS_ONLY_DIR) when they were extracted
    h.expect(all(p["assets"].get(k) == v for k, v in android["assets"].items()), "project_ios doit contenir les fichiers Android")
    h.expect(p["version"] == v["version"], f"versions iOS : {p['version']} / {v['version']}")


def test_ios_pvr_textures(h):
    """iOS crash after a scout / in the first battle: the iOS client loads x.pvrenc where Android loads x.pkmenc.
    A character atlas the IPA lacks must be in the iOS manifest (tools/pkm_to_pvr.py), served as a PVR v3 ETC2
    RGBA texture with the same blocks as the Android PKM."""
    import base64
    import hashlib
    import json
    import os
    import assets
    import config
    from saoub_crypto import decrypt, hashed_name
    if not os.path.exists(os.path.join(config.IOS_ONLY_DIR, "index.json")):
        return  # no iOS files on this machine
    store = assets.AssetStore()
    v = json.loads(decrypt(store.resolve("/cdn/asset/production/ios/production/version_ios.manifestenc")[0]))
    p = json.loads(decrypt(store.resolve(f"/cdn/{v['version']}/ios/production/project_ios.manifestenc")[0]))
    pvr_path = "files/spine/characterAtlas/chara_3/quinella_child.pvrenc"
    name = hashed_name(pvr_path)
    entry = p["assets"].get(f"hashed/{name[:2]}/{name}")
    h.expect(entry is not None, f"{pvr_path} absent du manifest iOS")
    if entry is None:
        return
    data, _ = store.resolve(f"/cdn/v/contents/hashed/{name[:2]}/{name}")
    h.expect(entry["md5"] == hashlib.md5(data).hexdigest() and entry["size"] == len(data), "md5/taille du .pvrenc")
    with open(os.path.join(config.DATA_DIR, "extracted", "master_json", "asset.0.json"), encoding="utf-8") as f:
        keys = {r["0"]: base64.b64decode(r["1"]) for r in json.load(f)}
    pvr = decrypt(data, keys[pvr_path])
    pkm_path = pvr_path.replace(".pvrenc", ".pkmenc")
    pkm = decrypt(store.hashed(hashed_name(pkm_path)), keys[pkm_path])
    h.expect(pvr[:4] == b"PVR\x03" and pvr[8] == 23, "en-tête PVR v3 ETC2 RGBA")
    h.expect(pvr[67:] == pkm[16:], "mêmes blocs ETC2 que le PKM Android")
