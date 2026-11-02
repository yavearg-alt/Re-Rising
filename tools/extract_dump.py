"""Decrypt the full manifest + final master data from the /data/data dump, and check asset coverage.

usage: extract_dump.py <apk> <dump_root> <out_dir>
"""
import hashlib
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from extract_apk import MASTER_TABLES  # noqa: E402
from saoub_crypto import decrypt, hashed_name  # noqa: E402

apk_path, dump_root, out_dir = sys.argv[1:4]
files = f"{dump_root}/data/data/com.bandainamcoent.saoars/files/contents"
apk = zipfile.ZipFile(apk_path)
apk_names = set(apk.namelist())

os.makedirs(f"{out_dir}/manifest", exist_ok=True)
for m in ("project_android_zip", "version_android_zip"):
    data = decrypt(open(f"{files}/manifest/{m}.manifestenc", "rb").read())
    open(f"{out_dir}/manifest/{m}.json", "wb").write(data)
project = json.loads(open(f"{out_dir}/manifest/project_android_zip.json", "rb").read())
print("manifest keys:", [k for k in project if k != "assets"], {k: project[k] for k in project if k not in ("assets",)})

assets = project["assets"]
have_dump = have_apk = missing = bad = 0
missing_list = []
for key, info in assets.items():
    p_dump = f"{files}/{key}"
    if os.path.exists(p_dump):
        data = open(p_dump, "rb").read(); have_dump += 1
    elif f"assets/{key}" in apk_names:
        data = apk.read(f"assets/{key}"); have_apk += 1
    else:
        missing += 1; missing_list.append((key, info)); continue
    if hashlib.md5(data).hexdigest() != info["md5"]:
        bad += 1
print(f"assets in manifest: {len(assets)} | dump: {have_dump} | apk: {have_apk} | missing: {missing} | md5 mismatch: {bad}")
json.dump(missing_list, open(f"{out_dir}/manifest/missing_assets.json", "w"), indent=1)

os.makedirs(f"{out_dir}/master_final", exist_ok=True)
for t in MASTER_TABLES:
    h = hashed_name(f"master/divided/{t}.enc")
    key = f"hashed/{h[:2]}/{h}"
    src = "dump" if os.path.exists(f"{files}/{key}") else "apk"
    raw = open(f"{files}/{key}", "rb").read() if src == "dump" else apk.read(f"assets/{key}")
    open(f"{out_dir}/master_final/{t}.bin", "wb").write(decrypt(raw))
    print(f"  {t:14} <- {src}")
