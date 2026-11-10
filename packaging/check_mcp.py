#!/usr/bin/env python3
"""The packaged program, spoken to the way an AI assistant does: its standard input and output are
pipes and it has no console. Also checks that a calendar with repeating meetings can be read.
    python packaging/check_mcp.py dist/MeetingScribe/MeetingScribe.exe
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Sync\r\nDTSTART;TZID=America/New_York:{day}T090000\r\nDTEND;TZID=America/New_York:{day}T093000\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
NOTE = "# Migration sync\n\n## Summary\n- The move finished.\n\n## Action Items\n- [ ] **Priya**: update the reporting jobs\n\n---\n\n## Transcript\n"


def main():
    app = sys.argv[1]
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        work, notes = t / "w", t / "n"
        session = work / "sessions" / "2026-11-01_100000"
        session.mkdir(parents=True)
        notes.mkdir()
        (notes / "m.md").write_text(NOTE, encoding="utf-8")
        (session / "transcript.json").write_text(json.dumps([{"start": 1.0, "speaker": "Priya", "text": "The move finished."}]), encoding="utf-8")
        (session / "session.json").write_text(json.dumps({"title": "Migration sync", "status": "done", "started_at": "2026-11-01T10:00:00+00:00", "notes": str(notes / "m.md")}), encoding="utf-8")
        config = t / "c.toml"
        config.write_text(f"notify = false\nmcp_enabled = true\nwork_dir = {json.dumps(str(work))}\noutput_dir = {json.dumps(str(notes))}\nlog_dir = {json.dumps(str(t / 'logs'))}\n", encoding="utf-8")
        env = {**os.environ, "MEETINGSCRIBE_CONFIG": str(config)}
        calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "check", "version": "1"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "open_action_items", "arguments": {}}}]
        # stderr is left unconnected, as some assistants do: this is what a windowed app gets.
        r = subprocess.run([app, "mcp"], input="".join(json.dumps(c) + "\n" for c in calls).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=240)
        answers = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
        assert r.returncode == 0, f"exit {r.returncode}"
        assert [a["id"] for a in answers] == [1, 2, 3], answers
        assert answers[0]["result"]["protocolVersion"] == "2025-06-18" and answers[0]["result"]["serverInfo"]["version"] != "0", answers[0]
        assert len(answers[1]["result"]["tools"]) == 5, answers[1]
        items = json.loads(answers[2]["result"]["content"][0]["text"])
        assert items[0]["owner"] == "Priya" and "reporting jobs" in items[0]["task"], items
        brief = subprocess.run([app, "brief", "Migration", "--json"], capture_output=True, env=env, timeout=240)
        assert brief.returncode == 0 and json.loads(brief.stdout)["something"], brief
        ics = t / "c.ics"
        start = dt.datetime.now() + dt.timedelta(days=1)
        ics.write_text(ICS.format(day=start.strftime("%Y%m%d")), encoding="utf-8", newline="")
        cal = subprocess.run([app, "calendar-test", str(ics), "--days", "20"], capture_output=True, env=env, timeout=240)
        assert cal.returncode == 0, cal
        assert len(json.loads(cal.stdout)) >= 2, cal.stdout  # repeats expanded, named time zone understood
        off = config.read_text(encoding="utf-8").replace("mcp_enabled = true", "mcp_enabled = false")
        config.write_text(off, encoding="utf-8")
        refused = subprocess.run([app, "mcp"], input=b"", capture_output=True, env=env, timeout=240)
        assert refused.returncode != 0 and not refused.stdout, refused
    print("the packaged program answers an AI assistant, and refuses when access is off")


if __name__ == "__main__":
    main()
