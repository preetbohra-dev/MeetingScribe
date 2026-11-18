"""Consent: notes are only written for a call you confirm everyone was told about, a voice is
only saved with the person's agreement, and each of these leaves a dated line in a record
that shows when it has been changed.

    python -m pytest tests
"""

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = {**ms.DEFAULTS, "work_dir": tmp_path / "work", "output_dir": tmp_path / "notes",
         "log_dir": tmp_path / "logs", "jira": dict(ms.JIRA_DEFAULTS), "notify": False,
         "meeting_apps": ["zoom"], "start_after_s": 0, "min_meeting_s": 60}
    (c["work_dir"] / "sessions").mkdir(parents=True)
    c["output_dir"].mkdir()
    return c


def session(cfg, sid="2026-11-08_100000", **meta):
    d = cfg["work_dir"] / "sessions" / sid
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"title": "Sync", "source": "Zoom", "status": "needs_consent",
                                                "started_at": "2026-11-08T10:00:00+05:30",
                                                "ended_at": "2026-11-08T10:30:00+05:30", **meta}))
    return d


def status(d):
    return json.loads((d / "session.json").read_text())["status"]


# -- the record


def test_each_entry_is_dated_signed_and_chained(cfg):
    first = ms.log_consent(cfg, "told_everyone", meeting="a")
    second = ms.log_consent(cfg, "voice_agreed", person="Priya")
    assert first["user"] and first["computer"] and dt.datetime.fromisoformat(first["time"]).tzinfo
    entries, problem = ms.check_consent_log(cfg)
    assert problem is None and [e["event"] for e in entries] == ["told_everyone", "voice_agreed"]
    assert second["prev"] != first["prev"]


@pytest.mark.parametrize("tamper", ["edit", "remove", "garble"])
def test_a_changed_record_is_noticed(cfg, tamper):
    for n in range(3):
        ms.log_consent(cfg, "told_everyone", meeting=str(n))
    path = ms.consent_log_path(cfg)
    lines = path.read_text(encoding="utf-8").splitlines()
    if tamper == "edit":
        lines[1] = lines[1].replace('"meeting": "1"', '"meeting": "9"')
    elif tamper == "remove":
        del lines[1]
    else:
        lines[1] = "{not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    entries, problem = ms.check_consent_log(cfg)
    caught = 3 if tamper == "edit" else 2  # an edit shows at the next line, which holds its hash
    assert problem and problem.startswith(f"line {caught} ") and len(entries) == caught - 1


def test_an_empty_record_is_fine(cfg):
    assert ms.check_consent_log(cfg) == ([], None)


# -- confirming a meeting


def test_confirming_saves_with_the_meeting_and_in_the_record(cfg):
    d = session(cfg)
    e = ms.confirm_consent(cfg, d)
    assert e["event"] == "told_everyone" and e["meeting"] == d.name and e["title"] == "Sync"
    assert e["during_call"] is False and e["call_ended"] == "2026-11-08T10:30:00+05:30"
    assert e["notice"] == cfg["consent_notice"]
    assert json.loads((d / "consent.json").read_text()) == e
    assert (cfg["work_dir"] / "consent-changed").exists()
    assert ms.confirm_consent(cfg, d) == e  # a second click changes nothing
    assert len(ms.check_consent_log(cfg)[0]) == 1


def test_confirming_during_the_call_says_so(cfg):
    d = session(cfg, status="recording", ended_at=None)
    assert ms.confirm_consent(cfg, d)["during_call"] is True


def test_processing_waits_for_consent(cfg, monkeypatch):
    d = session(cfg, status="recorded")
    ran = []
    monkeypatch.setattr(ms, "process_session", lambda cfg, d: ran.append(d))
    monkeypatch.setattr(ms, "setup_logging", lambda *a: None)
    args = type("A", (), {"session": str(d)})
    ms.cmd_process(cfg, args)
    assert not ran and status(d) == "needs_consent"
    ms.confirm_consent(cfg, d)
    ms.cmd_process(cfg, args)
    assert ran == [d]


def test_the_note_names_who_confirmed_and_when(cfg):
    turns = [{"start": 0.0, "end": 2.0, "speaker": "Me", "text": "Morning"}]
    meta = {"title": "Sync", "source": "Zoom", "started_at": "2026-11-08T10:00:00+05:30", "duration_s": 600,
            "consent": {"time": "2026-11-08T10:02:00+05:30", "user": "asha", "during_call": True}}

    class Words:
        def chat(self, user, max_tokens=1500):
            return "## Summary\n- Fine.\n\n## Action Items\n- None identified."
    text = ms.write_note(cfg, meta, turns, {}, Words()).read_text(encoding="utf-8")
    assert "**Consent:** asha confirmed everyone was told it was recorded, at 10:02 during the call" in text
    meta["consent"]["before_recording"] = True
    text = ms.write_note(cfg, meta, turns, {}, Words()).read_text(encoding="utf-8")
    assert "**Consent:** asha confirmed everyone agreed to be recorded, at 10:02, before recording started" in text


# -- the recorder


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
        self.dir, self.service, self.warnings = directory, service, []
        self.meta = {"status": "recording", "title": title or "?", "started_at": "2026-11-08T10:00:00+05:30"}
        self._save_meta()

    def _save_meta(self):
        (self.dir / "session.json").write_text(json.dumps(self.meta))

    def start(self):
        pass

    def stop(self):
        self.meta.update(status="recorded", ended_at=dt.datetime.now().astimezone().isoformat())
        self._save_meta()
        return 500.0

    def set_title(self, title):
        pass

    def watchdog(self, *a):
        pass


@pytest.fixture
def recorder(cfg, monkeypatch):
    monkeypatch.setattr(ms.plat, "AudioProbe", Probe)
    monkeypatch.setattr(ms, "Recording", Rec)
    monkeypatch.setattr(ms, "meeting_window", lambda cfg, app, titles=None: ("Zoom", "Sync"))
    told = []
    monkeypatch.setattr(ms, "notify", lambda cfg, text: told.append(text))
    d = ms.Daemon(cfg)
    d.pump_queue = lambda: None
    d.told = told
    return d


def read_status(cfg):
    return json.loads((cfg["work_dir"] / "status.json").read_text())


def test_nothing_is_recorded_until_everyone_agreed(recorder, cfg):
    for _ in range(3):
        recorder.tick()
    s = read_status(cfg)
    assert recorder.rec is None and not list(recorder.sessions.iterdir())
    assert (s["state"], s["app"], s["service"], s["title"]) == ("asking", "zoom", "Zoom", "Sync")
    assert len(recorder.told) == 1 and "Nothing is recorded until you confirm" in recorder.told[0]
    entry = ms.agree_to_record(cfg)
    assert entry["event"] == "everyone_agreed" and entry["before_recording"] is True and entry["title"] == "Sync"
    recorder.tick()
    s = read_status(cfg)
    assert s["state"] == "recording" and s["consented"] is True and s["session"] == recorder.rec.dir.name
    consent = json.loads((recorder.rec.dir / "consent.json").read_text())
    assert consent["time"] == entry["time"] and consent["meeting"] == recorder.rec.dir.name
    events = ms.check_consent_log(cfg)[0]
    assert [e["event"] for e in events] == ["everyone_agreed", "recording_started"]
    assert events[1]["agreed_at"] == entry["time"] and events[1]["meeting"] == recorder.rec.dir.name


def test_dont_record_leaves_that_call_alone(recorder, cfg):
    recorder.tick()
    assert ms.decline_recording(cfg)["event"] == "not_recorded"
    for _ in range(3):
        recorder.tick()
    assert recorder.rec is None and read_status(cfg)["state"] == "watching"
    assert ms.agree_to_record(cfg) is None  # nothing is being asked about any more
    recorder.probe.mic = (False, False)  # that call ends, and the next one is asked about again
    recorder.tick()
    recorder.probe.mic = (True, True)
    recorder.tick()
    assert read_status(cfg)["state"] == "asking"


def test_an_answer_only_counts_for_the_call_it_was_given_in(recorder, cfg):
    assert ms.agree_to_record(cfg) is None  # no call yet
    recorder.tick()
    ms.agree_to_record(cfg)
    recorder.probe.mic = (False, False)  # the call ends before the recorder acts on it
    recorder.tick()
    recorder.probe.mic = (True, True)
    recorder.tick()
    assert recorder.rec is None and read_status(cfg)["state"] == "asking"


def test_an_answer_from_before_a_restart_is_dropped(cfg, monkeypatch):
    monkeypatch.setattr(ms.plat, "AudioProbe", Probe)
    (cfg["work_dir"] / "consent-answer.json").write_text(json.dumps({"app": "zoom", "agreed": {"time": "x"}}))
    ms.Daemon(cfg)
    assert not (cfg["work_dir"] / "consent-answer.json").exists()


def test_pausing_stops_asking(recorder, cfg):
    recorder.tick()
    recorder.paused_flag.touch()
    recorder.step()
    assert recorder.asking is None and read_status(cfg)["state"] == "paused"


def test_the_banner_is_up_while_asking_and_recording(recorder, cfg, no_banner_window):
    recorder.step()
    assert len(no_banner_window.started) == 1
    no_banner_window.started[0].returncode = 1  # closed somehow
    recorder.banner_tries = 0
    recorder.step()
    assert len(no_banner_window.started) == 2  # back on screen
    ms.agree_to_record(cfg)
    recorder.step()
    assert recorder.rec and len(no_banner_window.started) == 2  # the same banner carries on
    recorder.probe.mic = (False, False)
    recorder.last_signal = -1e9
    recorder.step()
    assert recorder.rec is None and no_banner_window.started[-1].returncode == -15


def test_a_banner_that_cannot_start_is_not_retried_every_second(recorder, no_banner_window):
    recorder.step()
    no_banner_window.started[0].returncode = 1
    recorder.step()
    recorder.step()
    assert len(no_banner_window.started) == 1


def test_an_unconfirmed_call_is_held_not_processed(recorder, cfg):
    """A recording without consent.json (made by an older version) is not processed."""
    recorder.app = recorder.apps[0]
    recorder.rec = Rec(cfg, recorder.sessions / "2026-11-08_100000", recorder.app, "Zoom", "Sync")
    d = recorder.rec.dir
    recorder.stop_recording()
    assert status(d) == "needs_consent" and recorder.queue == []
    assert "confirm" in recorder.told[-1] and "deleted in 24 hours" in recorder.told[-1]
    ms.confirm_consent(recorder.cfg, d)
    recorder.probe.mic = (False, False)  # the call is over
    recorder.step()  # the app's flag wakes the recorder up
    assert status(d) == "recorded" and recorder.queue == [d]


def test_an_agreed_call_goes_straight_to_notes(recorder, cfg):
    recorder.tick()
    ms.agree_to_record(cfg)
    recorder.tick()
    d = recorder.rec.dir
    recorder.stop_recording()
    assert status(d) == "recorded" and recorder.queue == [d]


def test_a_crash_mid_call_does_not_skip_consent(recorder, cfg):
    held = session(cfg, "2026-11-08_090000", status="recording", ended_at=None)
    told = session(cfg, "2026-11-08_100000", status="recording", ended_at=None)
    ms.confirm_consent(cfg, told)
    recorder.recover()
    assert status(held) == "needs_consent" and status(told) == "recorded" and recorder.queue == [told]


def test_unconfirmed_recordings_are_deleted_after_a_day(cfg):
    now = dt.datetime(2026, 11, 9, 11, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    old = session(cfg, "2026-11-08_090000")
    fresh = session(cfg, "2026-11-08_100000", ended_at="2026-11-09T09:00:00+05:30")
    kept = session(cfg, "2026-11-08_080000")
    ms.confirm_consent(cfg, kept)
    ms.sweep_unconfirmed(cfg, now)
    assert not old.exists() and fresh.exists() and kept.exists()
    entries, problem = ms.check_consent_log(cfg)
    assert problem is None and entries[-1]["event"] == "deleted_unconfirmed" and entries[-1]["meeting"] == old.name


def test_deleting_a_meeting_keeps_its_lines_in_the_record(cfg):
    d = session(cfg)
    ms.confirm_consent(cfg, d)
    ms.delete_session(cfg, d)
    entries, _ = ms.check_consent_log(cfg)
    assert not d.exists() and [e["event"] for e in entries] == ["told_everyone", "meeting_deleted"]


# -- saved voices


@pytest.fixture
def voices(cfg, monkeypatch):
    class Encoder:
        def embed(self, windows):
            return np.tile(np.eye(8)[0], (len(windows), 1))
    monkeypatch.setattr(ms, "VoiceEncoder", lambda _cfg: Encoder())
    d = cfg["work_dir"] / "sessions" / "2026-11-08_100000"
    (d / "voices").mkdir(parents=True)
    ms.write_wav(d / "voices" / "Speaker 1.wav", np.ones(16000 * 5, np.float32) * 0.1)
    sp = {"Speaker 1": {"centroid": [1.0] + [0.0] * 7, "seconds": 60, "known": False, "first": [3.0, "hi"], "sample": "Speaker 1.wav"}}
    (d / "speakers.json").write_text(json.dumps(sp))
    (d / "transcript.json").write_text(json.dumps([{"start": 3.0, "end": 4.0, "speaker": "Speaker 1", "text": "hi"}]))
    (d / "session.json").write_text(json.dumps({"title": "Sync", "source": "Zoom", "status": "done",
                                                "started_at": "2026-11-08T10:00:00+05:30", "duration_s": 600}))
    return d


def test_a_voice_is_not_saved_without_agreement(cfg, voices):
    msg = ms.name_speaker(cfg, voices, "Speaker 1", "Priya")
    assert "didn't confirm they agreed" in msg and ms.VoiceLibrary(cfg).voices == {}
    assert "Priya" in json.loads((voices / "speakers.json").read_text())
    assert ms.check_consent_log(cfg)[0] == []


def test_a_voice_saved_with_agreement_is_recorded(cfg, voices):
    assert "Their voice is saved" in ms.name_speaker(cfg, voices, "Speaker 1", "Priya", agreed=True)
    v = ms.VoiceLibrary(cfg).voices["Priya"]
    e = ms.check_consent_log(cfg)[0][-1]
    assert e["event"] == "voice_agreed" and e["person"] == "Priya" and v["agreed_at"] == e["time"]


def test_a_voice_without_agreement_is_never_matched(cfg):
    lib = ms.VoiceLibrary(cfg)
    clip = np.ones(16000 * 4, np.float32) * 0.1
    lib.add("Old", clip, np.eye(8)[0], None)
    lib.add("New", clip, np.eye(8)[1], "2026-11-01T09:00:00")
    assert [n for n, _ in lib.ranked(np.eye(8)[0])] == ["New"]
    assert lib.agreed("Old") and [n for n, _ in ms.VoiceLibrary(cfg).ranked(np.eye(8)[0])][0] == "Old"
    assert lib.agreed("Nobody") is None
    assert ms.check_consent_log(cfg)[0][-1]["event"] == "voice_agreed"


def test_voices_not_heard_for_three_years_are_deleted(cfg):
    lib = ms.VoiceLibrary(cfg)
    clip = np.ones(16000 * 4, np.float32) * 0.1
    lib.add("Gone", clip, np.eye(8)[0], "2022-01-01T09:00:00")
    lib.add("Still", clip, np.eye(8)[1], "2022-01-01T09:00:00")
    lib.add("Recent", clip, np.eye(8)[2], "2026-01-01T09:00:00")
    for v in lib.voices.values():
        v["saved_at"] = "2022-01-01T09:00:00"
    lib.voices["Recent"]["saved_at"] = "2026-01-01T09:00:00"
    lib.save()
    ms.VoiceLibrary(cfg).heard(["Still"])
    ms.sweep_old_voices(cfg, dt.datetime(2026, 11, 17))
    assert sorted(ms.VoiceLibrary(cfg).voices) == ["Recent", "Still"]
    e = ms.check_consent_log(cfg)[0][-1]
    assert e["event"] == "voice_deleted" and e["person"] == "Gone"


# -- the app


@pytest.fixture
def api(cfg, tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(f'work_dir = {json.dumps(str(cfg["work_dir"]))}\noutput_dir = {json.dumps(str(cfg["output_dir"]))}\nnotify = false\n',
                 encoding="utf-8")
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(p))
    return app.Api()


def test_app_confirms_and_shows_the_record(api, cfg):
    d = session(cfg)
    assert api.meetings()[0]["status"] == "needs_consent"
    m = api.meeting(d.name)
    assert m["consent"] is None and m["consent_wait_h"] == 24
    r = api.confirm_consent(d.name)
    assert r["user"] and api.meeting(d.name)["consent"]["time"] == r["time"]
    rec = api.consent_record()
    assert rec["count"] == 1 and rec["problem"] is None and rec["entries"][0]["meeting"] == d.name
    with pytest.raises(ValueError):
        api.confirm_consent("../x")


def test_app_passes_agreement_only_when_ticked(api, cfg, monkeypatch):
    session(cfg)
    sent = []
    monkeypatch.setattr(app, "run_command", lambda *a, **k: (sent.append(a), (True, "ok"))[1])
    api.name_voice("2026-11-08_100000", "Speaker 1", "Priya", True)
    api.name_voice("2026-11-08_100000", "Speaker 1", "Priya", False)
    api.name_voice("2026-11-08_100000", "Speaker 1", "Priya", "yes")
    assert ["--agreed" in a for a in sent] == [True, False, False]


def test_app_people_show_agreement_and_forgetting_is_recorded(api, cfg):
    lib = ms.VoiceLibrary(cfg)
    lib.add("Old", np.ones(16000 * 4, np.float32) * 0.1, np.eye(8)[0], None)
    lib.save()
    assert api.people()[0]["agreed_at"] is None
    assert api.person_agreed("Old") and api.people()[0]["agreed_at"]
    assert api.forget_person("Old") is True and api.forget_person("Old") is False
    assert [e["event"] for e in ms.check_consent_log(cfg)[0]] == ["voice_agreed", "voice_deleted"]


# -- the banner


def test_banner_asks_then_shows_the_recording(recorder, cfg):
    import banner
    b = banner.Api(cfg)
    assert b.state() == {"state": "off"}  # no recorder yet
    recorder.tick()
    s = b.state()
    assert (s["state"], s["service"], s["title"]) == ("asking", "Zoom", "Sync") and s["notice"] == cfg["consent_notice"]
    assert b.agree() is True and b.agree() is False  # a second click has nothing left to answer
    recorder.tick()
    s = b.state()
    assert s["state"] == "recording" and s["agreed_at"] == ms.check_consent_log(cfg)[0][0]["time"]


def test_banner_can_decline(recorder, cfg):
    import banner
    recorder.tick()
    assert banner.Api(cfg).decline() is True
    assert banner.Api(cfg).agree() is False  # answered already
    recorder.tick()
    assert recorder.rec is None and banner.Api(cfg).state()["state"] == "watching"


def test_banner_refuses_to_close_and_has_no_close_button():
    import banner
    assert banner.refuse() is False
    page = banner.PAGE.lower()
    assert "close" not in page and "hide" not in page and "minimi" not in page


def test_app_answers_the_question_too(api, cfg, monkeypatch):
    monkeypatch.setattr(ms.plat, "AudioProbe", Probe)
    monkeypatch.setattr(ms, "Recording", Rec)
    monkeypatch.setattr(ms, "meeting_window", lambda cfg, app, titles=None: ("Zoom", "Sync"))
    monkeypatch.setattr(ms, "notify", lambda cfg, text: None)
    d = ms.Daemon({**api.cfg, "start_after_s": 0})
    d.pump_queue = lambda: None
    assert api.agree_to_record() is False and api.decline_recording() is False
    d.tick()
    assert api.status()["state"] == "asking"
    assert api.agree_to_record() is True
    d.tick()
    assert api.status()["state"] == "recording" and api.status()["consented"] is True
