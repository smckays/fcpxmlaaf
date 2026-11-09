#!/bin/bash
# FCPXML to AAF — app launcher. Picks the bundled Python for this Mac and starts the app.
APP="$(cd "$(dirname "$0")/../.." && pwd)"
R="$APP/Contents/Resources"
LOG="$HOME/Library/Logs/FCPXML to AAF.log"
mkdir -p "$HOME/Library/Logs"
exec >>"$LOG" 2>&1
echo "=== launch $(date) from $APP"

# macOS runs downloaded apps from a hidden read-only copy until they're moved. Ask to move it.
case "$APP" in
  */AppTranslocation/*)
    osascript -e 'display alert "Please move FCPXML to AAF into your Applications folder" message "macOS is running it from a temporary copy, so it can'"'"'t start properly. Drag the app into Applications, then open it from there." as warning'
    exit 0 ;;
esac

# The user already approved this app in macOS security settings; clear the download flag on the
# bundled Python and ffmpeg so macOS doesn't stop them one by one.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null

if [ "$(uname -m)" = "arm64" ] || [ "$(sysctl -in hw.optional.arm64 2>/dev/null)" = "1" ]; then
  PY="$R/python-arm64/bin/python3"; export FCPXML2AAF_FFMPEG="$R/ffmpeg/ffmpeg-arm64"
else
  PY="$R/python-x86_64/bin/python3"; export FCPXML2AAF_FFMPEG="$R/ffmpeg/ffmpeg-x86_64"
fi
[ -x "$FCPXML2AAF_FFMPEG" ] || unset FCPXML2AAF_FFMPEG

if [ ! -x "$PY" ]; then
  osascript -e 'display alert "FCPXML to AAF is damaged" message "Its built-in Python is missing. Please download the app again." as critical'
  exit 1
fi
exec "$PY" -I "$R/app/main.py"
