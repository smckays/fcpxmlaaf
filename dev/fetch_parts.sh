#!/bin/bash
# Download the third-party parts bundled into the Mac app (not stored in git):
# standalone Python for Apple Silicon + Intel, ffmpeg (from imageio-ffmpeg wheels), pywebview + PyObjC.
# Usage: dev/fetch_parts.sh PARTS_DIR
set -euo pipefail
P="$1"; mkdir -p "$P/ff" "$P/wv" "$P/pt"
for A in aarch64 x86_64; do
  curl -sSL -o "$P/py-$A.tar.gz" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7+20241016-$A-apple-darwin-install_only_stripped.tar.gz"
done
pip download imageio-ffmpeg==0.6.0 --no-deps --only-binary=:all: --platform macosx_11_0_arm64 -d "$P/ff" -q
pip download imageio-ffmpeg==0.5.1 --no-deps --only-binary=:all: --platform macosx_10_9_x86_64 -d "$P/ff" -q
PLAT="--only-binary=:all: --python-version 3.12 --implementation cp --platform macosx_11_0_universal2 --platform macosx_11_0_arm64 --platform macosx_10_13_universal2 --platform macosx_10_9_universal2 --platform macosx_10_9_x86_64"
pip install --target "$P/wv" $PLAT --no-deps -q "pywebview==5.3.2" "bottle==0.13.2" "typing_extensions==4.12.2" \
  "pyobjc-core==10.3.1" "pyobjc-framework-Cocoa==10.3.1" "pyobjc-framework-WebKit==10.3.1" "pyobjc-framework-Quartz==10.3.1" \
  "pyobjc-framework-Security==10.3.1" "pyobjc-framework-UniformTypeIdentifiers==10.3.1"
pip download --no-deps --no-binary=:all: "proxy_tools==0.1.0" -d "$P/pt" -q
tar xzf "$P/pt/proxy_tools-0.1.0.tar.gz" -C "$P/pt" && cp -r "$P/pt/proxy_tools-0.1.0/proxy_tools" "$P/wv/"
rm -rf "$P/wv/bin" "$P/wv/__pycache__"
cp "$(dirname "$0")/../app/macos/AppIcon.icns" "$P/AppIcon.icns"
echo "Parts ready in $P"
