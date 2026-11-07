"""Which window titles count as a meeting, for each app, on both systems' title styles
(macOS: the active tab's title; Windows: the tab's title, then the browser's name).

    python -m pytest tests
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402

CFG = {"meeting_window_exclude_regex": ms.DEFAULTS["meeting_window_exclude_regex"],
       "meeting_apps": ["teams", "zoom", "browsers"]}
CHROME, TEAMS, ZOOM = ms.APPS_BY_ID["chrome"], ms.APPS_BY_ID["teams"], ms.APPS_BY_ID["zoom"]


@pytest.mark.parametrize("title,want", [
    ("Meet - Weekly sync", ("Google Meet", "Weekly sync")),
    ("Meet - Weekly sync - Google Chrome", ("Google Meet", "Weekly sync")),
    ("Meet – Weekly sync — Mozilla Firefox", ("Google Meet", "Weekly sync")),
    ("Meet - Weekly sync and 2 more pages - Personal - Microsoft​ Edge", ("Google Meet", "Weekly sync")),
    ("(1) Meet - Weekly sync - Google Chrome", ("Google Meet", "Weekly sync")),
    ("Meet - abc-defg-hij - Google Chrome", ("Google Meet", None)),
    ("Sprint review | Microsoft Teams - Google Chrome", ("Microsoft Teams", "Sprint review")),
    ("Zoom - Google Chrome", ("Zoom", None)),
    ("Zoom Meeting", ("Zoom", None)),
    ("Webex Meetings - Google Chrome", ("Webex", None)),
    ("Jitsi Meet - Google Chrome", ("Jitsi Meet", None)),
])
def test_browser_meeting_tabs(title, want):
    assert ms.meeting_window(CFG, CHROME, [title]) == want


@pytest.mark.parametrize("title", [
    "Google Meet - Google Chrome",  # Meet's start page, not a call
    "Inbox (3) - me@example.com - Gmail - Google Chrome",
    "Chat | Microsoft Teams - Google Chrome",
    "Calendar | Microsoft Teams",
    "Zoom tips for remote teams - YouTube - Google Chrome",
    "One platform to connect | Zoom - Google Chrome",
    "New Tab - Google Chrome",
])
def test_other_tabs_are_not_meetings(title):
    assert ms.meeting_window(CFG, CHROME, [title]) is None


def test_first_meeting_window_wins():
    titles = ["New Tab - Google Chrome", "Meet - Design review - Google Chrome"]
    assert ms.meeting_window(CFG, CHROME, titles) == ("Google Meet", "Design review")


def test_teams_app_titles():
    assert ms.meeting_window(CFG, TEAMS, ["Chat | Microsoft Teams", "Microsoft Teams"]) is None
    assert ms.meeting_window(CFG, TEAMS, ["Sprint review | Microsoft Teams"]) == \
        ("Microsoft Teams", "Sprint review")
    assert ms.meeting_window(CFG, TEAMS, ["Sprint review"]) == ("Microsoft Teams", "Sprint review")


def test_zoom_app_titles():
    assert ms.meeting_window(CFG, ZOOM, ["Zoom Workplace"]) is None
    assert ms.meeting_window(CFG, ZOOM, ["Zoom Meeting"]) == ("Zoom", None)


def test_enabled_apps():
    ids = [a["id"] for a in ms.enabled_apps(CFG)]
    assert ids[:2] == ["teams", "zoom"] and {"chrome", "edge", "firefox", "safari"} <= set(ids)
    assert [a["id"] for a in ms.enabled_apps({**CFG, "meeting_apps": ["zoom", "chrome"]})] == \
        ["zoom", "chrome"]


class FakeProbe:
    def __init__(self, states):
        self.states = states

    def app_states(self, apps):
        return {a["id"]: self.states.get(a["id"], (False, False)) for a in apps}


def daemon(monkeypatch, windows):
    monkeypatch.setattr(ms.plat, "app_windows", lambda app: [(app["name"], t) for t in windows.get(app["id"], [])])
    d = ms.Daemon.__new__(ms.Daemon)
    d.cfg, d.apps = CFG, ms.enabled_apps(CFG)
    return d


def test_find_call(monkeypatch):
    d = daemon(monkeypatch, {"chrome": ["Meet - Standup - Google Chrome"]})
    # A browser with the mic but no meeting tab (dictation, a voice note) is no call.
    assert d.find_call(FakeProbe({"edge": (True, False)}).app_states(d.apps)) is None
    app, service, title = d.find_call(FakeProbe({"chrome": (True, True)}).app_states(d.apps))
    assert (app["id"], service, title) == ("chrome", "Google Meet", "Standup")
    # A native app holding the mic is a call on its own, and comes first.
    app, service, title = d.find_call(FakeProbe({"zoom": (True, False), "chrome": (True, True)})
                                      .app_states(d.apps))
    assert (app["id"], service, title) == ("zoom", "Zoom", None)
