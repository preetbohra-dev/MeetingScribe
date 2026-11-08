"""Who is who: a name reaches a note only with strong proof, and everything else stays a
question for the user. Embeddings are made up here; tests/test_notes.py covers the rest.

    python -m pytest tests
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import screen_names as sn  # noqa: E402

DIM = 64


def unit(i):
    v = np.zeros(DIM)
    v[i] = 1.0
    return v


def blend(i, j, sim):
    """A unit vector whose cosine with unit(i) is sim, the rest along unit(j)."""
    return sim * unit(i) + np.sqrt(1 - sim ** 2) * unit(j)


class FakeEncoder:
    def embed(self, windows):
        return np.tile(unit(5), (len(windows), 1))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = {**ms.DEFAULTS, "work_dir": tmp_path / "work", "output_dir": tmp_path / "notes",
         "log_dir": tmp_path / "logs", "jira": dict(ms.JIRA_DEFAULTS), "notify": False}
    c["work_dir"].mkdir()
    c["output_dir"].mkdir()
    monkeypatch.setattr(ms, "VoiceEncoder", lambda _cfg: FakeEncoder())
    return c


def library(cfg, **people):
    """A saved-voice library; people maps a name to the index of its embedding axis."""
    lib = ms.VoiceLibrary(cfg)
    clip = np.ones(16000 * 4, np.float32) * 0.1
    for name, i in people.items():
        lib.add(name, clip, unit(i))
    lib.save()
    return ms.VoiceLibrary(cfg)


# -- deciding what a voice is called


def test_stranger_below_the_bar_is_not_named(cfg):
    lib = library(cfg, Priya=0)
    names, info = ms.identify(cfg, {0: blend(0, 9, 0.62)}, {0: 120}, lib, heard="thanks priya")
    assert names[0] == "Speaker 1"
    assert info["Speaker 1"]["known"] is False


def test_old_threshold_case_stays_unnamed(cfg):
    """0.51 was enough under the old threshold, and is how strangers got names."""
    lib = library(cfg, Priya=0)
    _, info = ms.identify(cfg, {0: blend(0, 9, 0.51)}, {0: 120}, lib, heard="priya")
    assert "Priya" not in info


def test_strong_match_with_attendance_is_named(cfg):
    lib = library(cfg, Priya=0)
    names, info = ms.identify(cfg, {0: blend(0, 9, 0.85)}, {0: 120}, lib, roster=["Priya Shah"])
    assert names[0] == "Priya" and info["Priya"]["known"] is True


def test_strong_match_without_attendance_is_only_a_suggestion(cfg):
    lib = library(cfg, Priya=0)
    names, info = ms.identify(cfg, {0: blend(0, 9, 0.9)}, {0: 120}, lib, roster=["Tom Berg"], heard="nothing here")
    assert names[0] == "Speaker 1"
    assert info["Speaker 1"]["suggest"]["name"] == "Priya"


def test_attendance_requirement_can_be_switched_off(cfg):
    cfg["speaker_require_attendance"] = False
    lib = library(cfg, Priya=0)
    names, _ = ms.identify(cfg, {0: blend(0, 9, 0.9)}, {0: 120}, lib)
    assert names[0] == "Priya"


def test_name_said_aloud_counts_as_attendance(cfg):
    lib = library(cfg, Priya=0)
    names, _ = ms.identify(cfg, {0: blend(0, 9, 0.9)}, {0: 120}, lib, heard="Thanks Priya, that helps")
    assert names[0] == "Priya"


def test_two_close_candidates_are_not_decided(cfg):
    lib = library(cfg, Priya=0, Tom=1)
    v = 0.74 * unit(0) + 0.67 * unit(1)
    v /= np.linalg.norm(v)
    names, info = ms.identify(cfg, {0: v}, {0: 120}, lib, heard="priya tom")
    assert names[0] == "Speaker 1"
    assert info["Speaker 1"]["suggest"]["name"] == "Priya"


def test_too_little_speech_is_not_decided(cfg):
    lib = library(cfg, Priya=0)
    names, _ = ms.identify(cfg, {0: blend(0, 9, 0.9)}, {0: 8}, lib, heard="priya")
    assert names[0] == "Speaker 1"


def test_two_clusters_of_one_person_become_one(cfg):
    lib = library(cfg, Priya=0)
    names, info = ms.identify(cfg, {0: blend(0, 9, 0.9), 1: blend(0, 8, 0.85)}, {0: 100, 1: 30}, lib, heard="priya")
    assert names == {0: "Priya", 1: "Priya"}
    assert len(info) == 1 and info["Priya"]["seconds"] == 130


def test_matching_never_changes_a_saved_voice(cfg):
    lib = library(cfg, Priya=0)
    before = json.dumps(lib.voices)
    ms.identify(cfg, {0: blend(0, 9, 0.95)}, {0: 120}, lib, heard="priya")
    assert json.dumps(ms.VoiceLibrary(cfg).voices) == before


def test_a_name_is_suggested_once(cfg):
    lib = library(cfg, Priya=0)
    _, info = ms.identify(cfg, {0: blend(0, 9, 0.6), 1: blend(0, 8, 0.58)}, {0: 100, 1: 90}, lib)
    assert [i.get("suggest", {}).get("name") for i in info.values()].count("Priya") == 1


# -- voices that are not people


def seg(speaker, text, start=0.0, end=1.0, flag=None):
    return {"start": start, "end": end, "speaker": speaker, "text": text, "flag": flag}


def test_a_voice_with_almost_no_words_is_not_a_person(cfg):
    sp = {"Speaker 1": {"centroid": unit(0).tolist(), "seconds": 90, "known": False},
          "Speaker 2": {"centroid": blend(0, 1, 0.8).tolist(), "seconds": 16, "known": False},
          "Speaker 3": {"centroid": unit(2).tolist(), "seconds": 60, "known": False}}
    lines = [seg("Speaker 1", "one two three four five six seven eight nine"), seg("Speaker 2", "yeah okay"),
             seg("Speaker 3", "this is also long enough to count as a real voice")]
    out, lines = ms.prune_voices(cfg, sp, lines)
    assert list(out) == ["Speaker 1", "Speaker 2"]  # renumbered, no gap
    assert [ln["speaker"] for ln in lines] == ["Speaker 1", "Speaker 1", "Speaker 2"]
    assert out["Speaker 1"]["seconds"] == 106


def test_one_voice_is_kept_even_when_quiet(cfg):
    sp = {"Speaker 1": {"centroid": unit(0).tolist(), "seconds": 3, "known": False}}
    out, _ = ms.prune_voices(cfg, sp, [seg("Speaker 1", "hello")])
    assert list(out) == ["Speaker 1"]


# -- names


@pytest.mark.parametrize("a,b,want", [("Anna", "Anna Jones", True), ("anna jones", "Anna Jones", True),
                                      ("Anna Smith", "Anna Jones", False), ("Tom", "Anna", False), ("", "Anna", False)])
def test_same_person(a, b, want):
    assert ms.same_person(a, b) is want


@pytest.mark.parametrize("text,want", [("Priya Shah", True), ("O'Brien", True), ("Jean-Luc Picard", True), ("प्रिया", True),
                                       ("Speaker 2", False), ("priya", False), ("Room 12", False), ("a b c d", False), ("", False)])
def test_looks_like_name(text, want):
    assert ms.looks_like_name(text) is want


def test_a_person_is_saved_once_under_the_fullest_name(cfg):
    lib = library(cfg, **{"Anna Jones": 0})
    assert lib.canonical("anna") == "Anna Jones"
    assert ms.VoiceLibrary(cfg).canonical("Tom") == "Tom"


def test_old_voiceprints_are_archived_not_used_or_deleted(cfg):
    (cfg["work_dir"] / "voices.json").write_text(json.dumps({"Priya": {"embedding": unit(0).tolist(), "n": 3}}))
    lib = ms.VoiceLibrary(cfg)
    assert lib.voices == {}
    assert not (cfg["work_dir"] / "voices.json").exists()
    assert len(list(cfg["work_dir"].glob("voices.archived-*.json"))) == 1


def test_forgetting_removes_the_audio_too(cfg):
    lib = library(cfg, Priya=0)
    wav = lib.dir / lib.voices["Priya"]["file"]
    assert wav.exists()
    assert lib.forget("Priya") is True
    assert not wav.exists()
    assert ms.VoiceLibrary(cfg).voices == {}
    assert lib.forget("Priya") is False


def test_clip_is_capped(cfg):
    lib = ms.VoiceLibrary(cfg)
    lib.add("Long", np.ones(16000 * 40, np.float32) * 0.1, unit(0))
    assert len(ms.read_wav(lib.dir / lib.voices["Long"]["file"])) == 16000 * ms.MAX_CLIP_S


def test_library_recomputes_embeddings_when_the_model_changes(cfg):
    lib = library(cfg, Priya=0)
    lib.voices["Priya"]["model"] = "something-older"
    lib.sync(FakeEncoder())
    assert lib.voices["Priya"]["model"] == cfg["voice_model"]
    assert np.allclose(lib.voices["Priya"]["embedding"], unit(5))


# -- naming and un-naming in a finished meeting


def meeting(cfg, voices=("Speaker 1", "Speaker 2"), clip_s=5):
    d = cfg["work_dir"] / "sessions" / "2026-11-08_100000"
    (d / "voices").mkdir(parents=True)
    sp, turns = {}, [{"start": 0.0, "end": 2.0, "speaker": cfg["my_name"], "text": "Morning everyone"}]
    for i, v in enumerate(voices):
        sp[v] = {"centroid": unit(i).tolist(), "seconds": 60, "known": False, "similarity": None,
                 "first": [3.0, "hello"], "sample": f"{v}.wav"}
        ms.write_wav(d / "voices" / f"{v}.wav", np.ones(16000 * clip_s, np.float32) * 0.1)
        turns.append({"start": 3.0 + i, "end": 4.0 + i, "speaker": v, "text": f"line from {v}"})
    meta = {"title": "Sync", "source": "Zoom", "started_at": "2026-11-08T10:00:00+05:30", "duration_s": 600,
            "status": "done", "id": d.name}
    (d / "transcript.json").write_text(json.dumps(turns), encoding="utf-8")
    (d / "speakers.json").write_text(json.dumps(sp))
    out = ms.write_note(cfg, meta, turns, sp, FakeLLM())
    meta["notes"] = str(out)
    (d / "session.json").write_text(json.dumps(meta))
    return d, out


class FakeLLM:
    def __init__(self, answer="## Summary\n- Speaker 1 reported progress.\n\n## Action Items\n- [ ] **Speaker 1**: send it"):
        self.answer, self.prompts = answer, []

    def chat(self, user, max_tokens=1500):
        self.prompts.append(user)
        return self.answer


def test_naming_updates_everything_and_saves_the_voice(cfg):
    d, note = meeting(cfg)
    msg = ms.name_speaker(cfg, d, "Speaker 1", "  Priya   Shah ")
    assert "saved" in msg
    text = note.read_text(encoding="utf-8")
    assert "**Participants:** Me, Speaker 2 (unrecognized), Priya Shah" in text
    assert "Speaker 1" not in text and "Priya Shah reported progress" in text
    assert [t["speaker"] for t in json.loads((d / "transcript.json").read_text())][1] == "Priya Shah"
    assert "Priya Shah" in ms.VoiceLibrary(cfg).voices
    side = json.loads(note.with_suffix(".json").read_text(encoding="utf-8"))
    assert "Priya Shah" in side["participants"] and side["unconfirmed_speakers"] == ["Speaker 2"]


def test_naming_the_last_voice_clears_the_table(cfg):
    d, note = meeting(cfg, voices=("Speaker 1",))
    assert ms.VOICES_BEGIN in note.read_text(encoding="utf-8")
    ms.name_speaker(cfg, d, "Speaker 1", "Tom")
    assert ms.VOICES_BEGIN not in note.read_text(encoding="utf-8")


def test_short_clip_names_the_meeting_but_saves_no_voice(cfg):
    d, _ = meeting(cfg, clip_s=1)
    assert "too little clean speech" in ms.name_speaker(cfg, d, "Speaker 1", "Tom")
    assert ms.VoiceLibrary(cfg).voices == {}


def test_same_person_given_twice_merges(cfg):
    d, note = meeting(cfg)
    ms.name_speaker(cfg, d, "Speaker 1", "Anna Jones")
    ms.name_speaker(cfg, d, "Speaker 2", "anna")
    sp = json.loads((d / "speakers.json").read_text())
    assert list(sp) == ["Anna Jones"] and sp["Anna Jones"]["seconds"] == 120
    assert "Speaker 2" not in note.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["", "   ", "Speaker 3", "Me"])
def test_bad_names_are_refused_and_change_nothing(cfg, bad):
    d, _ = meeting(cfg)
    with pytest.raises(ValueError):
        ms.name_speaker(cfg, d, "Speaker 1", bad)
    assert "Speaker 1" in json.loads((d / "speakers.json").read_text())


def test_unknown_label_is_refused(cfg):
    d, _ = meeting(cfg)
    with pytest.raises(ValueError):
        ms.name_speaker(cfg, d, "Speaker 9", "Tom")
    with pytest.raises(ValueError):
        ms.reject_name(cfg, d, "Speaker 9")


def test_rejecting_a_suggestion_keeps_it_away(cfg):
    d, note = meeting(cfg)
    sp = json.loads((d / "speakers.json").read_text())
    sp["Speaker 1"]["suggest"] = {"name": "Priya", "why": ["voice"]}
    (d / "speakers.json").write_text(json.dumps(sp))
    ms.reject_name(cfg, d, "Speaker 1")
    sp = json.loads((d / "speakers.json").read_text())
    assert "suggest" not in sp["Speaker 1"] and sp["Speaker 1"]["rejected"] == ["Priya"]
    ms.offer_name(sp, "Speaker 1", "Priya", "said")
    assert "suggest" not in sp["Speaker 1"]
    assert "maybe" not in note.read_text(encoding="utf-8")


def test_removing_a_wrong_person_from_a_note(cfg):
    """The bug report: someone listed who was not in the meeting."""
    d, note = meeting(cfg, voices=("Speaker 1",))
    ms.name_speaker(cfg, d, "Speaker 1", "Priya Shah")
    msg = ms.reject_name(cfg, d, "Priya Shah")
    assert "Speaker 1" in msg
    text = note.read_text(encoding="utf-8")
    assert "Priya" not in text and "Speaker 1 (unrecognized)" in text and ms.VOICES_BEGIN in text
    assert json.loads((d / "speakers.json").read_text())["Speaker 1"]["known"] is False


def test_note_shows_a_suggestion_as_a_question(cfg):
    sp = {"Speaker 1": {"known": False, "first": [1.0, "hi"], "suggest": {"name": "Priya", "why": ["voice"]}}}
    assert ms.participant_label("Speaker 1", sp["Speaker 1"]) == "Speaker 1 (maybe Priya?)"
    assert any("| Priya |" in ln for ln in ms.voices_section(sp))


# -- suggestions


def test_screen_beats_voice_and_agreement_is_recorded():
    sp = {"Speaker 1": {"known": False}}
    ms.offer_name(sp, "Speaker 1", "Tom", "voice")
    ms.offer_name(sp, "Speaker 1", "Priya", "screen")
    assert sp["Speaker 1"]["suggest"]["name"] == "Priya"
    ms.offer_name(sp, "Speaker 1", "Priya Shah", "said")
    assert sp["Speaker 1"]["suggest"] == {"name": "Priya Shah", "why": ["screen", "said"]}


def test_a_weaker_source_does_not_replace_a_stronger_one():
    sp = {"Speaker 1": {"known": False, "suggest": {"name": "Priya", "why": ["screen"]}}}
    ms.offer_name(sp, "Speaker 1", "Tom", "voice")
    assert sp["Speaker 1"]["suggest"]["name"] == "Priya"


def test_a_name_already_in_the_meeting_is_not_offered():
    sp = {"Speaker 1": {"known": False}, "Priya Shah": {"known": True}}
    ms.offer_name(sp, "Speaker 1", "Priya", "said")
    assert "suggest" not in sp["Speaker 1"]


LINES = [seg("Me", "Hi Tom, can you start?", 0, 2), seg("Speaker 1", "Sure, the release is on track.", 2, 5),
         seg("Speaker 2", "Hi all, this is Priya from finance.", 6, 9)]


@pytest.mark.parametrize("answer,want", [
    ('[{"label": "Speaker 2", "name": "Priya", "confidence": 0.95, "quote": "this is Priya from finance"}]', "Priya"),
    ('Sure! [{"label": "Speaker 2", "name": "Priya", "confidence": 0.9, "quote": "this is Priya"}] done', "Priya"),
    ('[{"label": "Speaker 2", "name": "Priya", "confidence": 0.5, "quote": "this is Priya"}]', None),  # unsure
    ('[{"label": "Speaker 2", "name": "Rahul", "confidence": 0.99, "quote": "this is Rahul"}]', None),  # invented
    ('[{"label": "Speaker 2", "name": "Priya", "confidence": 0.99, "quote": "no name here"}]', None),  # no proof
    ('[{"label": "Me", "name": "Priya", "confidence": 0.99, "quote": "this is Priya"}]', None),
    ('[{"label": "Speaker 2", "name": "the finance lead", "confidence": 0.99, "quote": "the finance lead"}]', None),
    ('[{"label": "Speaker 2", "name": "Me", "confidence": 0.99, "quote": "Me"}]', None),
    ('[{"label": "Speaker 2", "name": "Priya", "confidence": "high", "quote": "this is Priya"}]', None),
    ('[]', None), ('I cannot tell.', None), ('[{broken', None), ('["Priya"]', None),
])
def test_spoken_names_must_be_proven(cfg, answer, want):
    sp = {"Speaker 1": {"known": False}, "Speaker 2": {"known": False}}
    ms.suggest_spoken_names(cfg, FakeLLM(answer), [dict(s) for s in LINES], sp)
    assert sp["Speaker 2"].get("suggest", {}).get("name") == want


def test_no_model_call_when_everyone_is_named(cfg):
    llm = FakeLLM("[]")
    ms.suggest_spoken_names(cfg, llm, [dict(s) for s in LINES], {"Tom": {"known": True}})
    assert llm.prompts == []


# -- reading the meeting window


def look(t, active=None, roster=()):
    return {"t": t, "active": active, "roster": list(roster)}


REMOTE = [seg("Speaker 1", "long turn", 0, 40), seg("Speaker 2", "another turn", 50, 90)]


def test_two_looks_name_a_voice():
    assert sn.votes([look(5, "Priya"), look(20, "Priya")], REMOTE, "Me") == {"Speaker 1": "Priya"}


def test_one_look_is_not_enough():
    assert sn.votes([look(5, "Priya")], REMOTE, "Me") == {}


def test_looks_must_be_far_apart():
    assert sn.votes([look(5, "Priya"), look(9, "Priya")], REMOTE, "Me") == {}


def test_conflicting_tiles_name_nobody():
    looks = [look(5, "Priya"), look(20, "Priya"), look(30, "Tom"), look(38, "Tom")]
    assert sn.votes(looks, REMOTE, "Me") == {}


def test_one_name_cannot_belong_to_two_voices():
    looks = [look(5, "Priya"), look(20, "Priya"), look(55, "Priya"), look(70, "Priya")]
    assert sn.votes(looks, REMOTE, "Me") == {}


def test_no_vote_while_you_are_talking():
    lines = REMOTE + [seg("Me", "over", 4, 6), seg("Me", "over", 19, 21)]
    assert sn.votes([look(5, "Priya"), look(20, "Priya")], lines, "Me") == {}


def test_no_vote_during_crosstalk():
    lines = REMOTE + [seg("Speaker 3", "x", 4, 6), seg("Speaker 3", "x", 19, 21)]
    assert sn.votes([look(5, "Priya"), look(20, "Priya")], lines, "Me") == {}


def test_roster_needs_two_sightings():
    assert sn.roster([look(1, roster=["Priya"]), look(2, roster=["Priya", "Budget"])]) == ["Priya"]
    assert sn.roster([look(1, roster=["Priya"])]) == ["Priya"]
    assert sn.roster([look(1, roster=["Me", "Tom"]), look(2, roster=["Me", "Tom"])], "Me") == ["Tom"]


@pytest.mark.parametrize("text,want", [("Priya Shah", "Priya Shah"), ("Tom Berg (Guest)", "Tom Berg"),
                                       ("Anna (Organizer) ", "Anna"), ("Mute", None), ("Share", None),
                                       ("Leave", None), ("Q3 Budget Review 2026", None), ("microsoft teams", None)])
def test_clean_name(text, want):
    assert sn.clean_name(text) == want


def picture(box, color, w=1000, h=600):
    img = np.full((h, w, 3), 32, np.uint8)
    if box:
        l, t, r, b = box
        img[t:t + 4, l:r] = color
        img[b - 4:b, l:r] = color
        img[t:b, l:l + 4] = color
        img[t:b, r - 4:r] = color
    return img


@pytest.mark.parametrize("service,color", [("Zoom", (35, 217, 89)), ("Google Meet", (138, 180, 248)),
                                           ("Microsoft Teams", (127, 133, 245))])
def test_highlight_found(service, color):
    assert sn.find_highlight(picture((100, 80, 500, 400), color), service) == (100, 80, 499, 399)


def test_no_highlight_on_a_plain_window():
    assert sn.find_highlight(picture(None, None)) is None


def test_a_solid_colored_area_is_not_a_highlight():
    img = picture(None, None)
    img[100:400, 100:600] = (35, 217, 89)
    assert sn.find_highlight(img, "Zoom") is None


def test_snapshot_reads_the_name_on_the_highlighted_tile():
    img = picture((100, 80, 500, 400), (35, 217, 89))
    lines = [{"text": "Priya Shah", "x": 110, "y": 370, "w": 90, "h": 16},
             {"text": "Tom Berg", "x": 610, "y": 370, "w": 90, "h": 16},
             {"text": "Budget chart", "x": 200, "y": 100, "w": 90, "h": 16},  # top of the tile
             {"text": "Mute", "x": 300, "y": 580, "w": 40, "h": 16}]
    out = sn.read_snapshot(img, lines, "Zoom")
    assert out["active"] == "Priya Shah"
    assert out["roster"] == ["Priya Shah", "Tom Berg"]
