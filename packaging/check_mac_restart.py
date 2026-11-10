#!/usr/bin/env python3
"""The packaged recorder under the real launchd, with the app's own login item: the app must
open while the recorder runs, and the recorder must stay stopped after a force quit or a plain
quit, and come back after a crash.
    python packaging/check_mac_restart.py dist/MeetingScribe.app/Contents/MacOS/MeetingScribe
"""

import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402

ITEM = app.login_item()
LABEL = ITEM["Label"]
DOMAIN = f"gui/{os.getuid()}"
SETTLE_S = ITEM["ThrottleInterval"] + 15  # launchd would have restarted it by now


def running():
    out = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True).stdout
    found = re.search(r'"PID" = (\d+);', out)
    return int(found[1]) if found else None


def wait_for_start(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if running():
            time.sleep(5)  # past the program's own start-up
            return running()
        time.sleep(1)
    return None


def start():
    subprocess.run(["launchctl", "kickstart", f"{DOMAIN}/{LABEL}"], check=True)
    pid = wait_for_start(60)
    if not pid:
        sys.exit("launchd did not start the recorder")
    return pid


def stays_stopped(how, sig, exe):
    os.kill(start(), sig)
    time.sleep(SETTLE_S)
    if running():
        sys.exit(f"the recorder came back after a {how}")
    if subprocess.run(["pgrep", "-f", f"{exe} run"], capture_output=True).returncode == 0:
        sys.exit(f"part of the recorder kept running after a {how}")
    print(f"it stayed stopped after a {how}", flush=True)


def windows(exe):
    """Processes of the app itself: the recorder and other jobs have arguments after the path."""
    out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
    return [int(line.split()[0]) for line in out.splitlines() if line.strip().endswith(exe) and "ps -axo" not in line]


def open_app(exe):
    opened = subprocess.run(["open", str(Path(exe).parents[2])], capture_output=True, text=True)
    if opened.returncode:
        sys.exit(f"the app would not open while the recorder ran: {opened.stderr.strip()}")


def wait_for_window(exe, seconds):
    end = time.time() + seconds
    while time.time() < end and not windows(exe):
        time.sleep(1)
    return windows(exe)


def opens_while_recorder_runs(exe):
    """Quitting the window leaves the recorder running; opening the app must still give one window."""
    time.sleep(8)
    if windows(exe):
        sys.exit("the recorder opened a window by itself when it started")
    for attempt in ("first", "again after quitting the window"):
        open_app(exe)
        found = wait_for_window(exe, 45)
        if not found:
            sys.exit(f"no window appeared when the app was opened ({attempt})")
        open_app(exe)  # already open: it must come forward, not double up
        time.sleep(8)
        if len(windows(exe)) != 1:
            sys.exit(f"opening the app twice gave {len(windows(exe))} windows")
        os.kill(found[0], signal.SIGTERM)
        time.sleep(4)
    print("the app opens while the recorder runs, in one window", flush=True)


def main():
    exe = sys.argv[1] if os.path.isabs(sys.argv[1]) else os.path.abspath(sys.argv[1])
    with tempfile.TemporaryDirectory() as t:
        plist = Path(t) / f"{LABEL}.plist"
        work = Path(t) / "work"
        work.mkdir()
        (work / "terms_accepted.json").write_text(json.dumps({"version": ms.DISCLAIMER_VERSION}), encoding="utf-8")
        config = Path(t) / "config.toml"
        config.write_text(f"notify = false\nwork_dir = {json.dumps(str(work))}\n", encoding="utf-8")
        plist.write_bytes(plistlib.dumps({**ITEM, "ProgramArguments": [exe, "run"], "EnvironmentVariables": {"MEETINGSCRIBE_CONFIG": str(config)}}))
        subprocess.run(["launchctl", "bootstrap", DOMAIN, str(plist)], check=True)
        try:
            if not wait_for_start(60):
                sys.exit("the recorder did not start at load")
            opens_while_recorder_runs(exe)
            os.kill(running(), signal.SIGKILL)
            time.sleep(SETTLE_S)
            stays_stopped("force quit", signal.SIGKILL, exe)
            stays_stopped("plain quit", signal.SIGTERM, exe)
            crashed = start()
            time.sleep(ITEM["ThrottleInterval"] + 2)
            os.kill(crashed, signal.SIGSEGV)
            back = wait_for_start(SETTLE_S + 30)
            if not back or back == crashed:
                sys.exit("the recorder did not come back after a crash")
            print("it came back after a crash", flush=True)
        finally:
            subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], capture_output=True)


if __name__ == "__main__":
    main()
