#!/usr/bin/env python3
"""Drives the real app page against the real backend, on made-up meetings, and saves what it shows
to video/shots/ for the demo. It fails if the page logs an error or a control doesn't work.

  video/.venv/Scripts/python video/capture_screens.py          (video/.venv/bin/python on a Mac)

The backend needs the app's own environment, so it runs from .venv as a small local server.
"""
import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHOTS = HERE / "shots"
PORT = 8931
BACKEND_START_S = 25  # the app's imports are slow, more so on a fresh test machine


def backend_python():
    """The app's own environment when there is one; on a build machine, this one."""
    venv = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(venv if venv.exists() else sys.executable)


def note(title, summary, sections, actions):
    body = [f"# {title}", "", "## Summary"] + [f"- {s}" for s in summary] + [""]
    for heading, items in sections:
        body += [f"## {heading}"] + [f"- {i}" for i in items] + [""]
    body += ["## Action Items"] + [f"- [ ] {a}" for a in actions] + ["", "---", "", "## Transcript", ""]
    return "\n".join(body)


LINES = [("Me", "Let's start with the migration."), ("Priya Shah", "The move finished last night, and the reporting jobs still point at the old table."),
         ("Me", "Can you update those by Thursday?"), ("Priya Shah", "Yes, I'll do that."), ("Dan Okafor", "I'll raise the firewall change with security.")]
MEETINGS = [
    (9, "Weekly migration sync", "general", ["The order table moved to the new cluster.", "Reporting jobs still read the old table.", "Cutover of the archive is planned for next week."], [],
     ["**Priya Shah**: update the reporting jobs", "**Dan Okafor**: raise the firewall change with security"]),
    (6, "Vendor review with Acme", "customer", ["Acme asked for single sign-on before renewing."], [("Customer needs", ["Single sign-on by January"]), ("Objections", ["Price of the larger plan"]), ("Next step", ["Send a written proposal"])],
     ["**Me**: send Acme the written proposal"]),
    (2, "Weekly migration sync", "general", ["Order table cutover went cleanly.", "Reporting jobs are the last thing still on the old table.", "Archive cutover needs the firewall change first."], [],
     ["**Priya Shah**: update the reporting jobs (Thursday)", "**Dan Okafor**: raise the firewall change with security", "**Me**: book the archive cutover window"]),
    (0, "Daily stand-up", "standup", ["Priya: reporting jobs.", "Dan: firewall request for the archive cutover.", "Me: cutover runbook."], [("Blockers", ["Dan is waiting on security to approve the firewall change"])],
     ["**Me**: finish the cutover runbook", "**Priya Shah**: share the reporting test results"]),
]


def build_world(root):
    now = dt.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    work, notes = root / "w", root / "n"
    (work / "sessions").mkdir(parents=True)
    notes.mkdir()
    (root / "config.toml").write_text(f'my_name = "Me"\nnotify = false\nmcp_enabled = true\nretain_audio_days = 30\nnever_record = ["HR", "doctor"]\nwork_dir = {json.dumps(str(work))}\noutput_dir = {json.dumps(str(notes))}\nlog_dir = {json.dumps(str(root / "l"))}\n', encoding="utf-8")
    (work / "terms_accepted.json").write_text(json.dumps({"version": 2}))
    for ago, title, template, summary, sections, actions in MEETINGS:
        when = now - dt.timedelta(days=ago)
        sid = when.strftime("%Y-%m-%d_%H%M%S")
        d = work / "sessions" / sid
        d.mkdir()
        path = notes / f"{sid}.md"
        path.write_text(note(title, summary, sections, actions), encoding="utf-8")
        (d / "transcript.json").write_text(json.dumps([{"start": 4.0 + 9 * i, "speaker": s, "text": t} for i, (s, t) in enumerate(LINES)]))
        (d / "speakers.json").write_text(json.dumps({"Priya Shah": {"known": True, "confirmed": True, "seconds": 90}, "Dan Okafor": {"known": True, "confirmed": True, "seconds": 40}}))
        (d / "session.json").write_text(json.dumps({"title": title, "status": "done", "started_at": when.isoformat(timespec="seconds"), "duration_s": 1500, "source": "Microsoft Teams", "notes": str(path), "template": template}))
    (work / "connectors.json").write_text(json.dumps([{"id": "slack-1", "service": "slack", "name": "#migration-team"}, {"id": "gmail", "service": "gmail", "name": "Gmail", "account": "work"}]))
    soon = [("Weekly migration sync", 1, ["Priya Shah", "Dan Okafor"]), ("Vendor review with Acme", 2, ["Sam Lee"]), ("New starter welcome", 3, [])]
    events = []
    for title, days, people in soon:
        start = now + dt.timedelta(days=days)
        events.append({"id": f"{title}|{days}", "title": title, "start": start.isoformat(timespec="seconds"), "end": (start + dt.timedelta(minutes=30)).isoformat(timespec="seconds"), "people": people})
    (work / "calendar.json").write_text(json.dumps({"connected": True, "fetched": now.isoformat(timespec="seconds"), "events": events, "error": ""}))
    return work


def serve(root, port):
    sys.path.insert(0, str(ROOT))
    os.environ["MEETINGSCRIBE_CONFIG"] = str(Path(root) / "config.toml")
    import meeting_scribe as ms
    import meetingscribe_app as app
    ms.models_ready = lambda cfg: True
    app.Api.ensure_recorder = lambda self: True
    api, work = app.Api(), Path(root) / "w"

    def alive():
        while True:
            (work / "status.json").write_text(json.dumps({"time": time.time(), "state": (work / "state").read_text() if (work / "state").exists() else "watching", "service": "Teams", "title": "Daily stand-up", "warnings": []}))
            time.sleep(1)
    threading.Thread(target=alive, daemon=True).start()

    class Bridge(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = (ROOT / "ui" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            name = self.path.strip("/")
            args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                body = json.dumps({"value": getattr(api, name)(*args)})
            except Exception as e:
                body = json.dumps({"error": f"{type(e).__name__}: {e}"})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Bridge).serve_forever()


SHIM = """window.pywebview = {api: new Proxy({}, {get: (_, name) => async (...args) => {
  const r = await (await fetch('/' + name, {method: 'POST', body: JSON.stringify(args)})).json();
  if (r.error) throw new Error(r.error);
  return r.value;
}})};
window.addEventListener('load', () => setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 50));
Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => { window.copied = text; }}});"""


def capture(work, url, engine="chromium", out=SHOTS):
    from playwright.sync_api import sync_playwright
    out.mkdir(exist_ok=True)
    problems = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1440, "height": 810}, device_scale_factor=1.5, color_scheme="light")
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.add_init_script(SHIM)
        page.goto(url)
        nav = lambda name: (page.click(f"#nav-{name}"), page.wait_for_timeout(500))  # noqa: E731

        def shot(name):
            page.evaluate("document.getElementById('toast').style.display = 'none'; document.activeElement.blur()")
            page.screenshot(path=str(out / f"{name}.png"))
        page.wait_for_selector("#nav-meetings")

        (work / "state").write_text("recording")
        page.wait_for_timeout(4200)
        page.wait_for_selector("text=Copy a notice for the chat")
        shot("meetings")
        page.click("text=Copy a notice for the chat")
        assert "recording" in page.evaluate("window.copied"), "notice not copied"
        (work / "state").write_text("watching")
        page.wait_for_selector("text=Watching for meetings", timeout=8000)

        page.click("text=Daily stand-up")
        page.wait_for_selector("text=Blockers")
        shot("note")
        assert page.locator("text=What did we decide?").count() == 1, "saved question missing"
        assert page.locator("text=Rewrite the note").count() == 1, "no way to change the template"

        nav("actions")
        page.wait_for_selector("text=Carried over")
        shot("actions")
        page.locator("input[type=checkbox]").first.check()
        page.wait_for_timeout(700)
        assert page.locator("text=Finished (1)").count() == 1, "finished item not listed"
        ticked = sum("- [x]" in p.read_text(encoding="utf-8") for p in (work.parent / "n").glob("*.md"))
        assert ticked >= 1, "the note was not ticked"
        page.locator("summary", has_text="Finished").click()
        page.locator("text=Reopen").first.click()
        page.wait_for_timeout(700)
        assert page.locator("text=Finished (1)").count() == 0, "reopen did not work"

        nav("upcoming")
        page.locator("button.meeting", has_text="Weekly migration sync").click()
        page.wait_for_selector("text=Still open")
        shot("upcoming")
        page.fill("input[aria-label='Prepare for']", "Acme")
        page.click("button:has-text('Prepare')")
        page.wait_for_selector("text=send Acme the written proposal")
        page.fill("input[aria-label='Prepare for']", "Nothing like this at all")
        page.click("button:has-text('Prepare')")
        page.wait_for_selector("text=looks like a first meeting")

        nav("connectors")
        page.wait_for_selector("text=claude mcp add")
        shot("connectors")
        page.locator("text=AI assistants (MCP)").scroll_into_view_if_needed()
        page.evaluate("document.getElementById('main').scrollTop = 180")
        page.wait_for_timeout(200)
        shot("assistants")

        nav("settings")
        page.locator("text=Consent and keeping meetings").scroll_into_view_if_needed()
        page.evaluate("[...document.querySelectorAll('h2')].find(e => e.textContent.startsWith('Consent')).scrollIntoView()")
        page.wait_for_timeout(300)
        page.fill("input[aria-label='Days to keep meetings']", "1")
        page.wait_for_selector("text=permanently deletes")
        shot("consent")
        page.fill("input[aria-label='Days to keep meetings']", "0")
        page.wait_for_timeout(300)
        assert page.locator("text=permanently deletes").count() == 0 or "audio" in page.locator(".problem").first.inner_text(), "warning stayed"
        page.fill("textarea[aria-label='Never record']", "HR\nlegal")
        page.click("button.primary:has-text('Save')")
        page.wait_for_selector("text=Saved.")
        page.evaluate("document.getElementById('templates').previousElementSibling.scrollIntoView()")
        page.wait_for_timeout(300)
        shot("templates")
        page.locator("summary", has_text="Add a template").click()
        page.fill("input[aria-label='Template name']", "Board")
        page.fill("input[aria-label='Words in the title']", "board, investor")
        page.fill("textarea[aria-label='Sections']", "Summary: not allowed")
        page.click("#templates button:has-text('Add'):not(:has-text('template'))")
        page.wait_for_selector("text=already part of every note")
        page.fill("textarea[aria-label='Sections']", "Decisions: what the board approved")
        page.click("#templates button:has-text('Add'):not(:has-text('template'))")
        page.wait_for_selector("text=Adds: Decisions")
        page.locator("#templates .panel", has_text="Board").locator("text=Remove").click()
        page.wait_for_timeout(500)
        assert page.locator("text=Adds: Decisions").count() == 0, "template not removed"

        nav("meetings")
        page.click("text=Daily stand-up")
        page.wait_for_selector("text=Send recap and follow-ups")
        page.click("text=Send recap and follow-ups")
        page.wait_for_selector("text=Follow-ups")
        shot("send")
        browser.close()
    if problems:
        sys.exit("the page reported problems:\n" + "\n".join(problems))
    print("every control worked; screens are in", out)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--serve":
        return serve(sys.argv[2], PORT)
    with tempfile.TemporaryDirectory() as t:
        work = build_world(Path(t))
        server = subprocess.Popen([backend_python(), str(Path(__file__).resolve()), "--serve", t])
        try:
            time.sleep(BACKEND_START_S)
            capture(work, f"http://127.0.0.1:{PORT}/")
        finally:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
