#!/usr/bin/env python3
"""The Mac app's real window with real key events: typing, Backspace, Enter and pasting.
Browser engines alone can't show these, since macOS delivers keys to the window itself.
    python packaging/check_mac_window.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "video"), str(ROOT / "packaging")]
import capture_screens as c  # noqa: E402
import check_ui  # noqa: E402

KEY_RETURN, KEY_DELETE = 36, 51


def activate():
    subprocess.run(["osascript", "-e", f'tell application "System Events" to set frontmost of (first process whose unix id is {os.getpid()}) to true'], capture_output=True, timeout=30)


def script(line):
    r = subprocess.run(["osascript", "-e", f'tell application "System Events" to {line}'], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise SystemExit(f"macOS refused the test's key presses, so nothing was tested: {r.stderr.strip()}")


def drive(window, work, sid, out):
    failures = []

    def js(code):
        return window.evaluate_js(code)

    def wait(code, what, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            if js(code):
                return True
            time.sleep(0.3)
        failures.append(f"timed out waiting for {what}")
        return False

    def focus(selector):
        activate()
        js(f"document.querySelector({json.dumps(selector)}).click(); document.querySelector({json.dumps(selector)}).focus()")
        time.sleep(0.5)

    def value(selector):
        return js(f"document.querySelector({json.dumps(selector)}).value")

    def try_box(selector, name):
        focus(selector)
        script('keystroke "Ab c"')
        time.sleep(0.6)
        if value(selector) != "Ab c":
            failures.append(f"typing into {name} gave {value(selector)!r}")
        for _ in range(4):
            script(f"key code {KEY_DELETE}")
        time.sleep(0.5)
        if value(selector):
            failures.append(f"Backspace in {name} left {value(selector)!r}")
        subprocess.run(["pbcopy"], input=b"pasted", timeout=10)
        focus(selector)
        script('keystroke "v" using command down')
        time.sleep(0.6)
        if value(selector) != "pasted":
            failures.append(f"pasting into {name} gave {value(selector)!r}")
        js(f"document.querySelector({json.dumps(selector)}).value = ''")

    try:
        if not window.events.loaded.wait(200):  # a test machine's first WebKit start is slow
            raise RuntimeError("the page never loaded")
        if wait("!!document.getElementById('nav-meetings')", "the app to open"):
            js(f"go('meetings', {json.dumps(sid)})")
            name = "input[aria-label='Name for Speaker 3']"
            if wait(f"!!document.querySelector({json.dumps(name)})", "the speaker's name box"):
                try_box(name, "the speaker's name")
                try_box("input[placeholder='Ask about this meeting']", "Ask")
                focus(name)
                script('keystroke "Sam Lee"')
                script(f"key code {KEY_RETURN}")
                time.sleep(4)
                if "Sam Lee" not in (work / "sessions" / sid / "speakers.json").read_text(encoding="utf-8"):
                    failures.append("a typed name confirmed with Return was not saved")
            js("go('search')")
            if wait("!!document.querySelector('input[type=search]')", "the search box"):
                try_box("input[type=search]", "Search")
            js("go('connectors')")
            secret = "input[aria-label='Webhook link']"
            if wait(f"!!document.querySelector({json.dumps(secret)})", "the connector forms"):
                js("document.querySelectorAll('details').forEach(d => d.open = true)")
                time.sleep(0.4)
                try_box(secret, "a hidden password box")
    except BaseException as e:  # SystemExit from a refused key press must still close the window
        failures.append(str(e))
    out.extend(failures or ["ok"])
    window.destroy()


def main():
    with tempfile.TemporaryDirectory() as t:
        work = c.build_world(Path(t))
        sid = check_ui.add_unnamed_voice(work)
        os.environ["MEETINGSCRIBE_CONFIG"] = str(Path(t) / "config.toml")
        import webview

        import meeting_scribe as ms
        import meetingscribe_app as app
        ms.models_ready = lambda cfg: True
        app.Api.ensure_recorder = lambda self: True
        out = []
        window = webview.create_window("MeetingScribe", str(app.UI), js_api=app.Api(), width=1100, height=760)
        threading.Thread(target=lambda: (time.sleep(420), os._exit(3)), daemon=True).start()  # never hang the build
        webview.start(drive, (window, work, sid, out))
    if out != ["ok"]:
        sys.exit("The Mac window failed:\n- " + "\n- ".join(out or ["it closed without finishing"]))
    print("the Mac window takes typing, Backspace, Return and paste")


if __name__ == "__main__":
    main()
