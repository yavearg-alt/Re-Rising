"""Frame-rate unlock for the game APK (up to 120 fps): smali hooks + tools/fps_patch/Fps.java as an extra dex.

usage: .venv/Scripts/python tools/fps_patch.py in.apk out.apk [--fps N]
   out.apk is unsigned (tools/build_apk.sh aligns and signs it).
   --fps N  build default (assets/saoub_fps.txt), e.g. 120; without it the game keeps 60 fps until the SAOUB
            Launcher (SETTINGS > Frame rate) or the player writes Android/data/com.bandainamcoent.saoars/files/
            saoub_fps.txt (one line: 90, 120...).

How it works (see Fps.java): the game is cocos2d-x; Cocos2dxRenderer.onDrawFrame sleeps whenever
sAnimationInterval is above 1/60 s and otherwise renders at the display's refresh rate. The hooks make
setAnimationInterval never slower than the target and ask the window for a matching display mode, so a 90/120 Hz
screen (or BlueStacks with its own frame-rate setting raised) actually shows 90/120 fps.
Game logic follows cocos2d-x's delta time; anything tied to frames instead would run faster: test before relying
on it. Needs: JDK 21, Android build-tools (d8), apktool 2.9.3, android-31.jar (same as tools/build_apk.sh).
"""
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("SAOUB_DATA") or os.path.join(ROOT, "data")
T = os.environ.get("SAOUB_TOOLS") or DATA + "/tools"
DL = T + "/dl"
JDK = T + "/jdk-21.0.12.1+1/bin"
SDK = T + "/android-14"
WORK = DATA + "/build/fps_work"
JAVA = os.path.join(ROOT, "tools", "fps_patch", "Fps.java")
ASSET = "assets/saoub_fps.txt"
SIGNATURE = re.compile(r"META-INF/([^/]+\.(SF|RSA|DSA|EC)|MANIFEST\.MF)$")

RENDERER = "smali/org/cocos2dx/lib/Cocos2dxRenderer.smali"
RENDERER_ANCHOR = ".method public static setAnimationInterval(F)V\n    .locals 2\n"
RENDERER_HOOK = ("\n    invoke-static {p0}, Lcom/saoub/fps/Fps;->interval(F)F\n\n    move-result p0\n")
ACTIVITY = "smali/org/cocos2dx/lib/Cocos2dxActivity.smali"
ACTIVITY_ANCHOR = "    invoke-super {p0, p1}, Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V\n"
ACTIVITY_HOOK = "\n    invoke-static {p0}, Lcom/saoub/fps/Fps;->apply(Landroid/app/Activity;)V\n"


def run(*cmd, **kw):
    print("$", " ".join(str(c) for c in cmd)[:200], flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def _insert(path: str, anchor: str, hook: str, marker: str):
    s = open(path, encoding="utf-8").read()
    if marker in s:
        return  # already hooked
    if s.count(anchor) != 1:
        raise SystemExit(f"{os.path.basename(path)}: hook point not found as expected")
    open(path, "w", encoding="utf-8").write(s.replace(anchor, anchor + hook))


def patch_smali(dec: str):
    """Adds both hooks to an apktool-decoded APK."""
    _insert(os.path.join(dec, RENDERER), RENDERER_ANCHOR, RENDERER_HOOK, "Lcom/saoub/fps/Fps;->interval")
    _insert(os.path.join(dec, ACTIVITY), ACTIVITY_ANCHOR, ACTIVITY_HOOK, "Lcom/saoub/fps/Fps;->apply")


def _d8_env() -> dict:
    # d8.bat runs the first java on PATH (an old system JRE on some PCs): use the JDK
    return dict(os.environ, JAVA_HOME=os.path.dirname(JDK), PATH=JDK.replace("/", os.sep) + os.pathsep + os.environ["PATH"])


def fps_dex() -> bytes:
    """Fps.java alone -> classes2.dex."""
    build = WORK + "/java"
    shutil.rmtree(build, ignore_errors=True)
    os.makedirs(build + "/classes")
    run(JDK + "/javac.exe", "--release", "8", "-nowarn", "-cp", DL + "/android-31.jar", "-d", build + "/classes", JAVA)
    jar = build + "/fps.jar"
    with zipfile.ZipFile(jar, "w") as z:
        for d, _, fs in os.walk(build + "/classes"):
            for f in fs:
                z.write(os.path.join(d, f), os.path.relpath(os.path.join(d, f), build + "/classes"))
    os.makedirs(build + "/dex")
    run(SDK + "/d8.bat", "--release", "--min-api", "24", "--lib", DL + "/android-31.jar", "--output", build + "/dex",
        jar, env=_d8_env())
    return open(build + "/dex/classes.dex", "rb").read()


def hooked_classes_dex(apk: str) -> bytes:
    """The APK's classes.dex with both hooks."""
    dec = WORK + "/dec"
    shutil.rmtree(dec, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    mini = WORK + "/min.apk"
    with zipfile.ZipFile(apk) as src, zipfile.ZipFile(mini, "w") as z:
        for n in ("AndroidManifest.xml", "classes.dex", "resources.arsc"):
            z.writestr(n, src.read(n))
    apktool = [JDK + "/java.exe", "-jar", DL + "/apktool_2.9.3.jar"]
    run(*apktool, "d", "-r", "-f", mini, "-o", dec)
    patch_smali(dec)
    run(*apktool, "b", dec, "-o", WORK + "/min_built.apk")
    return zipfile.ZipFile(WORK + "/min_built.apk").read("classes.dex")


def main():
    args = sys.argv[1:]
    fps = None
    if "--fps" in args:
        i = args.index("--fps")
        fps = int(args[i + 1])
        del args[i:i + 2]
        if not 30 <= fps <= 240:
            sys.exit("--fps must be between 30 and 240")
    if len(args) != 2:
        sys.exit(__doc__)
    src, out = args
    with zipfile.ZipFile(src) as z:
        if "classes2.dex" in z.namelist():
            sys.exit(f"{src} already has a classes2.dex (already patched?)")
    extra = {"classes.dex": hooked_classes_dex(src), "classes2.dex": fps_dex()}
    if fps:
        extra[ASSET] = f"{fps}\n".encode()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            if info.filename in extra or SIGNATURE.match(info.filename):
                continue  # old signature: the caller signs again
            with zin.open(info) as r, zout.open(info, "w", force_zip64=info.file_size > 0x7FFFFFFF) as w:
                shutil.copyfileobj(r, w, 1 << 20)
        for name, data in extra.items():
            zout.writestr(zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0)), data, compress_type=zipfile.ZIP_DEFLATED)
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"built {out}" + (f" (default {fps} fps)" if fps else " (60 fps until saoub_fps.txt says otherwise)"))


if __name__ == "__main__":
    main()
