#!/usr/bin/env bash
# Patch + align + sign + (optionally) install the private-server APK.
# usage: [CDN_URL=http://host:port] [FPS=120] [ARM64=bundle.apkm] tools/build_apk.sh [server_url] [--install]
# ARM64: config.arm64_v8a.apk or an APKMirror .apkm of 3.7.0 (4728): adds the 64-bit game (tools/patch_apk.py --arm64)
# FPS: default frame rate baked into the APK (tools/fps_patch.py); without it 60 fps until the launcher sets one
# CDN_URL: download the game files from another server than the API
# SAOUB_DATA (default <repo>/data), SAOUB_TOOLS (default $SAOUB_DATA/tools), GAME_APK (an installable 3.7.0 APK),
# SAOUB_ADB: see README.md
set -euo pipefail
cd "$(dirname "$0")/.."
URL="${1:-http://10.0.2.2:8080}"
DATA="${SAOUB_DATA:-$PWD/data}"
B="$DATA/build"
T="${SAOUB_TOOLS:-$DATA/tools}"
JDK="$T/jdk-21.0.12.1+1"
ADB="${SAOUB_ADB:-/c/Program Files/BlueStacks_nxt/HD-Adb.exe}"
APK="${GAME_APK:-SAO+Unleash+Blading_3.7.0_APKPure.apk}"
mkdir -p "$B"
./.venv/Scripts/python tools/patch_apk.py "$APK" "$B/unsigned.apk" "$URL" ${CDN_URL:+"$CDN_URL"} ${ARM64:+--arm64 "$ARM64"}
./.venv/Scripts/python tools/fps_patch.py "$B/unsigned.apk" "$B/unsigned_fps.apk" ${FPS:+--fps "$FPS"}
mv -f "$B/unsigned_fps.apk" "$B/unsigned.apk"
"$T/android-14/zipalign.exe" -p -f 4 "$B/unsigned.apk" "$B/aligned.apk"
[ -f "$B/debug.keystore" ] || "$JDK/bin/keytool.exe" -genkeypair -keystore "$B/debug.keystore" -storepass android \
  -keypass android -alias saoub -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Re:Rising local build"
"$JDK/bin/java.exe" -Xmx768m -jar "$T/android-14/lib/apksigner.jar" sign --ks "$B/debug.keystore" --ks-pass pass:android \
  --out "$B/saoub_private.apk" "$B/aligned.apk"
rm -f "$B/unsigned.apk" "$B/aligned.apk"
echo "built $B/saoub_private.apk"
if [ "${2:-}" = "--install" ]; then
  "$ADB" -s 127.0.0.1:5555 install -r "$B/saoub_private.apk"
fi
