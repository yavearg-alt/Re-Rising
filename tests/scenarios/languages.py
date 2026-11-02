"""Other game languages (French, German, Spanish, Korean, Traditional Chinese): their translated texts are served, and
the on-demand files (category 7) nobody archived fall back to the English file of the same rank, so a player who picks
another language finds every file of the manifest."""
import contextlib
import io
import json

with contextlib.redirect_stdout(io.StringIO()):
    import assets
    from saoub_crypto import decrypt

LANGS = ("fr", "de", "es", "kr", "tc")


def test_languages_served(h):
    import os
    import config
    if not os.path.isdir(config.RESTORED_DIR):
        return  # the translations come from restored/ (an iOS IPA): the public dump alone has 17 per language
    with contextlib.redirect_stdout(io.StringIO()):
        store = assets.CURRENT or assets.AssetStore()
    served = json.loads(decrypt(store.project_manifest_raw))
    project = store.project["assets"]
    english = [k for k, i in project.items() if k.startswith("hashed/") and i.get("category") == 7 and i.get("lang") == "en"]
    for lang in LANGS:
        keys = [k for k, i in project.items() if k.startswith("hashed/") and i.get("lang") == lang]
        lazy = [k for k in keys if project[k].get("category") == 7]
        own = [k for k in keys if k in served["assets"] and k.split("/")[-1] not in store.aliases]
        h.expect(len(own) > 300, f"{lang}: {len(own)} translated files only")
        missing = [k for k in lazy if k not in served["assets"]]
        h.expect(not missing, f"{lang}: {len(missing)} on-demand files absent from the manifest")
        # the newest one (never archived): the English file of the same rank, with its md5 in the manifest
        key, en_key = lazy[-1], english[len(lazy) - 1]
        data = store.hashed(key.split("/")[-1])
        h.expect(data is not None and data == store.hashed(en_key.split("/")[-1]), f"{lang}: last on-demand file")
        info = served["assets"].get(key, {})
        h.expect(data is not None and info.get("size") == len(data), f"{lang}: manifest size {info.get('size')}")
    h.expect(served["version"][1] == "l", f"served version {served['version'][:3]}: clients must read it again")
