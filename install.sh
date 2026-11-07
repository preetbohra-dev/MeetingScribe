#!/bin/bash
# Installs MeetingScribe as a per-user background agent: ./install.sh, nothing else.
# No admin rights needed. Safe to run again; it reuses what is already there.
#
#  0. Python 3.11-3.14 (Homebrew's, or one fetched with uv), .venv with requirements.txt,
#     the terms of use, and the models (~10 GB, the only step that uses the network).
#  1. ~/Applications/MeetingScribe.app: a tiny AppleScript applet (built with the
#     stock `osacompile`) that launches the Python daemon. macOS privacy
#     permissions attach to this app, so Screen Recording / Microphone are granted
#     to "MeetingScribe" instead of to Terminal or a bare python binary.
#  2. ~/Library/LaunchAgents/local.meetingscribe.plist: starts the app at login
#     and restarts it if it ever exits.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
APP="$HOME/Applications/MeetingScribe.app"
LABEL="local.meetingscribe"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/MeetingScribe"

[[ "$(uname -m)" == "arm64" ]] || { echo "MeetingScribe needs an Apple silicon Mac (MLX)."; exit 1; }

find_python() {  # MLX has wheels for 3.11-3.14; stock /usr/bin/python3 is too old
  local v p
  for v in 3.13 3.12 3.14 3.11; do
    for p in "/opt/homebrew/bin/python$v" "$(command -v "python$v" 2>/dev/null)"; do
      [[ -n "$p" && -x "$p" ]] && { echo "$p"; return; }
    done
  done
}

if [[ ! -x "$PY" ]]; then
  echo "== Python"
  BASE="$(find_python)"
  if [[ -z "$BASE" ]] && command -v brew >/dev/null; then
    brew install python@3.12 && BASE=/opt/homebrew/bin/python3.12
  fi
  if [[ -z "$BASE" ]]; then  # no Homebrew either: uv fetches a standalone Python for this user
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
    "$UV" python install 3.12 && BASE="$("$UV" python find 3.12)"
  fi
  [[ -n "$BASE" ]] || { echo "Could not find or install Python 3.12. Install it from python.org and run ./install.sh again."; exit 1; }
  "$BASE" -m venv "$HERE/.venv"
fi

echo "== Packages"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$HERE/requirements.txt"

echo "== Terms of use"
"$PY" "$HERE/meeting_scribe.py" terms || { echo "Not installed: the terms of use were not accepted."; exit 1; }

echo "== Models (~10 GB, once)"
"$PY" "$HERE/meeting_scribe.py" download-models || { echo "Model download failed; run ./install.sh again to resume."; exit 1; }

echo "== Background agent"
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents" "$LOGDIR"

cat > "$HERE/run.sh" <<EOF
#!/bin/bash
# Launched by MeetingScribe.app. All output goes to a file so the applet never buffers it.
exec "$PY" "$HERE/meeting_scribe.py" run >>"$LOGDIR/stderr.log" 2>&1
EOF
chmod +x "$HERE/run.sh"

# Stop any running copy before replacing the app.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -f "meeting_scribe.py run" 2>/dev/null || true
pkill -f "$APP/Contents/MacOS/applet" 2>/dev/null || true

rm -rf "$APP"
osacompile -o "$APP" -e "try
	do shell script quoted form of \"$HERE/run.sh\"
end try"

INFO="$APP/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string "$LABEL" "$INFO"
plutil -replace CFBundleName -string "MeetingScribe" "$INFO"
plutil -replace LSUIElement -bool YES "$INFO"   # no Dock icon
plutil -replace NSMicrophoneUsageDescription -string \
  "MeetingScribe records your side of Teams meetings for on-device transcription." "$INFO"
plutil -replace NSAudioCaptureUsageDescription -string \
  "MeetingScribe records the Teams side of your meetings for on-device transcription." "$INFO"
plutil -replace NSAppleEventsUsageDescription -string \
  "MeetingScribe shows a notification when it starts and stops recording." "$INFO"
codesign --force --deep --sign - "$APP" 2>/dev/null   # ad-hoc signature, gives TCC a stable identity

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string><string>-W</string><string>-g</string><string>-j</string>
    <string>$APP</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGDIR/launchd.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd.log</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed and started."
echo "  App:   $APP"
echo "  Agent: $PLIST"
echo "  Logs:  $LOGDIR/daemon.log"
echo
echo "Next: grant MeetingScribe Screen Recording and Microphone access (see Readme, step 4)."
