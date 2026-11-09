#!/bin/bash
# FCPXML -> AAF converter (test build). Double-click to run.
cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

say_dialog() {  # message, [buttons...]
  local msg="$1"; shift
  local btns='"OK"'
  if [ $# -gt 0 ]; then btns=$(printf '"%s",' "$@"); btns="${btns%,}"; fi
  osascript -e "button returned of (display dialog \"$msg\" buttons {$btns} default button 1 with title \"FCPXML to AAF\")" 2>/dev/null
}

echo "=== FCPXML -> AAF converter (test build) ==="

# 1. Python (comes with Apple's Command Line Tools)
if ! xcode-select -p >/dev/null 2>&1 || ! /usr/bin/python3 -c "import sys" >/dev/null 2>&1; then
  say_dialog "This tool needs Apple's free Command Line Tools (for Python). An installer will open now. When it finishes, double-click this tool again." "OK"
  xcode-select --install 2>/dev/null
  exit 1
fi
PY=/usr/bin/python3
command -v python3 >/dev/null && PY="$(command -v python3)"

# 2. ffmpeg (optional, needed for camera files like .mov/.mp4)
if ! command -v ffmpeg >/dev/null; then
  ans=$(say_dialog "ffmpeg is not installed.\n\nWAV/AIFF audio will still convert, but audio inside video files (.mov/.mp4) needs ffmpeg.\n\nTo install it: open Terminal and run  brew install ffmpeg  (needs Homebrew from brew.sh)." "Continue anyway" "Quit")
  [ "$ans" = "Quit" ] && exit 0
fi

# 3. Pick the FCPXML
IN=$(osascript -e 'POSIX path of (choose file with prompt "Choose the .fcpxml or .fcpxmld exported from Final Cut Pro (File > Export XML)")' 2>/dev/null)
if [ -z "$IN" ]; then
  IN=$(osascript -e 'POSIX path of (choose folder with prompt "No file chosen. If your export is a .fcpxmld folder, choose it here:")' 2>/dev/null)
fi
[ -z "$IN" ] && { echo "Cancelled."; exit 0; }
IN="${IN%/}"

# 4. Optional preset from the mapping page
ARGS=()
ans=$(say_dialog "Use a track-mapping preset (.json from the mapping page)?\n\nWithout one: Dialogue -> DX, Music -> MX, Effects -> FX." "No preset" "Choose preset...")
if [ "$ans" = "Choose preset..." ]; then
  P=$(osascript -e 'POSIX path of (choose file with prompt "Choose the preset .json")' 2>/dev/null)
  [ -n "$P" ] && ARGS+=(--preset "$P")
fi

# 5. Output next to the input
OUT="$(dirname "$IN")/AAF Export"
echo "Input:  $IN"
echo "Output: $OUT"
echo
PYTHONPATH="$HERE/engine" "$PY" -m fcpxml2aaf "$IN" --out "$OUT" "${ARGS[@]}"
STATUS=$?

if [ $STATUS -eq 0 ]; then
  open "$OUT"
  REPORT=$(ls -t "$OUT"/*"AAF report.txt" 2>/dev/null | head -1)
  [ -n "$REPORT" ] && open -e "$REPORT"
  say_dialog "Done. The AAF and a report are in the 'AAF Export' folder next to your FCPXML." "OK" >/dev/null
else
  say_dialog "Something went wrong. Please screenshot this Terminal window and send it back." "OK" >/dev/null
fi
echo
echo "You can close this window."
