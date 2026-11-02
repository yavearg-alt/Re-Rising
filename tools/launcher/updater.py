"""Signed updates for the SAOUB Launcher: the owner publishes, players click "Update".

Published files (any static HTTPS host, e.g. a folder served by Caddy on the VPS or a public release):
  update.json      {"version": N, "notes": "...", "code": {...}, "data": {...}, "apk": {...}}
                   each part: {"url": "<relative to update.json, or absolute>", "sha256": "...", "size": n,
                   "version": n}; "data" and "apk" are optional
  update.json.sig  Ed25519 signature (RFC 8032) of the exact bytes of update.json
The launcher only trusts update.json when the signature matches the public key in update_config.json, then only
trusts each file whose sha256 matches. The private key stays with the owner, outside the repository.

  code  zip of the server + launcher (CODE_PATHS), extracted over the install folder. Never touches the player's
        database, logs, admin password or settings.
  data  the developer data pack zip (tools/build_dev_data.py: dev_data/...), extracted into the data folder.
  apk   a patched game APK, only downloaded (the player installs it from the Connect tab).

Owner side: tools/launcher/publish_update.py (key generation, building and signing a release).
"""
import glob
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CONFIG_FILE = os.path.join(HERE, "update_config.json")
VERSION_FILE = os.path.join(HERE, "version.json")
DATA_VERSION_FILE = "launcher_data_version.txt"  # in the data folder
USER_AGENT = "saoub-launcher"

# What a code update may write, relative to the install folder (anything else in the zip is refused).
CODE_PATHS = re.compile(r"^(server/[A-Za-z0-9_]+\.(py|json)|tools/[A-Za-z0-9_]+\.py|tools/launcher/[A-Za-z0-9_]+\.(py|pyw|json)"
                        r"|requirements\.txt|SAOUB_Launcher\.bat|start_local_server\.bat)$")
# never shipped nor overwritten: player data and secrets
NEVER = re.compile(r"(\.sqlite3|admin_password\.txt|saoub_dev_token\.txt|\.jsonl|\.log)$")
# maintainer tools: building and signing releases (not shipped to players)
OWNER_ONLY = {"tools/launcher/build_bundle.py", "tools/launcher/publish_update.py", "tools/launcher/make_tester_zip.py"}


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def config() -> dict:
    return load_json(CONFIG_FILE, {})


def installed_code_version() -> int:
    return int(load_json(VERSION_FILE, {}).get("version", 0))


def installed_data_version(data_dir: str) -> int:
    try:
        with open(os.path.join(data_dir, DATA_VERSION_FILE), encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def developer_checkout() -> bool:
    """A git checkout (the owner / developers): updates would overwrite work in progress, use git instead."""
    return os.path.exists(os.path.join(ROOT, ".git"))


def verify(message: bytes, signature: bytes, public_key_pem: str) -> bool:
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa
    try:
        eddsa.new(ECC.import_key(public_key_pem), "rfc8032").verify(message, signature)
        return True
    except (ValueError, TypeError):
        return False


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check() -> dict:
    """Download and verify update.json; returns it with absolute part URLs. Raises RuntimeError with a message."""
    cfg = config()
    url, key = cfg.get("url", ""), cfg.get("public_key", "")
    if not url or not key:
        raise RuntimeError("No update server configured (tools/launcher/update_config.json).")
    try:
        body = _get(url)
        sig = _get(url + ".sig")
    except OSError as e:
        raise RuntimeError(f"Could not reach the update server:\n{e}")
    if not verify(body, sig, key):
        raise RuntimeError("The update manifest is not signed by the server owner: refused.")
    manifest = json.loads(body)
    for part in ("code", "data", "apk"):
        if part in manifest:
            manifest[part]["url"] = urllib.parse.urljoin(url, manifest[part]["url"])
    return manifest


def download(part: dict, dest: str, progress=None) -> str:
    """Stream part["url"] to dest, checking size and sha256; progress(done_bytes, total_bytes)."""
    req = urllib.request.Request(part["url"], headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    done, total = 0, int(part.get("size") or 0)
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if h.hexdigest() != part["sha256"].lower():
        os.remove(tmp)
        raise RuntimeError(f"Corrupted download (checksum mismatch): {os.path.basename(dest)}")
    os.replace(tmp, dest)
    return dest


def apply_code(zip_path: str, root: str = ROOT) -> list:
    """Extract an allowed-paths-only code zip over root; returns the written paths."""
    written = []
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        bad = [n for n in names if not CODE_PATHS.match(n) or NEVER.search(n)]
        if bad:
            raise RuntimeError(f"Update refused, unexpected files: {', '.join(bad[:5])}")
        for n in names:
            out = os.path.join(root, *n.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            tmp = out + ".new"
            with open(tmp, "wb") as f:
                f.write(z.read(n))
            os.replace(tmp, out)
            written.append(n)
    return written


def apply_data(zip_path: str, data_dir: str, version: int) -> int:
    """Extract the data pack (entries under dev_data/) into data_dir; returns the number of files."""
    count = 0
    real_root = os.path.realpath(data_dir)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith("dev_data/"):
                continue
            rel = info.filename[len("dev_data/"):]
            out = os.path.realpath(os.path.join(data_dir, *rel.split("/")))
            if not out.startswith(real_root + os.sep):
                raise RuntimeError(f"Update refused, bad path: {info.filename}")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(info) as src, open(out + ".new", "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            os.replace(out + ".new", out)
            count += 1
    with open(os.path.join(data_dir, DATA_VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(str(version))
    return count


# ---------------------------------------------------------------- owner side (publish_update.py)

def server_tool_modules(root: str = ROOT) -> list:
    """tools/*.py modules imported by the server, directly or through each other (they live on its sys.path)."""
    def read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    mods = [os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(root, "tools", "*.py"))]
    found, todo = set(), [read(f) for f in glob.glob(os.path.join(root, "server", "*.py"))]
    while todo:
        src = todo.pop()
        for m in mods:
            if m not in found and re.search(rf"^\s*(import|from)\s+{m}\b", src, re.M):
                found.add(m)
                todo.append(read(os.path.join(root, "tools", m + ".py")))
    return sorted(found)


def code_files(root: str = ROOT) -> list:
    """Relative paths shipped in a code update."""
    files = [f"tools/{m}.py" for m in server_tool_modules(root)]
    for pattern in ("server/*.py", "server/table_*.json", "tools/launcher/*.py", "tools/launcher/*.pyw",
                    "tools/launcher/update_config.json"):
        files += [os.path.relpath(f, root).replace("\\", "/") for f in glob.glob(os.path.join(root, pattern))]
    files += [f for f in ("requirements.txt", "SAOUB_Launcher.bat", "start_local_server.bat")
              if os.path.exists(os.path.join(root, f))]
    files = sorted(set(files) - OWNER_ONLY)
    for f in files:
        assert CODE_PATHS.match(f) and not NEVER.search(f), f
    return files
