#!/usr/bin/env python3
"""The release gate for the app's window: the real page on the real backend, in a real browser
engine, with real keystrokes. Every page is opened, and every box on it is typed into.
    python packaging/check_ui.py chromium     (the engine of Windows' WebView2)
    python packaging/check_ui.py webkit       (the engine of the Mac app's window)
Needs playwright and that browser. A release is not published unless this passes.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "video"))
import capture_screens as c  # noqa: E402

BOXES = "input:visible:not([type=checkbox]):not([type=radio]):not([type=date]):not([disabled]), textarea:visible:not([disabled])"
# So that a page losing its boxes fails here, rather than the check quietly covering less.
MINIMUM = {"meeting": 2, "upcoming": 1, "search": 1, "connectors": 5, "tasks": 2, "settings": 12, "send to a channel": 3, "send as email": 5,
           "hand to AI": 1, "company AI setup": 2, "Jira sign-in": 3, "task questions": 1, "ticket fields": 3}


def type_into_every_box(page, where, failed):
    """Real key presses, each one able to be refused by the page, which fill() would hide."""
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.wait_for_timeout(150)
    boxes = page.locator(BOXES)
    count = boxes.count()
    if count < MINIMUM.get(where, 0):
        failed.append(f"{where}: only {count} boxes, expected at least {MINIMUM[where]}")
    for i in range(count):
        box = boxes.nth(i)
        name = box.get_attribute("aria-label") or box.get_attribute("placeholder") or f"box {i + 1}"
        box.click()
        before = box.input_value()
        page.keyboard.press("End" if box.get_attribute("type") == "number" else "Control+End")
        typed = "42" if box.get_attribute("type") == "number" else "Ab c"
        page.keyboard.type(typed)
        value = box.input_value()
        if typed not in value:
            failed.append(f"{where}: typing into “{name}” gave {value!r}")
        for _ in typed:
            page.keyboard.press("Backspace")
        if box.input_value() != before:
            failed.append(f"{where}: Backspace in “{name}” left {box.input_value()!r}")
    print(f"  {where}: typed into {count} boxes", flush=True)


def steps(page, work, failed):
    nav = lambda name: (page.click(f"#nav-{name}"), page.wait_for_timeout(500))  # noqa: E731
    sweep = lambda where: type_into_every_box(page, where, failed)  # noqa: E731
    open_standup = lambda: page.click("button.meeting:has-text('Daily stand-up')")  # noqa: E731

    open_standup()
    page.wait_for_selector("text=Who was this?")
    sweep("meeting")
    page.locator("input[aria-label='Name for Speaker 3']").click()
    page.keyboard.type("Sam Lee")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Who was this?", state="detached", timeout=20000)
    if not any("Sam Lee" in p.read_text(encoding="utf-8") for p in (work / "sessions").glob("*/speakers.json")):
        failed.append("meeting: a name typed and confirmed with Enter was not saved")

    page.click("button:has-text('Send recap and follow-ups')")
    page.wait_for_selector("text=Follow-ups")
    sweep("send to a channel")
    page.select_option("select[aria-label='Where to send']", "gmail")
    sweep("send as email")

    nav("meetings")
    open_standup()
    page.locator("button.link:has-text('Hand to AI')").first.click()
    page.wait_for_selector("text=Hand this to the AI")
    sweep("hand to AI")

    nav("upcoming")
    sweep("upcoming")
    page.locator("input[aria-label='Prepare for']").click()
    page.keyboard.type("Acme")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=send Acme the written proposal", timeout=8000)

    nav("search")
    sweep("search")
    page.locator("input[type=search]").click()
    page.keyboard.type("firewall")
    page.keyboard.press("Enter")
    page.wait_for_selector("button.meeting:has-text('Weekly migration sync')", timeout=8000)

    calendar = work / "calendar.json"
    saved = calendar.read_bytes()
    calendar.unlink()  # so that the box for connecting one is on the page
    nav("connectors")
    sweep("connectors")
    calendar.write_bytes(saved)
    page.click("button:has-text('Connect Jira')")
    page.wait_for_selector("input[aria-label='Jira site']")
    sweep("Jira sign-in")

    for name in ("actions", "people", "tasks", "settings"):
        nav(name)
        if name == "settings":  # its templates arrive after the rest of the page
            page.wait_for_selector("#templates details", state="attached", timeout=20000)
        sweep(name)

    page.click("button:has-text('Change…')")
    page.wait_for_selector("text=Who should write your notes?")
    page.click("text=With my company's AI")
    sweep("company AI setup")
    page.click("button:has-text('Cancel')")
    page.wait_for_selector("#nav-meetings")

    # Forms that need a live Jira or a task in progress: the same page code, given their data.
    page.evaluate("""() => document.getElementById('main').replaceChildren(taskQuestions(
        {questions: ['Which branch?'], order: [], plan: [], understanding: ''}, {repos: []}))""")
    sweep("task questions")
    page.evaluate("""() => document.getElementById('main').replaceChildren(
        ...['text', 'long', 'number'].map(kind => fieldInput({kind, name: kind, value: '', options: []})[0]))""")
    sweep("ticket fields")


def walk(work, url, engine):
    from playwright.sync_api import sync_playwright
    failed, problems = [], []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="light")
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.add_init_script(c.SHIM)
        page.goto(url)
        page.wait_for_selector("#nav-meetings")
        try:
            steps(page, work, failed)
        except Exception as e:  # a step that hangs must not hide what already failed
            failed.append(f"stopped at: {str(e).splitlines()[0]}")
        browser.close()
    return failed + [f"page error: {p}" for p in problems]


def add_unnamed_voice(work):
    for p in (work / "sessions").glob("*/session.json"):
        if json.loads(p.read_text())["title"] == "Daily stand-up":
            speakers = json.loads((p.parent / "speakers.json").read_text())
            speakers["Speaker 3"] = {"known": False, "seconds": 30, "first": [1.0, "Hello there"]}
            (p.parent / "speakers.json").write_text(json.dumps(speakers))
            return p.parent.name


def main():
    engine = sys.argv[1]
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as shots:
        work = c.build_world(Path(t))
        add_unnamed_voice(work)
        server = subprocess.Popen([c.backend_python(), str(ROOT / "video" / "capture_screens.py"), "--serve", t])
        try:
            time.sleep(c.BACKEND_START_S)
            failed = walk(work, f"http://127.0.0.1:{c.PORT}/", engine)
        finally:
            server.terminate()
            server.wait()
        if not failed:  # the other controls, on a fresh set of meetings
            work = c.build_world(Path(t) / "fresh")
            server = subprocess.Popen([c.backend_python(), str(ROOT / "video" / "capture_screens.py"), "--serve", str(Path(t) / "fresh")])
            try:
                time.sleep(c.BACKEND_START_S)
                c.capture(work, f"http://127.0.0.1:{c.PORT}/", engine, Path(shots))
            finally:
                server.terminate()
                server.wait()
    if failed:
        sys.exit("The app's window failed:\n- " + "\n- ".join(failed))
    print(f"every page and every box worked in {engine}")


if __name__ == "__main__":
    main()
