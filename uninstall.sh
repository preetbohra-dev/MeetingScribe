#!/bin/bash
# Stops MeetingScribe and removes the app + launch agent. Notes and models are kept.
LABEL="local.meetingscribe"
APP="$HOME/Applications/MeetingScribe.app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -f "meeting_scribe.py run" 2>/dev/null || true
pkill -f "$APP/Contents/MacOS/applet" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$APP"
tccutil reset All "$LABEL" 2>/dev/null || true
echo "MeetingScribe removed. Notes in ~/Documents/Meeting Notes were left untouched."
