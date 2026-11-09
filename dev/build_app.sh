#!/bin/bash
# Assemble "FCPXML to AAF.app" (universal: Apple Silicon + Intel) and zip it.
# Usage: dev/build_app.sh PARTS_DIR OUT_DIR SAMPLE_DIR
#   PARTS_DIR holds py-aarch64.tar.gz, py-x86_64.tar.gz, ff/*.whl (imageio-ffmpeg), wv/ (pywebview + PyObjC for macOS)
#   SAMPLE_DIR is the friendly sample project folder ("FCPXML2AAF Sample")
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PARTS="$1"; OUT="$2"; SAMPLE="$3"
VER=$(sed -n 's/__version__ = "\(.*\)"/\1/p' "$ROOT/engine/fcpxml2aaf/__init__.py")
APP="$OUT/FCPXML to AAF.app"
C="$APP/Contents"; R="$C/Resources"
rm -rf "$APP"; mkdir -p "$C/MacOS" "$R/ffmpeg" "$R/Sample Project" "$R/Licenses"

cp "$ROOT/app/macos/Info.plist" "$C/Info.plist"
sed -i "s#<string>0.3.0</string>#<string>$VER</string>#" "$C/Info.plist"
cp "$ROOT/app/macos/launcher.sh" "$C/MacOS/FCPXML to AAF"; chmod 755 "$C/MacOS/FCPXML to AAF"
cp "$PARTS/AppIcon.icns" "$R/AppIcon.icns"

# app code + engine + presets + sample
mkdir -p "$R/app/ui" "$R/engine"
cp "$ROOT/app/main.py" "$ROOT/app/server.py" "$R/app/"; cp "$ROOT/app/ui/index.html" "$R/app/ui/"
cp -r "$ROOT/engine/fcpxml2aaf" "$ROOT/engine/aaf2" "$R/engine/"
cp -r "$ROOT/presets" "$R/presets"
cp -r "$SAMPLE" "$R/Sample Project/"
cp -r "$PARTS/wv" "$R/lib"

# Python for each architecture, trimmed
for A in aarch64 x86_64; do
  D="$R/python-$([ $A = aarch64 ] && echo arm64 || echo x86_64)"
  mkdir -p "$D.tmp"; tar xzf "$PARTS/py-$A.tar.gz" -C "$D.tmp"; mv "$D.tmp/python" "$D"; rmdir "$D.tmp"
  L="$D/lib/python3.12"
  rm -rf "$D/include" "$D/share" "$L/test" "$L/idlelib" "$L/tkinter" "$L/turtledemo" "$L/ensurepip" \
         "$L/lib2to3" "$L/pydoc_data" "$L/site-packages/pip"* "$D/lib/tcl"* "$D/lib/tk"* "$D/lib/itcl"* \
         "$D/lib/thread"* "$L/lib-dynload/_tkinter"* "$D/bin/idle"* "$D/bin/pip"* "$D/bin/pydoc"* "$D/bin/2to3"*
  [ -f "$L/LICENSE.txt" ] && cp "$L/LICENSE.txt" "$R/Licenses/Python-LICENSE.txt"
done

# ffmpeg binaries from the imageio-ffmpeg wheels
T=$(mktemp -d)
for W in "$PARTS"/ff/*.whl; do unzip -q -o "$W" 'imageio_ffmpeg/binaries/ffmpeg-*' -d "$T"; done
cp "$T"/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-* "$R/ffmpeg/ffmpeg-arm64"
cp "$T"/imageio_ffmpeg/binaries/ffmpeg-osx64-* "$R/ffmpeg/ffmpeg-x86_64"
chmod 755 "$R/ffmpeg/"*; rm -rf "$T"

cp "$ROOT/engine/aaf2/LICENSE.txt" "$R/Licenses/pyaaf2-LICENSE.txt"
for p in pywebview pyobjc_core; do
  f=$(ls "$R/lib/$p"-*.dist-info/LICENSE* 2>/dev/null | head -1 || true); [ -n "$f" ] && cp "$f" "$R/Licenses/$p-LICENSE.txt"
done
cat > "$R/Licenses/README.txt" <<'EOT'
FCPXML to AAF bundles these open-source components:
- Python 3.12 (python-build-standalone): PSF License, see Python-LICENSE.txt
- pyaaf2: MIT License, see pyaaf2-LICENSE.txt
- pywebview: BSD License; PyObjC: MIT License
- FFmpeg (static builds distributed with imageio-ffmpeg): GPL. Source code: https://ffmpeg.org/download.html
EOT

find "$APP" -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/app/macos/READ ME FIRST.txt" "$OUT/READ ME FIRST.txt"
ZIP="$OUT/FCPXML-to-AAF-v$VER-mac.zip"
rm -f "$ZIP"; (cd "$OUT" && zip -qryX "$(basename "$ZIP")" "FCPXML to AAF.app" "READ ME FIRST.txt")
du -sh "$APP" "$ZIP"
