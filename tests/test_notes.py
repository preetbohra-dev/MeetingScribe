"""The note and everything around it: talk time, the JSON files for AI tools, vocabulary,
search and questions, surviving a broken model, and warnings during a call.

    python -m pytest tests
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402


class FakeLLM:
    def __init__(self, answer="## Summary\n- Done.\n\n## Action Items\n- [ ] **Me**: ship it"):
        self.answer, self.prompts = answer, []

    def chat(self, user, max_tokens=1500):
        self.prompts.append(user)
        return self.answer


class BrokenLLM:
    def chat(self, user, max_tokens=1500):
        raise RuntimeError("model blew up")


@pytest.fixture
def cfg(tmp_path):
    c = {**ms.DEFAULTS, "work_dir": tmp_path / "work", "output_dir": tmp_path / "notes",
         "log_dir": tmp_path / "logs", "jira": dict(ms.JIRA_DEFAULTS), "notify": False}
    c["work_dir"].mkdir()
    c["output_dir"].mkdir()
    return c


def turn(speaker, text, start=0.0):
    return {"start": start, "end": start + 1, "speaker": speaker, "text": text}


META = {"title": "Sync", "source": "Zoom", "started_at": "2026-11-08T10:00:00+05:30", "duration_s": 600, "id": "s1"}
LONG = " ".join(["word"] * 60)


def stored(cfg, sid, title, turns):
    d = cfg["work_dir"] / "sessions" / sid
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(json.dumps(turns), encoding="utf-8")
    (d / "session.json").write_text(json.dumps({"status": "done", "title": title, "started_at": f"{sid[:10]}T10:00:00"}))
    return d


# -- talk time


def test_talk_shares():
    shares = ms.talk_shares([turn("Me", " ".join(["a"] * 30)), turn("Tom", " ".join(["b"] * 70))])
    assert shares == [("Tom", 70), ("Me", 30)]


def test_no_talk_time_with_too_little_speech():
    assert ms.talk_shares([turn("Me", "a b"), turn("Tom", "c d")]) == []


def test_no_talk_time_for_a_monologue():
    assert ms.talk_shares([turn("Me", LONG)]) == []


def test_note_has_a_talk_time_line(cfg):
    note = ms.write_note(cfg, dict(META), [turn("Me", LONG), turn("Tom", LONG)], {}, FakeLLM())
    assert "**Talk time:** Me 50%, Tom 50%" in note.read_text(encoding="utf-8")


# -- files for AI tools


def test_sidecar_and_index(cfg):
    note = ms.write_note(cfg, dict(META), [turn("Me", LONG)], {}, FakeLLM())
    side = json.loads(note.with_suffix(".json").read_text(encoding="utf-8"))
    assert side["title"] == "Sync" and side["duration_min"] == 10 and side["note"] == note.name
    assert side["summary"] == ["Done."] and side["action_items"] == ["**Me**: ship it"]
    assert side["transcript"][0]["speaker"] == "Me"
    line = json.loads((cfg["output_dir"] / "index.jsonl").read_text(encoding="utf-8"))
    assert line["id"] == "s1" and "transcript" not in line


def test_index_has_one_line_per_meeting(cfg):
    ms.write_note(cfg, dict(META), [turn("Me", LONG)], {}, FakeLLM())
    ms.write_note(cfg, dict(META), [turn("Me", LONG)], {}, FakeLLM())
    ms.write_note(cfg, {**META, "id": "s2", "title": "Other"}, [turn("Me", LONG)], {}, FakeLLM())
    lines = (cfg["output_dir"] / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["s1", "s2"]


def test_non_ascii_title_survives(cfg):
    note = ms.write_note(cfg, {**META, "title": "बैठक — Müller"}, [turn("प्रिया", LONG)], {}, FakeLLM())
    assert json.loads(note.with_suffix(".json").read_text(encoding="utf-8"))["title"] == "बैठक — Müller"


# -- transcript clean-up


@pytest.mark.parametrize("text,want", [("Hey Siri.", True), ("Alexa", True), ("OK Google", True), ("okay, hey siri", True),
                                       ("We integrated Siri last week.", False), ("Google", False),
                                       ("Google is a customer", False), ("", False)])
def test_wake_noise(text, want):
    assert ms.is_wake_noise(text) is want


def test_vocabulary(cfg):
    cfg["vocabulary"] = {"app sumo": "AppSumo", "kube": "Kubernetes"}
    assert ms.fix_vocabulary(cfg, "The App Sumo deal, kube and kubeflow.") == "The AppSumo deal, Kubernetes and kubeflow."


def test_vocabulary_replacement_is_literal(cfg):
    cfg["vocabulary"] = {"a.b": r"C:\new\1"}
    assert ms.fix_vocabulary(cfg, "a.b axb") == r"C:\new\1 axb"


def test_clean_lines(cfg):
    cfg["vocabulary"] = {"pria": "Priya"}
    out = ms.clean_lines(cfg, [turn("Me", "Hey Siri"), turn("Me", "thanks pria")])
    assert [s["text"] for s in out] == ["thanks Priya"]


# -- search and ask


def two_meetings(cfg):
    stored(cfg, "2026-11-01_100000", "Budget review", [turn("Me", "The firewall change is blocked."),
                                                       turn("Priya", "I'll raise it with security.", 5)])
    stored(cfg, "2026-11-05_100000", "Launch", [turn("Tom", "Launch moved to Friday.")])


def test_search_finds_all_words_across_a_meeting(cfg):
    two_meetings(cfg)
    r = ms.search_meetings(cfg, "firewall security")
    assert [m["title"] for m in r] == ["Budget review"]
    assert len(r[0]["hits"]) == 2


def test_search_is_case_blind_and_newest_first(cfg):
    two_meetings(cfg)
    stored(cfg, "2026-11-07_100000", "Launch again", [turn("Tom", "LAUNCH is fine.")])
    assert [m["title"] for m in ms.search_meetings(cfg, "launch")] == ["Launch again", "Launch"]


def test_search_finds_people_and_titles(cfg):
    two_meetings(cfg)
    assert [m["title"] for m in ms.search_meetings(cfg, "priya")] == ["Budget review"]
    assert [m["title"] for m in ms.search_meetings(cfg, "budget")] == ["Budget review"]


@pytest.mark.parametrize("q", ["", "   ", "zebra", "firewall zebra"])
def test_search_without_results(cfg, q):
    two_meetings(cfg)
    assert ms.search_meetings(cfg, q) == []


def test_search_ignores_unfinished_meetings(cfg):
    two_meetings(cfg)
    d = stored(cfg, "2026-11-08_100000", "Live", [turn("Tom", "firewall")])
    (d / "session.json").write_text(json.dumps({"status": "recording", "title": "Live", "started_at": "x"}))
    assert [m["title"] for m in ms.search_meetings(cfg, "firewall")] == ["Budget review"]


def test_ask_uses_only_relevant_meetings(cfg):
    two_meetings(cfg)
    llm = FakeLLM("Priya will raise it.")
    out = ms.ask_meetings(cfg, "Who is handling the firewall?", llm=llm)
    assert out["answer"] == "Priya will raise it."
    assert [s["title"] for s in out["sources"]] == ["Budget review"]
    assert "firewall change" in llm.prompts[0] and "Launch moved" not in llm.prompts[0]


def test_ask_one_meeting_reads_all_of_it(cfg):
    two_meetings(cfg)
    llm = FakeLLM("ok")
    ms.ask_meetings(cfg, "anything else?", "2026-11-01", llm=llm)
    assert "firewall change" in llm.prompts[0] and "raise it" in llm.prompts[0]


def test_ask_with_nothing_relevant_does_not_load_a_model(cfg, monkeypatch):
    two_meetings(cfg)
    monkeypatch.setattr(ms, "LLM", lambda *_: pytest.fail("model loaded for nothing"))
    assert ms.ask_meetings(cfg, "zebra migration?")["sources"] == []


def test_ask_fits_long_meetings_keeping_relevant_lines(cfg):
    cfg["llm_chunk_chars"] = 2000
    turns = [turn("Tom", "filler " * 20, i) for i in range(200)]
    turns[150] = turn("Priya", "the secret deadline is March", 150)
    stored(cfg, "2026-11-01_100000", "Long", turns)
    text, _ = ms.ask_material(cfg, "what is the secret deadline?", "2026-11-01")
    assert "secret deadline" in text and len(text) < 2600


# -- a broken model never costs the meeting


def test_failed_summary_writes_a_basic_note(cfg):
    turns = [turn("Priya", "I'll send the revised plan by Friday to everyone."), turn("Tom", "Sounds good.", 3)]
    text = ms.write_note(cfg, dict(META), turns, {}, BrokenLLM()).read_text(encoding="utf-8")
    assert "could not be written" in text
    assert "**Priya**" in text and "revised plan" in text
    assert "Sounds good." in text  # the transcript is whole


def test_basic_note_invents_nothing(cfg):
    text = ms.basic_notes(cfg, [turn("Tom", "Nice weather today.")])
    assert "- None identified." in text


def test_model_load_failure_writes_a_basic_note(cfg, monkeypatch):
    def boom(*_):
        raise OSError("no model file")
    monkeypatch.setattr(ms, "LLM", boom)
    text = ms.write_note(cfg, dict(META), [turn("Me", LONG)], {}).read_text(encoding="utf-8")
    assert "basic note" in text


# -- end to end with stand-ins for the slow models


class Encoder:
    def __init__(self, _cfg):
        pass

    def embed(self, windows):
        e = np.tile(np.eye(8)[1], (len(windows), 1))
        return e


def session(cfg, seconds=30):
    d = cfg["work_dir"] / "sessions" / "2026-11-08_090000"
    d.mkdir(parents=True)
    t = np.arange(16000 * seconds) / 16000
    tone = (0.3 * np.sin(2 * np.pi * 220 * t) * (t % 3 < 2) * 32767).astype(np.int16)  # speech-like bursts
    tone.tofile(d / "mic.pcm")
    tone.tofile(d / "system.pcm")
    (d / "session.json").write_text(json.dumps({"status": "recorded", "title": "Standup", "source": "Zoom", "app": "zoom",
                                                "started_at": "2026-11-08T09:00:00+05:30", "duration_s": seconds}))
    return d


def stub_whisper(monkeypatch, llm):
    lines = {"Me": "Good morning, thanks Priya for joining the call today everyone.",
             "Others": "Hi, this is Priya, the migration is finished and validated now."}

    def transcribe(cfg, audio, speaker):
        return [{"start": 2.0, "end": 12.0, "speaker": speaker, "text": lines[speaker], "flag": None,
                 "words": [(2.0 + i, 3.0 + i, " " + w) for i, w in enumerate(lines[speaker].split())]}]
    monkeypatch.setattr(ms, "transcribe_track", transcribe)
    monkeypatch.setattr(ms, "release_whisper", lambda: None)
    monkeypatch.setattr(ms, "VoiceEncoder", Encoder)
    monkeypatch.setattr(ms, "LLM", lambda *_: llm)


class ScriptedLLM(FakeLLM):
    def chat(self, user, max_tokens=1500):
        self.prompts.append(user)
        if "Find the lines that are NOT work" in user:
            return "OFF: none"
        if "Say which unnamed label" in user:
            return '[{"label": "Speaker 1", "name": "Priya", "confidence": 0.95, "quote": "this is Priya"}]'
        return self.answer


def test_whole_pipeline(cfg, monkeypatch):
    stub_whisper(monkeypatch, ScriptedLLM())
    d = session(cfg)
    ms.process_session(cfg, d)
    meta = json.loads((d / "session.json").read_text())
    assert meta["status"] == "done" and meta["id"] == d.name
    assert not (d / "mic.pcm").exists()  # raw audio goes once the note is saved
    text = Path(meta["notes"]).read_text(encoding="utf-8")
    assert "Speaker 1 (maybe Priya?)" in text  # said aloud, so asked, never asserted
    sp = json.loads((d / "speakers.json").read_text())
    assert sp["Speaker 1"]["suggest"]["why"] == ["said"]
    assert Path(meta["notes"]).with_suffix(".json").exists()


def test_pipeline_survives_a_model_that_will_not_load(cfg, monkeypatch):
    stub_whisper(monkeypatch, None)

    def boom(*_):
        raise OSError("model missing")
    monkeypatch.setattr(ms, "LLM", boom)
    d = session(cfg)
    ms.process_session(cfg, d)
    meta = json.loads((d / "session.json").read_text())
    assert meta["status"] == "done"
    assert "basic note" in Path(meta["notes"]).read_text(encoding="utf-8")


def test_pipeline_survives_a_broken_voice_model(cfg, monkeypatch):
    stub_whisper(monkeypatch, ScriptedLLM())

    def boom(_cfg):
        raise OSError("voice model missing")
    monkeypatch.setattr(ms, "VoiceEncoder", boom)
    d = session(cfg)
    ms.process_session(cfg, d)
    meta = json.loads((d / "session.json").read_text())
    assert "Others:**" in Path(meta["notes"]).read_text(encoding="utf-8")


def test_pipeline_uses_what_the_screen_showed(cfg, monkeypatch):
    stub_whisper(monkeypatch, FakeLLMNoNames())
    d = session(cfg)
    (d / "screen.json").write_text(json.dumps([{"t": 4, "active": "Priya Shah", "roster": ["Priya Shah"]},
                                               {"t": 11.5, "active": "Priya Shah", "roster": ["Priya Shah"]},
                                               {"t": 11.8, "active": None, "roster": ["Priya Shah"]}]), encoding="utf-8")
    ms.process_session(cfg, d)
    sp = json.loads((d / "speakers.json").read_text())
    assert list(sp) == ["Speaker 1"]  # one look apart by < 10 s: no vote


class FakeLLMNoNames(ScriptedLLM):
    def chat(self, user, max_tokens=1500):
        if "Say which unnamed label" in user:
            return "[]"
        return super().chat(user, max_tokens)


# -- during a call


class Writer:
    def __init__(self, last_sound=None):
        self.samples, self.peak, self.last_sound, self.last_write = 1, 0.5, last_sound, None


class Stream:
    stopped = False
    mode = "app"
    stream = True
    last_cb = None


def recording(cfg, monkeypatch, quiet_for):
    told = []
    monkeypatch.setattr(ms, "notify", lambda _c, text: told.append(text))
    r = ms.Recording.__new__(ms.Recording)
    now = time.monotonic()
    r.cfg, r.t0, r.warned, r.warnings = cfg, now - 300, set(), []
    r.mic_w, r.sys_w = Writer(now - quiet_for), Writer(now)
    r.mic, r.sys = Stream(), Stream()
    r.mic.last_cb = now
    r.looks, r.looking, r.last_look = 0, False, None
    r.app, r.service = None, "Zoom"
    return r, told


def test_silent_mic_warns_once(cfg, monkeypatch):
    r, told = recording(cfg, monkeypatch, 60)
    r.watchdog(True, False)
    r.watchdog(True, False)
    assert len(told) == 1 and "microphone is silent" in told[0]
    assert len(r.warnings) == 1


def test_warning_clears_when_sound_returns(cfg, monkeypatch):
    r, _ = recording(cfg, monkeypatch, 60)
    r.watchdog(True, False)
    r.mic_w.last_sound = time.monotonic()
    r.watchdog(True, False)
    assert r.warnings == []


def test_working_mic_does_not_warn(cfg, monkeypatch):
    r, told = recording(cfg, monkeypatch, 2)
    r.watchdog(True, False)
    assert told == [] and r.warnings == []


def test_track_writer_tracks_sound(tmp_path):
    w = ms.TrackWriter(tmp_path / "x.pcm", time.monotonic())
    w.write(np.zeros(1600, np.float32), 16000, time.monotonic())
    assert w.last_sound is None
    w.write(np.ones(1600, np.float32) * 0.1, 16000, time.monotonic())
    assert w.last_sound is not None
    w.close()


def test_screen_is_not_read_unless_turned_on(cfg, monkeypatch):
    r, _ = recording(cfg, monkeypatch, 2)
    monkeypatch.setattr(ms.threading, "Thread", lambda **_: pytest.fail("looked at the screen"))
    r.maybe_look_at_screen(time.monotonic())
    assert r.looks == 0


def test_screen_looks_are_rationed(cfg, monkeypatch):
    cfg["screen_names"] = True
    r, _ = recording(cfg, monkeypatch, 2)
    started = []

    class T:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            r.looking = False
    monkeypatch.setattr(ms.threading, "Thread", T)
    now = time.monotonic()
    r.maybe_look_at_screen(now)
    r.maybe_look_at_screen(now + 1)  # too soon
    assert len(started) == 1
    r.looks = cfg["screen_max"]
    r.maybe_look_at_screen(now + 100)  # used up
    assert len(started) == 1


def test_the_picture_is_not_kept(cfg, monkeypatch, tmp_path):
    r, _ = recording(cfg, monkeypatch, 2)
    r.dir, r.app, r.looking = tmp_path / "rec", {"id": "zoom"}, True
    r.dir.mkdir()
    monkeypatch.setattr(ms.plat, "window_image", lambda *_: np.zeros((300, 400, 3), np.uint8), raising=False)
    monkeypatch.setattr(ms.plat, "read_text", lambda _i: [{"text": "Priya", "x": 1, "y": 1, "w": 9, "h": 9}], raising=False)
    r._look(12.5)
    assert sorted(p.name for p in r.dir.iterdir()) == ["screen.json"]
    assert json.loads((r.dir / "screen.json").read_text(encoding="utf-8")) == [{"t": 12.5, "active": None, "roster": ["Priya"]}]
    assert r.looking is False


def test_a_failed_look_is_harmless(cfg, monkeypatch, tmp_path):
    r, _ = recording(cfg, monkeypatch, 2)
    r.dir, r.looking = tmp_path / "rec", True
    r.dir.mkdir()

    def boom(*_):
        raise OSError("no window")
    monkeypatch.setattr(ms.plat, "window_image", boom, raising=False)
    r._look(1.0)
    assert r.looking is False and list(r.dir.iterdir()) == []


# -- packaged or checkout


def test_config_location_in_a_checkout(monkeypatch):
    monkeypatch.delenv("MEETINGSCRIBE_CONFIG", raising=False)
    assert ms.config_path() == ms.HERE / "config.toml"


def test_config_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(tmp_path / "c.toml"))
    (tmp_path / "c.toml").write_text('my_name = "Asha"\n')
    assert ms.load_config()["my_name"] == "Asha"


def test_packaged_app_starts_from_a_copy_of_the_shipped_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("MEETINGSCRIBE_CONFIG", raising=False)
    monkeypatch.setattr(ms, "FROZEN", True)
    monkeypatch.setattr(ms.plat, "DIRS", {"work_dir": str(tmp_path / "w"), "log_dir": str(tmp_path / "l")})
    path = ms.config_path()
    assert path == tmp_path / "w" / "config.toml" and path.exists()


def test_worker_command(monkeypatch):
    assert ms.self_command("process", "x")[1].endswith("meeting_scribe.py")
    monkeypatch.setattr(ms, "FROZEN", True)
    assert ms.self_command("process", "x")[1:] == ["process", "x"]


def test_terms_are_recorded(cfg):
    assert not ms.terms_accepted(cfg)
    ms.record_terms(cfg)
    assert ms.terms_accepted(cfg)


# -- Ollama: use what is already loaded


class Answer:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def ollama_cfg(cfg, **kw):
    return {**cfg, "llm_backend": "ollama", "ollama_model": "running", "ollama_num_ctx": 8192, **kw}


def loaded(monkeypatch, *models):
    calls = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(req)
        if url.endswith("/api/ps"):
            return Answer({"models": list(models)})
        return Answer({"message": {"content": "hello"}})
    monkeypatch.setattr(ms.urllib.request, "urlopen", urlopen)
    return calls


def test_running_model_is_used(cfg, monkeypatch):
    calls = loaded(monkeypatch, {"name": "gemma3:4b", "context_length": 32768})
    llm = ms.LLM(ollama_cfg(cfg))
    assert llm.chat("hi") == "hello"
    body = json.loads(calls[-1].data)
    assert body["model"] == "gemma3:4b" and body["options"]["num_ctx"] == 32768  # no reload


def test_short_context_is_raised(cfg, monkeypatch):
    loaded(monkeypatch, {"name": "gemma3:4b", "context_length": 4096})
    assert ms.LLM(ollama_cfg(cfg)).num_ctx == 8192


def test_image_models_are_skipped(cfg, monkeypatch):
    loaded(monkeypatch, {"name": "llama3.2-vision:latest"}, {"name": "moondream:latest"}, {"name": "llama3.1:latest"})
    assert ms.LLM(ollama_cfg(cfg)).ollama_model == "llama3.1:latest"


def test_fallback_when_nothing_is_loaded(cfg, monkeypatch):
    loaded(monkeypatch)
    assert ms.LLM(ollama_cfg(cfg, ollama_fallback_model="llama3.2:latest")).ollama_model == "llama3.2:latest"


def test_nothing_loaded_and_no_fallback_gives_a_basic_note(cfg, monkeypatch):
    loaded(monkeypatch)
    text = ms.write_note(ollama_cfg(cfg), dict(META), [turn("Me", LONG)], {}).read_text(encoding="utf-8")
    assert "basic note" in text


def test_named_model_still_works(cfg, monkeypatch):
    calls = loaded(monkeypatch)
    llm = ms.LLM(ollama_cfg(cfg, ollama_model="qwen2.5:14b", ollama_num_ctx=16384))
    assert llm.ollama_model == "qwen2.5:14b" and calls == []


def test_running_mode_stays_on_this_computer(cfg, monkeypatch):
    monkeypatch.setattr(ms.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("network used"))
    with pytest.raises(ValueError):
        ms.LLM(ollama_cfg(cfg, ollama_url="https://example.com"))


def test_local_settings_win(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text('my_name = "Asha"\nnotify = false\n')
    (tmp_path / "config.local.toml").write_text('my_name = "Local"\n')
    c = ms.load_config()
    assert c["my_name"] == "Local" and c["notify"] is False


# -- naming a call that has no name of its own


class Scripted:
    def __init__(self, *answers):
        self.answers = list(answers)

    def chat(self, user, max_tokens=1500):
        return self.answers.pop(0)


NOTE = "## Summary\n- We planned the move.\n\n## Action Items\n- None identified."
SAM = {"Sam Lee": {"known": True, "seconds": 60}}
UNTITLED = {**META, "title": "Microsoft Teams meeting", "source": "Microsoft Teams"}


@pytest.mark.parametrize("answer, want", [
    ("Firewall migration plan", "Firewall migration plan"),
    ('"Firewall migration plan."', "Firewall migration plan"),
    ("Title: **Firewall migration plan**", "Firewall migration plan"),
    ("# Firewall migration plan\n\nThis title fits.", "Firewall migration plan"),
    ("Sorry, I can only help if you give me some more of the conversation to work from today", ""),
    ("Migration", ""),
    ("", ""),
])
def test_only_a_real_title_is_taken_from_the_model(answer, want):
    assert ms.topic_from_answer(answer) == want


def test_an_unnamed_call_gets_its_topic_and_people(cfg):
    meta = dict(UNTITLED)
    note = ms.write_note(cfg, meta, [turn("Me", LONG), turn("Sam Lee", LONG)], SAM, Scripted(NOTE, "Firewall migration plan"))
    assert meta["title"] == "Firewall migration plan with Sam Lee" and "Firewall migration plan with Sam Lee" in note.stem
    assert note.read_text(encoding="utf-8").startswith("# Firewall migration plan with Sam Lee\n")


def test_a_guessed_name_is_not_put_in_the_title(cfg):
    meta = dict(UNTITLED)
    guess = {"Speaker 1": {"known": False, "seconds": 60, "suggest": {"name": "Sam Lee"}}}
    ms.write_note(cfg, meta, [turn("Me", LONG)], guess, Scripted(NOTE, "Firewall migration plan"))
    assert meta["title"] == "Firewall migration plan"


@pytest.mark.parametrize("names, want", [
    (["Sam Lee"], "Sam Lee"),
    (["Sam Lee", "Priya Shah"], "Sam Lee and Priya Shah"),
    (["Sam Lee", "Priya Shah", "Tom Ross"], "Sam Lee, Priya Shah and 1 other"),
])
def test_people_in_a_title(cfg, names, want):
    assert ms.people_on_call(cfg, {n: {"known": True} for n in names}) == want


def test_unrecognized_people_are_counted(cfg):
    speakers = {"Sam Lee": {"known": True}, "Speaker 1": {"known": False}, "Speaker 2": {"known": False}}
    assert ms.people_on_call(cfg, speakers) == "Sam Lee and 2 others"


def test_a_model_that_cannot_name_it_still_leaves_the_people(cfg):
    meta = dict(UNTITLED)
    ms.write_note(cfg, meta, [turn("Me", LONG)], SAM, Scripted(NOTE, "x"))
    assert meta["title"] == "Call with Sam Lee"


def test_a_failed_name_never_costs_the_note(cfg):
    meta = dict(UNTITLED)
    note = ms.write_note(cfg, meta, [turn("Me", LONG)], {}, Scripted(NOTE))  # the second question raises
    assert meta["title"] == "Microsoft Teams meeting" and "We planned the move" in note.read_text(encoding="utf-8")


def test_a_broken_model_gives_a_basic_note_named_for_the_people(cfg):
    meta = dict(UNTITLED)
    ms.write_note(cfg, meta, [turn("Me", LONG)], SAM, BrokenLLM())
    assert meta["title"] == "Call with Sam Lee"


def test_a_meeting_with_its_own_name_keeps_it(cfg):
    llm = FakeLLM(NOTE)
    meta = dict(META)
    ms.write_note(cfg, meta, [turn("Me", LONG)], SAM, llm)
    assert meta["title"] == "Sync" and len(llm.prompts) == 1
