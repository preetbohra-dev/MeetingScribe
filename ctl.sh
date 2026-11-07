#!/bin/bash
# MeetingScribe control: ./ctl.sh start | stop | restart | status | logs | name | listen | voices | refilter | jira | process | download-models
LABEL="local.meetingscribe"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Applications/MeetingScribe.app"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/MeetingScribe"

case "${1:-status}" in
  start)
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl kickstart "gui/$(id -u)/$LABEL"
    echo "started" ;;
  stop)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    pkill -f "meeting_scribe.py run"; pkill -f "$APP/Contents/MacOS/applet"
    echo "stopped (a recording in progress is finalized and processed on next start)" ;;
  restart)
    # Killing the daemon ends the applet; launchd relaunches it within ~10 s.
    pkill -f "meeting_scribe.py run"
    echo "restarting…" ;;
  status)
    if pgrep -f "meeting_scribe.py run" >/dev/null; then echo "daemon: running (pid $(pgrep -f 'meeting_scribe.py run'))"; else echo "daemon: NOT running"; fi
    pgrep -f "meeting_scribe.py process" >/dev/null && echo "processing: a meeting is being transcribed/summarized"
    echo "--- last daemon log lines"; tail -n 8 "$LOG/daemon.log" 2>/dev/null
    echo "--- live detection (from this terminal)"; "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" status ;;
  name)   # ./ctl.sh name "Speaker 2" "Priya Shah" [session]
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" name-speaker "$2" "$3" --session "${4:-latest}" ;;
  listen) # ./ctl.sh listen ["Speaker 2"] [session]
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" listen ${2:+"$2"} --session "${3:-latest}" ;;
  voices) # ./ctl.sh voices [--forget "Name"]
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" voices "${@:2}" ;;
  refilter) # ./ctl.sh refilter [--session ID] [--force]
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" refilter "${@:2}" ;;
  terms)  # show and accept the terms of use
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" terms ;;
  jira)   # ./ctl.sh jira [--session ID] [--project KEY] [--refresh] [--dry-run] [--forget]
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" jira "${@:2}" ;;
  process|download-models)  # ./ctl.sh process <session folder> | ./ctl.sh download-models
    "$HERE/.venv/bin/python" "$HERE/meeting_scribe.py" "$@" ;;
  logs)
    tail -n 50 -F "$LOG/daemon.log" "$LOG/process.log" "$LOG/stderr.log" ;;
  *) echo "usage: $0 start|stop|restart|status|logs|name|listen|voices|refilter|jira|terms|process|download-models"; exit 1 ;;
esac
