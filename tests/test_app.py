"""The desktop app's backend and the settings file it edits, without opening a window.

    python -m pytest tests
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402

CONFIG = '''# my settings
my_name = "Me"   # shown in notes
notify = false

[jira]
base_url = "https://x.atlassian.net"
'''


@pytest.fixture
def api(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(f'{CONFIG.split("[jira]")[0]}work_dir = {json.dumps(str(tmp_path / "w"))}\n'
                 f'output_dir = {json.dumps(str(tmp_path / "n"))}\n\n[jira]\nbase_url = "https://x.atlassian.net"\n', encoding="utf-8")
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(p))
    (tmp_path / "w" / "sessions").mkdir(parents=True)
    (tmp_path / "n").mkdir()
    return app.Api()


def session(api, sid, **meta):
    d = api.cfg["work_dir"] / "sessions" / sid
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"title": "T", "status": "done", "started_at": "2026-11-08T10:00:00", **meta}))
    return d


# -- settings file


def test_save_settings_keeps_comments_and_tables(api):
    ms.save_settings({"my_name": "Asha"})
    text = ms.config_path().read_text(encoding="utf-8")
    assert 'my_name = "Asha"   # shown in notes' in text and "# my settings" in text and "[jira]" in text
    assert ms.load_config()["jira"]["base_url"] == "https://x.atlassian.net"


def test_new_key_goes_above_the_tables(api):
    ms.save_settings({"keep_audio": True})
    assert ms.load_config()["keep_audio"] is True
    assert ms.load_config()["jira"].get("keep_audio") is None


@pytest.mark.parametrize("value", ['C:\\Users\\me\\Notes', 'with "quotes"', "à¤ªà¥à¤°à¤¿à¤¯à¤¾", "semi#colon"])
def test_strings_survive(api, value):
    ms.save_settings({"my_name": value})
    assert ms.load_config()["my_name"] == value


def test_lists_and_bools(api):
    ms.save_settings({"meeting_apps": ["zoom", "chrome"], "work_only": False})
    c = ms.load_config()
    assert c["meeting_apps"] == ["zoom", "chrome"] and c["work_only"] is False


def test_vocabulary_table_is_replaced_not_stacked(api):
    ms.save_settings({}, {"app sumo": "AppSumo"})
    ms.save_settings({}, {"kube": "Kubernetes"})
    c = ms.load_config()
    assert c["vocabulary"] == {"kube": "Kubernetes"}
    assert c["jira"]["base_url"].endswith("atlassian.net")
    ms.save_settings({}, {})
    assert ms.load_config()["vocabulary"] == {}


def test_a_broken_file_is_never_written(api, monkeypatch):
    before = ms.config_path().read_text(encoding="utf-8")
    monkeypatch.setattr(ms, "toml_value", lambda v: "= not toml")
    with pytest.raises(Exception):
        ms.save_settings({"my_name": "x"})
    assert ms.config_path().read_text(encoding="utf-8") == before


# -- backend


def test_setup_state_on_a_new_computer(api):
    s = api.setup_state()
    assert s["terms_ok"] is False and s["models_ok"] is False and "as is" in s["terms"]


def test_accepting_terms_stores_the_screen_choice(api):
    api.accept_terms(False)
    assert ms.load_config()["screen_names"] is False and api.setup_state()["terms_ok"]


def test_old_terms_must_be_accepted_again(api):
    (api.cfg["work_dir"] / "terms_accepted.json").write_text(json.dumps({"version": ms.DISCLAIMER_VERSION - 1}))
    assert api.setup_state()["terms_ok"] is False


def test_models_ready_needs_a_finished_download_of_these_models(api):
    cfg = api.cfg
    (cfg["work_dir"] / "models_ready.json").write_text(json.dumps(ms.model_plan(cfg)))
    assert api.setup_state()["models_ok"] is True
    (cfg["work_dir"] / "models_ready.json").write_text(json.dumps([["older/model", None]]))
    assert api.setup_state()["models_ok"] is False


def test_status_off_when_no_recorder(api):
    assert api.status()["state"] == "off"


def test_status_goes_stale(api):
    path = api.cfg["work_dir"] / "status.json"
    path.write_text(json.dumps({"time": time.time(), "state": "recording", "warnings": ["x"]}))
    assert api.status()["state"] == "recording" and api.status()["alive"]
    path.write_text(json.dumps({"time": time.time() - 100, "state": "recording", "warnings": ["x"]}))
    s = api.status()
    assert s["state"] == "off" and s["warnings"] == []


def test_pause_flag(api):
    api.set_paused(True)
    assert (api.cfg["work_dir"] / "paused").exists()
    api.set_paused(False)
    assert not (api.cfg["work_dir"] / "paused").exists()


def test_meetings_list_handles_every_state(api):
    session(api, "2026-11-08_090000", status="recording")
    d = session(api, "2026-11-08_100000", duration_s=600)
    (d / "speakers.json").write_text(json.dumps({"Speaker 1": {"known": False}, "Tom": {"known": True}}))
    rows = api.meetings()
    assert [r["id"] for r in rows] == ["2026-11-08_100000", "2026-11-08_090000"]
    assert rows[0]["to_name"] == 1 and rows[0]["minutes"] == 10


def test_a_corrupt_session_does_not_hide_the_others(api):
    session(api, "2026-11-08_100000")
    d = api.cfg["work_dir"] / "sessions" / "2026-11-08_110000"
    d.mkdir()
    (d / "session.json").write_text("{broken")
    assert [r["id"] for r in api.meetings()] == ["2026-11-08_100000"]


def test_meeting_without_a_note_opens(api):
    session(api, "2026-11-08_100000", status="recorded")
    m = api.meeting("2026-11-08_100000")
    assert m["note_exists"] is False and m["turns"] == [] and m["voices"] == []


@pytest.mark.parametrize("bad", ["../../x", "a/b", "a\\b", "", "..", "C:", None, "nope"])
def test_session_ids_are_checked(api, bad):
    with pytest.raises(ValueError):
        api.meeting(bad)
    with pytest.raises(ValueError):
        api.delete_meeting(bad)


def test_clip_lookup_cannot_leave_the_session(api):
    d = session(api, "2026-11-08_100000")
    (d / "speakers.json").write_text(json.dumps({"Speaker 1": {"known": False, "sample": "gone.wav"}}))
    assert api.voice_clip("2026-11-08_100000", "Speaker 1") is None
    assert api.voice_clip("2026-11-08_100000", "../../secret") is None


def test_delete_removes_only_this_meeting(api):
    cfg = api.cfg
    note = cfg["output_dir"] / "a.md"
    note.write_text("x")
    note.with_suffix(".json").write_text("{}")
    other = cfg["output_dir"] / "keep.md"
    other.write_text("y")
    (cfg["output_dir"] / "index.jsonl").write_text('{"id": "2026-11-08_100000"}\n{"id": "other"}\n')
    d = session(api, "2026-11-08_100000", notes=str(note))
    assert api.delete_meeting("2026-11-08_100000")
    assert not d.exists() and not note.exists() and not note.with_suffix(".json").exists() and other.exists()
    assert (cfg["output_dir"] / "index.jsonl").read_text() == '{"id": "other"}\n'


def test_delete_never_touches_a_note_outside_the_notes_folder(api, tmp_path):
    outside = tmp_path / "important.md"
    outside.write_text("keep me")
    session(api, "2026-11-08_100000", notes=str(outside))
    api.delete_meeting("2026-11-08_100000")
    assert outside.exists()


def test_settings_round_trip(api):
    assert api.save_settings({"my_name": "Asha", "keep_audio": False, "vocabulary": {"a": "B"}})["ok"]
    s = api.settings()
    assert s["my_name"] == "Asha" and s["keep_audio"] is False and s["vocabulary"] == {"a": "B"}
    assert (api.cfg["work_dir"] / "reload").exists()


def test_settings_reject_bad_values(api):
    assert not api.save_settings({"my_name": "   "})["ok"]
    assert not api.save_settings({"keep_audio": "yes"})["ok"]
    api.save_settings({"my_name": "Asha", "work_dir": "C:/elsewhere"})  # not a setting the page may change
    assert str(api.cfg["work_dir"]) != "C:/elsewhere"


def test_download_progress_counts_cached_files(api, tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    cfg = api.cfg
    repo = ms.model_plan(cfg)[0][0]
    folder = hub / ("models--" + repo.replace("/", "--")) / "blobs"
    folder.mkdir(parents=True)
    (folder / "x.incomplete").write_bytes(b"1" * 1000)
    (cfg["work_dir"] / "download_size.json").write_text("4000")
    p = api.download_progress()
    assert p == {"done": False, "failed": False, "error": "", "got": 1000, "total": 4000}


# -- the recorder obeying the app


def test_no_key_handler_can_swallow_typing():
    """An onkeydown that returns false makes the browser drop the key: this is why no box took typing."""
    page = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"onkey\w+:\s*\w+\s*=>\s*[^{]", page)


class Probe:
    def default_input_device(self):
        return "mic"

    def app_states(self, apps):
        return {a["id"]: (False, False) for a in apps}


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(ms.plat, "AudioProbe", Probe)
    cfg = {**ms.DEFAULTS, "work_dir": tmp_path, "output_dir": tmp_path, "log_dir": tmp_path, "jira": {}, "notify": False}
    return ms.Daemon(cfg)


def test_daemon_writes_status(daemon):
    daemon.step()
    s = json.loads((daemon.cfg["work_dir"] / "status.json").read_text())
    assert s["state"] == "watching" and s["pid"]


def test_daemon_pauses_and_stops_a_recording(daemon, monkeypatch):
    daemon.paused_flag.touch()
    stopped = []
    daemon.rec = type("R", (), {"service": "Zoom", "meta": {"title": "x", "started_at": "y"}, "warnings": []})()
    monkeypatch.setattr(daemon, "stop_recording", lambda: (stopped.append(1), setattr(daemon, "rec", None)))
    daemon.step()
    assert stopped == [1]
    assert json.loads((daemon.cfg["work_dir"] / "status.json").read_text())["state"] == "paused"


def test_paused_daemon_looks_for_nothing(daemon, monkeypatch):
    daemon.paused_flag.touch()
    monkeypatch.setattr(daemon, "tick", lambda: pytest.fail("watched while paused"))
    daemon.step()


def test_daemon_reloads_settings_between_calls(daemon, monkeypatch):
    daemon.reload_flag.touch()
    monkeypatch.setattr(ms, "load_config", lambda: {**daemon.cfg, "meeting_apps": ["zoom"]})
    daemon.step()
    assert [a["id"] for a in daemon.apps] == ["zoom"] and not daemon.reload_flag.exists()


def test_daemon_does_not_reload_mid_call(daemon, monkeypatch):
    daemon.reload_flag.touch()
    daemon.rec = object()
    monkeypatch.setattr(daemon, "tick", lambda: None)
    daemon.step()
    assert daemon.reload_flag.exists()
