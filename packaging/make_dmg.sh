#!/bin/bash
# Mac disk image, from PyInstaller's dist/MeetingScribe.app. Run on a Mac: packaging/make_dmg.sh 1.0.0
# The app is signed ad hoc: Apple silicon needs some signature to run it at all, and the macOS
# permissions (microphone, system audio) attach to it. It is not notarized.
set -euo pipefail
VERSION="${1:?usage: make_dmg.sh VERSION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/dist/MeetingScribe.app"
OUT="$ROOT/dist/MeetingScribe-$VERSION-Mac.dmg"
codesign --force --deep --sign - "$APP"
codesign --verify --deep "$APP"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$OUT"
hdiutil create -volname "MeetingScribe" -srcfolder "$STAGE" -format UDZO -ov "$OUT"
rm -rf "$STAGE"
echo "$OUT"
