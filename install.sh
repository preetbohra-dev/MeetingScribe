#!/bin/bash
# Installs MeetingScribe as a per-user background agent. No admin rights needed.
#
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

[[ -x "$PY" ]] || { echo "No virtualenv at $PY. Run the setup steps in the Readme first."; exit 1; }
"$PY" "$HERE/meeting_scribe.py" terms || { echo "Not installed: the terms of use were not accepted."; exit 1; }
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
