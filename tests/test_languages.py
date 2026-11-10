"""Meetings in more than one language: nothing said may be lost, and no script may stop a note.

    python -m pytest tests
"""

import ast
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import meeting_scribe as ms  # noqa: E402

SR = ms.TARGET_SR
CFG = {"language": "", "drop_suspect_lines": False}


def speech(*parts):
    """Audio whose level says the language: parts are (language, seconds)."""
    level = {"en": 0.1, "hi": 0.2, "de": 0.3, "??": 0.4}
    return np.concatenate([np.full(int(s * SR), level[lang], np.float32) for lang, s in parts])


@pytest.fixture
def heard(monkeypatch):
    """A stand-in model that recognizes the language, by majority, and counts its work."""
    calls = []

    def detect(cfg, audio):
        calls.append(len(audio) / SR)
        values, counts = np.unique(np.round(audio, 1), return_counts=True)
        top = round(float(values[np.argmax(counts)]), 1)
        if top == 0.4:
            return {"en": 0.3, "hi": 0.3}
        return {{0.1: "en", 0.2: "hi", 0.3: "de"}[top]: 0.95, "xx": 0.05}
    monkeypatch.setattr(ms, "detect_language", detect)
    return calls


def whole(audio):
    return [0.0, len(audio) / SR]


def test_one_language_is_one_pass(heard):
    a = speech(("en", 120))
    blocks = ms.language_blocks(CFG, a, whole(a))
    assert blocks == [("en", [0.0, 120.0])] and len(heard) == 8  # no closer look without a change


def test_a_switch_in_the_middle_is_found_near_where_it_happened(heard):
    a = speech(("en", 40), ("hi", 40), ("en", 40))
    blocks = ms.language_blocks(CFG, a, whole(a))
    assert [b[0] for b in blocks] == ["en", "hi", "en"]
    assert abs(blocks[0][1][-1] - 40) <= 5 and abs(blocks[1][1][-1] - 80) <= 5
    assert all(len(b[1]) == 2 for b in blocks)  # no needless cuts inside a stretch, which would lose words


def test_every_second_of_speech_is_still_covered(heard):
    a = speech(("en", 22), ("hi", 31), ("de", 19))
    clips = [0.0, 30.0, 30.5, 72.0]
    blocks = ms.language_blocks(CFG, a, clips)
    assert abs(sum(ms.spoken_seconds(b[1]) for b in blocks) - ms.spoken_seconds(clips)) < 0.01
    spans = [t for b in blocks for t in b[1]]
    assert spans == sorted(spans)


def test_a_brief_stray_guess_is_not_a_change(heard, monkeypatch):
    a = speech(("en", 40), ("hi", 3), ("en", 40))
    blocks = ms.language_blocks(CFG, a, whole(a))
    assert len(blocks) == 1 and blocks[0][0] == "en"


def test_unsure_windows_take_a_neighbours_language(heard):
    a = speech(("??", 15), ("hi", 30))
    assert [b[0] for b in ms.language_blocks(CFG, a, whole(a))] == ["hi"]


def test_nothing_recognizable_leaves_it_to_whisper(heard):
    a = speech(("??", 30))
    assert ms.language_blocks(CFG, a, whole(a)) == [(None, [0.0, 30.0])]


def test_hindi_heard_as_urdu_stays_hindi():
    assert ms.pick_language({"ur": 0.55, "hi": 0.4}) == ("hi", pytest.approx(0.95))
    assert ms.pick_language({"ur": 0.9, "hi": 0.05})[0] == "ur"


@pytest.fixture
def passes(monkeypatch):
    done = []

    def whisper(cfg, audio, **kw):
        done.append(kw)
        s, e = kw["clip_timestamps"][0], kw["clip_timestamps"][-1]
        return {"segments": [{"start": s, "end": e, "text": kw.get("language", "auto")}]}
    monkeypatch.setattr(ms, "whisper_transcribe", whisper)
    monkeypatch.setattr(ms, "speech_regions", lambda a: [0.0, len(a) / SR])
    return done


def test_each_stretch_is_transcribed_in_its_own_language(heard, passes):
    a = speech(("hi", 40), ("en", 40))
    assert [s["text"] for s in ms.transcribe_track(CFG, a, "Me")] == ["hi", "en"]


def test_a_chosen_language_is_used_alone(passes, monkeypatch):
    monkeypatch.setattr(ms, "detect_language", lambda *a: pytest.fail("checked anyway"))
    assert [s["text"] for s in ms.transcribe_track({**CFG, "language": "hi"}, speech(("en", 30)), "Me")] == ["hi"]


def test_a_failed_check_still_transcribes(passes, monkeypatch):
    monkeypatch.setattr(ms, "detect_language", lambda *a: 1 / 0)
    assert [s["text"] for s in ms.transcribe_track(CFG, speech(("en", 30)), "Me")] == ["auto"]


class Answers:
    parallel = False

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def chat(self, user, max_tokens=1500):
        self.asked.append(user)
        return self.answers.pop(0)


def test_the_small_talk_filter_leaves_other_scripts_alone():
    segs = [{"start": 1, "speaker": "A", "text": "ग्राहक को ईमेल भेजना है"}, {"start": 2, "speaker": "A", "text": "Nice weather today"}]
    llm = Answers("OFF: 1, 2", "D")
    kept, dropped = ms.work_only({"work_filter_chunk_chars": 8000}, llm, segs)
    assert [s["start"] for s in kept] == [1] and dropped == 1 and len(llm.asked) == 2


def test_notes_are_asked_for_in_english_with_fixed_headings():
    cfg = {"my_name": "Me", "others_name": "Others", "llm_chunk_chars": 32000}
    llm = Answers("## Summary\n- a\n\n## Action Items\n- None identified.")
    ms.summarize(cfg, "[00:01] Me: नमस्ते", llm)
    assert "Write the notes in English" in llm.asked[0] and "keep every heading exactly" in llm.asked[0]


def test_a_translated_answer_becomes_a_basic_note_rather_than_an_empty_one():
    cfg = {"my_name": "Me", "others_name": "Others", "llm_chunk_chars": 32000}
    with pytest.raises(ValueError):
        ms.summarize(cfg, "x", Answers("## सारांश\n- कुछ"))


MODULES = [*ROOT.glob("*.py"), *(ROOT / "platforms").glob("*.py")]


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_text_file_is_read_and_written_as_utf8(path):
    """Windows' default code page has no Hindi in it: one bare write_text and the meeting is lost."""
    bare = []
    for n in ast.walk(ast.parse(path.read_bytes())):
        if not isinstance(n, ast.Call) or any(k.arg == "encoding" for k in n.keywords):
            continue
        if isinstance(n.func, ast.Attribute) and n.func.attr in ("read_text", "write_text") and getattr(n.func.value, "id", "") != "plat":
            bare.append(n.lineno)
        if isinstance(n.func, ast.Name) and n.func.id == "open":
            mode = n.args[1] if len(n.args) > 1 else ast.Constant("r")
            if isinstance(mode, ast.Constant) and "b" not in mode.value:
                bare.append(n.lineno)
    assert not bare, f"{path.name}: lines {bare}"


def test_hindi_survives_saving_a_note(tmp_path):
    cfg = {**ms.DEFAULTS, "output_dir": tmp_path, "work_dir": tmp_path, "my_name": "Me", "llm_backend": "ollama", "jira": {}}
    meta = {"id": "s1", "title": "हिंदी बैठक", "started_at": "2026-11-08T10:00:00+00:00", "duration_s": 60}
    llm = Answers("## Summary\n- a\n\n## Action Items\n- None identified.")
    out = ms.write_note(cfg, meta, [{"start": 1.0, "speaker": "Me", "text": "नमस्ते दोस्तों"}], {}, llm)
    assert "नमस्ते दोस्तों" in out.read_text(encoding="utf-8")
