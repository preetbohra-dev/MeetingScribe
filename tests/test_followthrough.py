"""Note templates, the action item list, briefs, what assistants can read, and consent.

    python -m pytest tests
"""

import datetime as dt
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefs  # noqa: E402
import connectors as cn  # noqa: E402
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402
import note_templates as nt  # noqa: E402
import notes_mcp  # noqa: E402
import tracker as tr  # noqa: E402


def note_text(summary, actions):
    return ("# M\n\n## Summary\n" + "".join(f"- {s}\n" for s in summary) + "\n## Action Items\n" + "".join(f"- {a}\n" for a in actions)
            + "\n---\n\n## Transcript\n\n**[00:01] Me:** hello  \n")


class World:
    def __init__(self, root, monkeypatch):
        self.root, self.work, self.notes = root, root / "w", root / "n"
        (self.work / "sessions").mkdir(parents=True)
        self.notes.mkdir()
        self.config = root / "config.toml"
        self.config.write_text(f'my_name = "Me"\nnotify = false\nwork_dir = {json.dumps(str(self.work))}\noutput_dir = {json.dumps(str(self.notes))}\nlog_dir = {json.dumps(str(root / "l"))}\n', encoding="utf-8")
        monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(self.config))
        self.vault = {}

        def keychain(action, account, secret=None, service=None):
            assert service == cn.KEY_SERVICE
            if action == "set":
                self.vault[account] = secret
            elif action == "delete":
                self.vault.pop(account, None)
            return self.vault.get(account) if action == "get" else None
        monkeypatch.setattr(ms, "keychain", keychain)

    @property
    def cfg(self):
        return ms.load_config()

    def meeting(self, sid, title, day, actions=(), summary=("It went well.",), speakers=(), status="done"):
        d = self.work / "sessions" / sid
        d.mkdir()
        note = self.notes / f"{sid}.md"
        note.write_text(note_text(summary, actions), encoding="utf-8")
        (d / "transcript.json").write_text(json.dumps([{"start": 1.0, "speaker": "Me", "text": "hello"}]), encoding="utf-8")
        (d / "session.json").write_text(json.dumps({"title": title, "status": status, "started_at": f"{day}T10:00:00+00:00", "notes": str(note), "duration_s": 600}), encoding="utf-8")
        (d / "speakers.json").write_text(json.dumps({s: {"known": True} for s in speakers}), encoding="utf-8")
        return d, note


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


class Words:
    """A model that gives a fixed answer and remembers the question."""

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def chat(self, user, max_tokens=1500):
        self.asked.append(user)
        return self.answer


# -- templates ------------------------------------------------------------------------------------

@pytest.mark.parametrize("title,want", [("Daily Standup", "standup"), ("Priya / Me 1:1", "one-on-one"), ("SEV1 incident review", "incident"),
                                        ("Scrumptious lunch", "general"), ("", "general"), (None, "general"), ("Q3 planning", "planning")])
def test_template_is_chosen_by_whole_words(world, title, want):
    assert nt.choose(world.cfg, title)["id"] == want


def test_your_template_beats_a_built_in_one(world):
    nt.add(world.cfg, "Board", ["planning"], "Decisions only.", ["Decisions: what was approved"])
    assert nt.choose(world.cfg, "Board planning")["name"] == "Board"


@pytest.mark.parametrize("heading", ["Summary", "action items", "Transcript", "Bad#Heading", "x\n## Injected"])
def test_a_template_cannot_break_the_note(world, heading):
    with pytest.raises(nt.TemplateError):
        nt.add(world.cfg, "T", [], "", [f"{heading}: things"] if "\n" not in heading else [heading + ": x"])


def test_template_needs_a_name_a_new_name_and_few_sections(world):
    with pytest.raises(nt.TemplateError):
        nt.add(world.cfg, "  ", [], "", [])
    with pytest.raises(nt.TemplateError):
        nt.add(world.cfg, "stand-up", [], "", [])
    with pytest.raises(nt.TemplateError):
        nt.add(world.cfg, "Long", [], "", [f"S{i}: x" for i in range(7)])


def test_summary_asks_for_template_sections_before_action_items(world):
    llm = Words("## Summary\n- a\n\n## Blockers\n- waiting on Dan\n\n## Action Items\n- Priya: do it")
    out = ms.summarize(world.cfg, "[00:01] Me: hi", llm, nt.find(world.cfg, "standup"))
    prompt = llm.asked[0]
    assert prompt.index("## Summary") < prompt.index("## Blockers") < prompt.index("## Action Items") and "one bullet per person" in prompt
    assert "- waiting on Dan" in out and "- [ ] Priya: do it" in out  # only action items get boxes


def test_general_prompt_has_no_extra_section(world):
    llm = Words("## Summary\n- a\n\n## Action Items\n- None identified.")
    ms.summarize(world.cfg, "x", llm)
    assert "\n\n## Action Items" in llm.asked[0] and "{extra}" not in llm.asked[0] and "{focus}" not in llm.asked[0]


def test_note_records_template_and_app_shows_its_sections(world, monkeypatch):
    d, note = world.meeting("2026-11-01_100000", "Daily standup", "2026-11-01")
    llm = Words("## Summary\n- a\n\n## Blockers\n- waiting on Dan\n\n## Action Items\n- None identified.")
    meta = json.loads((d / "session.json").read_text())
    meta["id"] = d.name
    out = ms.write_note(world.cfg, meta, [{"start": 1, "speaker": "Me", "text": "hi"}], {}, llm)
    text = out.read_text(encoding="utf-8")
    assert "**Template:** Stand-up" in text and meta["template"] == "standup"
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["template"] == "standup"
    assert ms.template_sections(text) == [{"heading": "Blockers", "items": ["waiting on Dan"]}]


class Broken:
    def chat(self, *a, **k):
        raise RuntimeError("no model")


def test_failed_rewrite_keeps_the_old_note(world, monkeypatch):
    d, note = world.meeting("2026-11-01_100000", "Sync", "2026-11-01", ["[ ] **Me**: keep this"])
    before = note.read_text(encoding="utf-8")
    monkeypatch.setattr(ms, "LLM", lambda cfg, *a: Broken())
    with pytest.raises(RuntimeError):
        ms.cmd_rewrite(world.cfg, type("A", (), {"session": d.name, "template": "standup"})())
    assert note.read_text(encoding="utf-8") == before


def test_rewrite_changes_the_template_and_keeps_jira_links(world, monkeypatch):
    d, note = world.meeting("2026-11-01_100000", "Sync", "2026-11-01")
    jira = f"{ms.JIRA_BEGIN}\n## Jira\n\n- Created [AB-1](x): y\n{ms.JIRA_END}\n"
    note.write_text(note.read_text(encoding="utf-8").replace("\n---\n", "\n" + jira + "\n---\n"), encoding="utf-8")
    monkeypatch.setattr(ms, "LLM", lambda cfg, *a: Words("## Summary\n- new\n\n## Blockers\n- none\n\n## Action Items\n- None identified."))
    ms.cmd_rewrite(world.cfg, type("A", (), {"session": d.name, "template": "standup"})())
    meta = json.loads((d / "session.json").read_text())
    text = Path(meta["notes"]).read_text(encoding="utf-8")
    assert meta["template"] == "standup" and "- new" in text and "AB-1" in text and text.index("AB-1") < text.index("## Transcript")
    assert not note.exists() or Path(meta["notes"]) == note


def test_saved_questions_can_be_replaced(world):
    assert "What did we decide?" in nt.questions(world.cfg)
    nt.save_questions(world.cfg, ["  Who owns it? ", ""])
    assert nt.questions(world.cfg) == ["Who owns it?"]


# -- action items -----------------------------------------------------------------------------------

def track(world):
    return tr.Tracker(ms, world.cfg)


def test_items_are_gathered_and_none_is_skipped(world):
    world.meeting("2026-11-01_100000", "A", "2026-11-01", ["[ ] **Priya**: update the jobs (Thursday)", "[ ] Nobody bold"])
    world.meeting("2026-11-02_100000", "B", "2026-11-02", ["None identified."])
    items = track(world).items()
    assert {(i["owner"], i["task"]) for i in items} == {("Priya", "update the jobs (Thursday)"), ("Unassigned", "Nobody bold")}


def test_same_work_raised_again_is_carried_over(world):
    world.meeting("2026-11-01_100000", "Sync", "2026-11-01", ["[ ] **Priya**: update the reporting jobs"])
    world.meeting("2026-11-08_100000", "Sync", "2026-11-08", ["[ ] **Priya**: update the reporting jobs (Friday)", "[ ] **Dan**: update the reporting jobs"])
    items = track(world).items()
    priya = next(i for i in items if i["owner"] == "Priya")
    assert len(items) == 2 and priya["carried"] == 1 and priya["task"].endswith("(Friday)") and len(priya["meetings"]) == 2


def test_ticking_here_ticks_the_note_and_undo_unticks(world):
    _, note = world.meeting("2026-11-01_100000", "A", "2026-11-01", ["[ ] **Priya**: do it (a.b)"])
    t = track(world)
    item = t.items()[0]
    t.set_status(item["id"], "done")
    assert "- [x] **Priya**: do it (a.b)" in note.read_text(encoding="utf-8") and not t.items()
    assert t.items(True)[0]["status"] == "done"
    t.set_status(item["id"], "open")
    assert "- [ ] **Priya**: do it (a.b)" in note.read_text(encoding="utf-8") and t.items()[0]["status"] == "open"


def test_ticking_a_box_in_the_note_counts(world):
    world.meeting("2026-11-01_100000", "A", "2026-11-01", ["[x] **Priya**: do it"])
    assert not track(world).items() and track(world).items(True)[0]["status"] == "done"


def test_done_work_raised_later_is_new_but_dismissed_stays_gone(world):
    world.meeting("2026-11-01_100000", "A", "2026-11-01", ["[x] **Priya**: do it", "[ ] **Dan**: not real"])
    world.meeting("2026-11-08_100000", "A", "2026-11-08", ["[ ] **Priya**: do it", "[ ] **Dan**: not real"])
    t = track(world)
    t.set_status(next(i for i in t.items() if i["owner"] == "Dan")["id"], "dismissed")
    left = t.items()
    assert [(i["owner"], i["carried"]) for i in left] == [("Priya", 0)]


def test_unknown_item_and_status_are_refused(world):
    with pytest.raises(ValueError):
        track(world).set_status("nope", "done")
    with pytest.raises(ValueError):
        track(world).set_status("nope", "deleted")


# -- calendar and briefs ------------------------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0//EN
BEGIN:VTIMEZONE
TZID:Pacific Standard Time
BEGIN:STANDARD
DTSTART:16010101T020000
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:weekly-1
SUMMARY:Migration sync
DTSTART;TZID=Pacific Standard Time:20261102T090000
DTEND;TZID=Pacific Standard Time:20261102T093000
RRULE:FREQ=WEEKLY;BYDAY=MO
EXDATE;TZID=Pacific Standard Time:20261116T090000
ATTENDEE;CN="Priya Shah";PARTSTAT=ACCEPTED:mailto:priya@example.com
ATTENDEE:mailto:dan@example.com
END:VEVENT
BEGIN:VEVENT
UID:allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261110
DTEND;VALUE=DATE:20261111
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261110T170000Z
DTEND:20261110T173000Z
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").encode()
UTC = dt.timezone.utc


def test_calendar_repeats_time_zones_and_what_is_left_out():
    events = briefs.parse_events(ICS, dt.datetime(2026, 11, 2, tzinfo=UTC), dt.datetime(2026, 11, 25, tzinfo=UTC))
    assert [dt.datetime.fromisoformat(e["start"]).astimezone(UTC).day for e in events] == [2, 9, 23]  # the 16th is excepted
    first = dt.datetime.fromisoformat(events[0]["start"])
    assert first.astimezone(UTC) == dt.datetime(2026, 11, 2, 17, 0, tzinfo=UTC)  # 9:00 Pacific, after the clocks changed
    assert events[0]["people"] == ["Priya Shah", "dan@example.com"]


def test_bad_calendar_is_a_sentence():
    with pytest.raises(briefs.BriefError):
        briefs.parse_events(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:nonsense\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n", dt.datetime(2026, 1, 1, tzinfo=UTC), dt.datetime(2026, 2, 1, tzinfo=UTC))


@pytest.mark.parametrize("url", ["http://cal.example/a.ics", "ftp://x/y", "javascript:alert(1)", "", "https://"])
def test_only_secure_calendar_links(url):
    with pytest.raises(briefs.BriefError):
        briefs.calendar_link(url)


def test_webcal_is_https():
    assert briefs.calendar_link(" webcal://x.example/a.ics ") == "https://x.example/a.ics"


class Reply(io.BytesIO):
    def __init__(self, body, url="https://cal.example/secret"):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_calendar(monkeypatch, body=ICS, url="https://cal.example/secret"):
    def opener(request, timeout=None, context=None):
        if isinstance(body, Exception):
            raise body
        return Reply(body, url)
    monkeypatch.setattr(briefs.urllib.request, "urlopen", opener)


def test_a_wrong_link_is_not_saved(world, monkeypatch):
    fake_calendar(monkeypatch, b"<html>sign in</html>")
    with pytest.raises(briefs.BriefError):
        briefs.Briefs(ms, world.cfg).connect("https://cal.example/secret")
    assert not world.vault and not briefs.Briefs(ms, world.cfg).connected()


def test_a_redirect_to_plain_http_is_refused(world, monkeypatch):
    fake_calendar(monkeypatch, ICS, "http://cal.example/plain")
    with pytest.raises(briefs.BriefError):
        briefs.read_calendar("https://cal.example/secret", None)


def test_connect_keeps_link_only_in_the_keychain(world, monkeypatch):
    fake_calendar(monkeypatch)
    b = briefs.Briefs(ms, world.cfg)
    b.connect("https://cal.example/secret")
    assert world.vault["calendar"] == "https://cal.example/secret" and b.connected()
    assert "secret" not in (world.work / "calendar.json").read_text(encoding="utf-8")
    b.disconnect()
    assert not world.vault and not b.connected()


def test_a_failed_refresh_keeps_events_and_never_shows_the_link(world, monkeypatch):
    fake_calendar(monkeypatch)
    b = briefs.Briefs(ms, world.cfg)
    b.connect("https://cal.example/secret")
    events = b._cache()["events"]
    fake_calendar(monkeypatch, urllib.error.URLError("cannot reach https://cal.example/secret"))
    b.sync(force=True)
    cache = b._cache()
    assert cache["events"] == events and cache["error"] and "secret" not in cache["error"]


def test_brief_by_series_and_by_people(world):
    world.meeting("2026-11-01_100000", "Weekly migration sync", "2026-11-01", ["[ ] **Priya**: update jobs"], ["Move finished."], ["Priya"])
    world.meeting("2026-10-20_100000", "Vendor call", "2026-10-20", ["[ ] **Sam**: send contract"], ["Contract talk."], ["Sam"])
    world.meeting("2026-10-01_100000", "Lunch planning", "2026-10-01", ["[ ] **Zed**: book"], ["Food."], ["Zed"])
    b = briefs.Briefs(ms, world.cfg)
    series = b.brief("Migration sync", ["Me", "Dan"])
    assert [m["title"] for m in series["last"]] == ["Weekly migration sync"] and [i["owner"] for i in series["open"]] == ["Priya"]
    person = b.brief("Catch up", ["Sam Lee"])
    assert [m["title"] for m in person["last"]] == ["Vendor call"] and [i["task"] for i in person["open"]] == ["send contract"]
    assert not b.brief("Something brand new", ["Nobody Known"])["something"]


def test_notice_comes_once_and_only_with_something_to_say(world):
    world.meeting("2026-11-01_100000", "Migration sync", "2026-11-01", ["[ ] **Priya**: update jobs"])
    soon = (dt.datetime.now().astimezone() + dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    events = [{"id": "a", "title": "Migration sync", "start": soon, "end": soon, "people": []},
              {"id": "b", "title": "Brand new", "start": soon, "end": soon, "people": []}]
    ms.write_json(world.work / "calendar.json", {"connected": True, "events": events})
    b = briefs.Briefs(ms, world.cfg)
    first = b.due_notices(10)
    assert len(first) == 1 and "Migration sync" in first[0] and b.due_notices(10) == []


# -- what an assistant can read --------------------------------------------------------------------

def talk(world, *messages):
    out = io.BytesIO()
    notes_mcp.serve(ms, world.cfg, io.BytesIO(b"".join(m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def call(n, name, **arguments):
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_protocol_handshake(world, monkeypatch):
    stdout = sys.stdout
    try:
        r = talk(world, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
    finally:
        sys.stdout = stdout
    assert [x["id"] for x in r] == [1, 2, 3]  # the notification gets no answer
    assert r[0]["result"]["protocolVersion"] == "2025-03-26" and r[1]["result"]["protocolVersion"] == notes_mcp.VERSIONS[0] and r[2]["result"] == {}
    assert r[0]["result"]["capabilities"] == {"tools": {}} and "instructions" in r[0]["result"]


def test_every_tool_only_reads(world):
    assert all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] for t in talk(world, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"])


def test_tools_return_meetings(world):
    d, _ = world.meeting("2026-11-01_100000", "Migration sync", "2026-11-01", ["[ ] **Priya**: update jobs"], ["Move finished."], ["Priya"])
    world.meeting("2026-11-02_100000", "Still going", "2026-11-02", status="recording")
    r = talk(world, call(1, "list_meetings"), call(2, "search_meetings", query="hello"), call(3, "get_meeting", id=d.name), call(4, "get_meeting", id=d.name, include_transcript=True),
             call(5, "open_action_items", owner="priya"), call(6, "prepare_for_meeting", about="Migration"))
    value = [json.loads(x["result"]["content"][0]["text"]) for x in r]
    assert [m["id"] for m in value[0]] == [d.name] and value[0][0]["people"] == ["Priya"]  # never one still being recorded
    assert value[1][0]["session"] == d.name
    assert value[2]["summary"] == ["Move finished."] and "transcript" not in value[2] and "hello" in value[3]["transcript"]
    assert value[4][0]["owner"] == "Priya" and value[5]["something"]


@pytest.mark.parametrize("sid", ["../../etc", "..", "nope", "a/b"])
def test_only_real_meetings_can_be_read(world, sid):
    r = talk(world, call(1, "get_meeting", id=sid))[0]
    assert r["result"]["isError"] and "no meeting" in r["result"]["content"][0]["text"]


def test_bad_requests_get_errors_not_crashes(world):
    r = talk(world, b"not json\n", b"\n", b"[1,2]\n", {"jsonrpc": "2.0", "id": 1, "method": "nope"}, call(2, "write_note"), call(3, "search_meetings"), call(4, "list_meetings", limit=True),
             {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": "x"})
    assert r[0]["error"]["code"] == -32700 and r[1]["error"]["code"] == -32600 and r[2]["error"]["code"] == -32601 and r[3]["error"]["code"] == -32602
    assert r[4]["result"]["isError"] and r[5]["result"]["isError"] and r[6]["error"]["code"] == -32602


def test_off_until_turned_on(world):
    with pytest.raises(SystemExit) as e:
        ms.cmd_mcp(world.cfg, None)
    assert "off" in str(e.value)


def test_app_shows_how_to_connect_and_switches(world):
    api = app.Api()
    assert api.mcp_state()["on"] is False
    state = api.mcp_set(True)
    assert state["on"] and json.loads(state["config"])["mcpServers"]["meetingscribe"]["args"][-1] == "mcp" and state["claude"].startswith("claude mcp add meetingscribe -- ")
    assert world.cfg["mcp_enabled"] is True


# -- consent and keeping meetings ---------------------------------------------------------------------

class Probe:
    def __init__(self):
        self.mic = (True, True)

    def default_input_device(self):
        return "mic"

    def app_states(self, apps):
        return {a["id"]: self.mic if a["id"] == "zoom" else (False, False) for a in apps}


class Rec:
    def __init__(self, cfg, directory, app, service, title):
        directory.mkdir(parents=True)
        (directory / "mic.pcm").write_bytes(b"x")
        self.dir, self.service, self.warnings = directory, service, []
        self.meta = {"title": title or "?", "started_at": "now"}

    def start(self):
        pass

    def stop(self):
        return 500.0

    def set_title(self, title):
        pass

    def watchdog(self, *a):
        pass


@pytest.fixture
def recorder(world, monkeypatch):
    monkeypatch.setattr(ms.plat, "AudioProbe", Probe)
    monkeypatch.setattr(ms, "Recording", Rec)
    told = []
    monkeypatch.setattr(ms, "notify", lambda cfg, text: told.append(text))
    d = ms.Daemon({**world.cfg, "never_record": ["HR", ""], "meeting_apps": ["zoom"], "start_after_s": 0})
    d.pump_queue = lambda: None
    d.told, d.window = told, ("Zoom", "Sync")
    monkeypatch.setattr(ms, "meeting_window", lambda cfg, app, titles=None: d.window)
    return d


def sessions(d):
    return list(d.sessions.iterdir())


def test_a_listed_meeting_never_starts(recorder):
    recorder.window = ("Zoom", "Quarterly hr review")
    recorder.tick()
    recorder.tick()
    assert recorder.rec is None and not sessions(recorder) and len(recorder.told) == 1 and "Not recording" in recorder.told[0]


def test_a_title_that_appears_late_deletes_the_recording(recorder):
    recorder.window = None
    recorder.find_call = lambda states: (recorder.apps[0], "Zoom", None)
    recorder.tick()
    assert recorder.rec and sessions(recorder)
    recorder.window = ("Zoom", "HR chat")
    recorder.tick()
    assert recorder.rec is None and not sessions(recorder) and "deleted" in recorder.told[-1]
    recorder.window = None
    recorder.tick()  # the same call goes on without a name on screen, and stays unrecorded
    assert recorder.rec is None and not sessions(recorder)


def test_an_ordinary_meeting_records_and_reminds_about_consent(recorder):
    recorder.tick()
    assert recorder.rec and "Make sure everyone knows" in recorder.told[0]


def test_after_a_refused_call_the_next_one_records(recorder):
    recorder.window = ("Zoom", "HR chat")
    recorder.tick()
    recorder.probe.mic = (False, False)
    recorder.tick()
    recorder.probe.mic, recorder.window = (True, True), ("Zoom", "Sync")
    recorder.tick()
    assert recorder.rec


def test_status_offers_the_notice(world):
    assert "recording" in app.Api().status()["notice"]


def days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def test_old_meetings_are_deleted_and_recent_ones_kept(world):
    old, old_note = world.meeting("2026-01-01_100000", "Old", days_ago(40))
    new, new_note = world.meeting("2026-11-01_100000", "New", days_ago(2))
    going, _ = world.meeting("2026-01-02_100000", "Unfinished", days_ago(40), status="recorded")
    old_note.with_suffix(".wav").write_bytes(b"x")
    (world.notes / "index.jsonl").write_text(json.dumps({"id": old.name}) + "\n" + json.dumps({"id": new.name}) + "\n", encoding="utf-8")
    cfg = {**world.cfg, "retain_days": 30}
    assert ms.sweep_retention(cfg) == (1, 0)
    assert not old.exists() and not old_note.exists() and not old_note.with_suffix(".wav").exists()
    assert new.exists() and new_note.exists() and going.exists()
    assert old.name not in (world.notes / "index.jsonl").read_text(encoding="utf-8")


def test_audio_only_keeps_the_note(world):
    d, note = world.meeting("2026-01-01_100000", "Old", days_ago(10))
    note.with_suffix(".wav").write_bytes(b"x")
    (d / "voices").mkdir()
    (d / "voices" / "a.wav").write_bytes(b"x")
    cfg = {**world.cfg, "retain_audio_days": 7}
    assert ms.sweep_retention(cfg) == (0, 1)
    assert note.exists() and not note.with_suffix(".wav").exists() and not (d / "voices").exists()
    assert ms.sweep_retention(cfg) == (0, 0)


def test_keep_forever_touches_nothing(world):
    d, note = world.meeting("2026-01-01_100000", "Old", days_ago(4000))
    assert ms.sweep_retention(world.cfg) == (0, 0) and d.exists() and note.exists()


def test_a_note_outside_the_notes_folder_is_left_alone(world, tmp_path):
    d, note = world.meeting("2026-01-01_100000", "Old", days_ago(40))
    elsewhere = tmp_path / "other.md"
    elsewhere.write_text("mine", encoding="utf-8")
    meta = json.loads((d / "session.json").read_text())
    (d / "session.json").write_text(json.dumps({**meta, "notes": str(elsewhere)}))
    ms.sweep_retention({**world.cfg, "retain_days": 30})
    assert elsewhere.exists() and not d.exists()


def test_preview_counts_what_saving_would_delete(world):
    world.meeting("2026-01-01_100000", "Old", days_ago(40))
    api = app.Api()
    assert api.retention_preview(30, 0) == {"meetings": 1, "audio": 0} and api.retention_preview(0, 0) == {"meetings": 0, "audio": 0}


def test_settings_validate_the_new_values(world):
    api = app.Api()
    assert api.save_settings({"retain_days": 30, "never_record": ["HR"], "consent_notice": "Hi"})["ok"]
    cfg = world.cfg
    assert cfg["retain_days"] == 30 and cfg["never_record"] == ["HR"] and cfg["consent_notice"] == "Hi"
    assert not api.save_settings({"retain_days": -1})["ok"] and not api.save_settings({"retain_days": True})["ok"] and not api.save_settings({"never_record": "HR"})["ok"]


def calendar_titled(*titles):
    day = (dt.datetime.now(UTC) + dt.timedelta(days=2)).strftime("%Y%m%d")
    events = "".join(f"BEGIN:VEVENT\r\nUID:{i}\r\nSUMMARY:{t}\r\nDTSTART:{day}T1{i}0000Z\r\nDTEND:{day}T1{i}3000Z\r\nEND:VEVENT\r\n" for i, t in enumerate(titles))
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{events}END:VCALENDAR\r\n".encode()


def test_a_calendar_that_hides_meeting_names_says_how_to_fix_it(world, monkeypatch):
    fake_calendar(monkeypatch, calendar_titled("Busy", "Tentative"))
    with pytest.raises(briefs.BriefError, match="Can view all details"):
        briefs.Briefs(ms, world.cfg).connect("https://cal.example/secret")
    assert not briefs.Briefs(ms, world.cfg).connected()


def test_a_calendar_with_some_busy_time_is_fine(world, monkeypatch):
    fake_calendar(monkeypatch, calendar_titled("Busy", "Design review"))
    b = briefs.Briefs(ms, world.cfg)
    b.connect("https://cal.example/secret")
    assert [e["title"] for e in b.upcoming()["events"]] == ["Busy", "Design review"]
