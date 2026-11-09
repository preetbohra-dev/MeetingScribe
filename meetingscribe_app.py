#!/usr/bin/env python3
"""MeetingScribe's desktop app: a window over the same recorder and commands as the terminal.

With a command (run, process, ...) this is the ordinary program, which is how the packaged
app runs its own background recorder and workers. With none it opens the window. Anything
that loads a model runs as a separate process, so the window never freezes.
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import connectors
import meeting_scribe as ms

UI =ms.HERE / "ui" / "index.html"
APP_NAME = "MeetingScribe"
ALIVE_S = 15  # a recorder that hasn't reported for this long is not running
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run_command(*args, timeout=None):
    """Run this program with args; (ok, stdout text or the error for the user)."""
    r = subprocess.run(ms.self_command(*args), capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    out, err = r.stdout.decode("utf-8", "replace").strip(), r.stderr.decode("utf-8", "replace").strip()
    return (True, out) if r.returncode == 0 else (False, err.splitlines()[-1] if err else "That didn't work.")


def start_at_login(enable):
    """Have the recorder start when the user logs in. Only the packaged app registers this
    way; a checkout has its install script's scheduled task or launch agent."""
    if not ms.FROZEN:
        return
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" run')
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
    else:
        plist = Path("~/Library/LaunchAgents/local.meetingscribe.plist").expanduser()
        if not enable:
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink(missing_ok=True)
            return
        import plistlib
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({"Label": "local.meetingscribe", "ProgramArguments": [sys.executable, "run"],
                                          "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                                          "ProcessType": "Background"}))
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)


def starts_at_login():
    if not ms.FROZEN:
        return True
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, APP_NAME)
            return True
        except OSError:
            return False
    return Path("~/Library/LaunchAgents/local.meetingscribe.plist").expanduser().exists()


class Api:
    """What the page can ask for. Every method returns plain JSON-able data. Page input
    is treated as untrusted: a session id must name a folder that really exists."""

    def __init__(self):
        self.download = None
        self._window = None  # underscore: pywebview walks every public attribute for methods to expose
        self._jira_jobs = {}
        self._task_jobs = {}
        self._jira_lock = threading.Lock()  # a card can't be approved twice by a double click
        self._send_lock = threading.Lock()

    @property
    def cfg(self):
        return ms.load_config()

    def _session(self, sid):
        cfg = self.cfg
        if not isinstance(sid, str) or not re.fullmatch(r"[\w-]+", sid):
            raise ValueError("bad meeting id")
        d = cfg["work_dir"] / "sessions" / sid
        if not (d / "session.json").exists():
            raise ValueError("no such meeting")
        return d

    # -- first run

    def setup_state(self):
        cfg = self.cfg
        return {"terms_ok": ms.terms_accepted(cfg), "models_ok": ms.models_ready(cfg), "terms": ms.DISCLAIMER,
                "ai_chosen": bool(ms.ai_choice(cfg)) or ms.models_ready(cfg), "cloud": cfg["llm_backend"] == "cloud",
                "ram_gb": round(ms.plat.ram_gb()), "llm_fits": cfg["llm_fits"],
                "local_ok": cfg["llm_backend"] == "ollama" or ms.plat.ram_gb() >= ms.LLM_TIERS[-1][0],
                "platform": sys.platform, "downloading": self._downloading()}

    def accept_terms(self, screen_names):
        cfg = self.cfg
        ms.save_settings({"screen_names": bool(screen_names)})
        ms.record_terms(cfg)
        return True

    # -- who writes the notes. A key goes from the page to the keychain and never comes back.

    def cloud_state(self):
        cfg = self.cfg
        s = cfg["cloud_llm"]
        return {"cloud": cfg["llm_backend"] == "cloud", "settings": {k: s[k] for k in ms.CLOUD_FIELDS},
                "providers": [{"id": k, "label": p["label"], "key": p["key"], "model": p["model"], "fields": p["fields"],
                               "optional": p.get("optional", []), "note": p.get("note", ""),
                               "saved": bool(p["key"] and ms.ai_key(k)), "notice": ms.AI_NOTICE.format(p["label"])}
                              for k, p in ms.cloud_llm.PROVIDERS.items()]}

    def cloud_save(self, values, key, agreed):
        """Tests the connection and switches to it only if that worked."""
        if agreed is not True:
            return {"ok": False, "message": "Tick the box to agree first."}
        cfg = self.cfg
        settings = {k: values[k].strip() for k in ms.CLOUD_FIELDS if isinstance(values.get(k), str)}
        key = key.strip() if isinstance(key, str) else ""
        try:
            ms.try_cloud_settings(settings, key)
            ms.save_cloud_choice(cfg, settings, key)
        except Exception as e:  # shown on the page: a wrong key, a missing command, no network
            return {"ok": False, "message": str(e)}
        (cfg["work_dir"] / "reload").touch()
        return {"ok": True}

    def use_local(self, forget=False):
        cfg = self.cfg
        provider = cfg["cloud_llm"]["provider"]
        if forget and ms.cloud_llm.PROVIDERS.get(provider, {}).get("key"):
            ms.keychain("delete", provider, service=ms.cloud_llm.KEY_SERVICE)
        ms.use_local_ai(cfg)
        (cfg["work_dir"] / "reload").touch()
        return True

    def _downloading(self):
        return bool(self.download and self.download.poll() is None)

    def start_download(self):
        if not self._downloading():
            self.download = subprocess.Popen(ms.self_command("download-models"), stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        return True

    def download_progress(self):
        cfg = self.cfg
        try:
            total = json.loads((cfg["work_dir"] / "download_size.json").read_text())
        except (OSError, ValueError):
            total = None
        hub = Path(os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME", "~/.cache/huggingface") + "/hub").expanduser()
        got = 0
        for repo, _ in ms.model_plan(cfg):
            for p in (hub / ("models--" + repo.replace("/", "--"))).rglob("*"):
                try:
                    if not p.is_symlink() and p.is_file():
                        got += p.stat().st_size
                except OSError:
                    pass
        failed = self.download is not None and self.download.poll() not in (None, 0)
        try:
            error = (cfg["work_dir"] / "download_error.txt").read_text(encoding="utf-8") if failed else ""
        except OSError:
            error = ""
        return {"done": ms.models_ready(cfg), "failed": failed, "error": error, "got": got, "total": total}

    def finish_setup(self):
        cfg = self.cfg
        start_at_login(True)
        self.ensure_recorder()
        return ms.terms_accepted(cfg) and ms.models_ready(cfg)

    # -- the recorder

    def ensure_recorder(self):
        if not self.status()["alive"]:
            subprocess.Popen(ms.self_command("run"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        return True

    def status(self):
        cfg = self.cfg
        try:
            s = json.loads((cfg["work_dir"] / "status.json").read_text())
        except (OSError, ValueError):
            s = {"time": 0, "state": "off", "warnings": []}
        s["alive"] = time.time() - s.get("time", 0) < ALIVE_S
        if not s["alive"]:
            s.update(state="off", warnings=[])
        return s

    def set_paused(self, paused):
        flag = self.cfg["work_dir"] / "paused"
        if paused:
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
        return True

    # -- meetings

    def meetings(self):
        cfg = self.cfg
        out = []
        for p in sorted((cfg["work_dir"] / "sessions").glob("*/session.json"), reverse=True):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            sp = p.parent / "speakers.json"
            speakers = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
            out.append({"id": p.parent.name, "title": m.get("title", "Meeting"), "started_at": m.get("started_at"),
                        "minutes": round(m.get("duration_s", 0) / 60), "status": m.get("status"), "source": m.get("source"),
                        "to_name": sum(not s["known"] for s in speakers.values()),
                        "people": [n for n in speakers]})
        return out

    def meeting(self, sid):
        d = self._session(sid)
        speakers, meta = ms.session_speakers(d)
        tr = d / "transcript.json"
        turns = json.loads(tr.read_text(encoding="utf-8")) if tr.exists() else []
        note = Path(meta.get("notes") or "/missing")
        text = note.read_text(encoding="utf-8") if note.exists() else ""
        voices = []
        for name, s in speakers.items():
            voices.append({"label": name, "known": s["known"], "confirmed": s.get("confirmed", False), "seconds": s.get("seconds"),
                           "suggest": s.get("suggest"), "first": s.get("first"), "has_clip": bool(s.get("sample"))})
        return {"id": sid, "meta": {k: meta.get(k) for k in ("title", "started_at", "duration_s", "source", "status")},
                "summary": ms.note_section(text, "Summary"), "actions": ms.note_section(text, "Action Items"),
                "talk": ms.talk_shares(turns), "turns": [{"start": t["start"], "speaker": t["speaker"], "text": t["text"], "flag": t.get("flag")} for t in turns],
                "voices": voices, "note_exists": note.exists(), "me": self.cfg["my_name"],
                "jira": bool(self.cfg["jira"]["base_url"]), "destinations": self._connectors().destinations()}

    def voice_clip(self, sid, label):
        d = self._session(sid)
        speakers, _ = ms.session_speakers(d)
        sample = speakers.get(label, {}).get("sample")
        return self._data_url(d / "voices" / sample) if sample else None

    @staticmethod
    def _data_url(path):
        if not path.exists():
            return None
        return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()

    def name_voice(self, sid, label, name):
        self._session(sid)
        ok, msg = run_command("name-speaker", label, name, "--session", sid)
        return {"ok": ok, "message": msg}

    def reject_voice(self, sid, label):
        self._session(sid)
        ok, msg = run_command("reject-name", label, "--session", sid)
        return {"ok": ok, "message": msg}

    def retry(self, sid):
        d = self._session(sid)
        subprocess.Popen(ms.self_command("process", str(d)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW)
        return True

    def delete_meeting(self, sid):
        """Removes the recording, the note, and its files, after the page has confirmed."""
        import shutil
        d = self._session(sid)
        cfg = self.cfg
        _, meta = ms.session_speakers(d)
        note = Path(meta["notes"]) if meta.get("notes") else None
        if note and note.parent == cfg["output_dir"]:
            for suffix in (".md", ".json", ".wav"):
                note.with_suffix(suffix).unlink(missing_ok=True)
        index = cfg["output_dir"] / "index.jsonl"
        if index.exists():
            kept = [ln for ln in index.read_text(encoding="utf-8").splitlines() if json.loads(ln).get("id") != sid]
            index.write_text("".join(ln + "\n" for ln in kept), encoding="utf-8")
        shutil.rmtree(d)
        return True

    def open_note(self, sid):
        _, meta = ms.session_speakers(self._session(sid))
        self._open(Path(meta["notes"]))
        return True

    def open_notes_folder(self):
        self._open(self.cfg["output_dir"])
        return True

    @staticmethod
    def _open(path):
        if sys.platform == "win32":
            os.startfile(str(path))
        else:
            subprocess.Popen(["open", str(path)])

    # -- search and questions

    def search(self, query):
        return ms.search_meetings(self.cfg, str(query))

    def ask(self, question, sid=None):
        args = ["ask", str(question), "--json"] + (["--session", sid] if sid else [])
        if sid:
            self._session(sid)
        ok, out = run_command(*args)
        if not ok:
            return {"answer": None, "error": out, "sources": []}
        return json.loads(out.splitlines()[-1])

    # -- people

    def people(self):
        lib = ms.VoiceLibrary(self.cfg)
        return [{"name": n, "seconds": v["seconds"], "saved_at": v["saved_at"]} for n, v in sorted(lib.voices.items())]

    def person_clip(self, name):
        lib = ms.VoiceLibrary(self.cfg)
        v = lib.voices.get(name)
        return self._data_url(lib.dir / v["file"]) if v else None

    def forget_person(self, name):
        return ms.VoiceLibrary(self.cfg).forget(name)

    # -- Slack, Teams, Gmail and Outlook. A webhook link goes to the keychain and never comes back.

    def _connectors(self):
        return connectors.Connectors(ms, self.cfg)

    @staticmethod
    def _connector_result(fn):
        try:
            return {"ok": True, "message": fn() or ""}
        except connectors.ConnectorError as e:
            return {"ok": False, "message": str(e)}

    def connector_state(self):
        c = self.cfg
        return {"destinations": self._connectors().destinations(), "jira": bool(c["jira"]["base_url"]), "task_places": len(ms.task_agent.Tasks(ms, c).repos())}

    def connector_add_channel(self, service, name, url):
        return self._connector_result(lambda: self._connectors().add_channel(service, name, url) and "Connected. A test message was posted to the channel.")

    def connector_set_email(self, service, account="work"):
        return self._connector_result(lambda: self._connectors().set_email(service, account))

    def connector_remove(self, did):
        return self._connector_result(lambda: self._connectors().remove(did))

    def connector_compose(self, sid):
        d = self._session(sid)
        speakers, meta = ms.session_speakers(d)
        note = Path(meta.get("notes") or "/missing")
        return {"title": meta.get("title"), **self._connectors().compose(d, meta, note.read_text(encoding="utf-8") if note.exists() else "", list(speakers))}

    def connector_send(self, sid, did, kind, owner, subject, text, to="", slack="", again=False):
        d = self._session(sid)
        with self._send_lock:  # a double click can't message a channel twice
            return self._connector_result(lambda: self._connectors().send(d, did, kind, owner, subject, text, to, slack, again is True))

    # -- Jira

    def jira_status(self):
        j = self.cfg["jira"]
        out = {"base_url": j["base_url"], "auth": j["auth"], "name": None, "error": None}
        if j["base_url"]:
            try:
                out["name"] = ms.stored_jira_client(j)[1].get("displayName") or j["base_url"]
            except ms.JiraError as e:
                out["error"] = "Sign in again: Jira didn't accept what is saved." if e.status == 401 else str(e)
        return out

    def jira_sign_in(self, base_url, auth, email, secret):
        try:
            name = ms.sign_in_jira(self.cfg, str(base_url), str(auth), str(email or ""), str(secret))
        except ms.JiraError as e:
            hint = {401: "Jira didn't accept that. Check it and paste it again."}.get(e.status)
            return {"ok": False, "message": hint or ms.jira_problem(e)}
        return {"ok": True, "message": f"Connected as {name}."}

    def jira_forget(self):
        ms.forget_jira(self.cfg["jira"])
        return {"ok": True, "message": "Disconnected. Your saved sign-in is gone from this computer."}

    def jira_spaces(self, sid):
        d = self._session(sid)
        cfg = self.cfg
        try:
            client, _ = ms.stored_jira_client(cfg["jira"])
            spaces = sorted(client.projects(), key=lambda p: p["key"])
        except ms.JiraError as e:
            return {"spaces": [], "error": ms.jira_problem(e), "needs_signin": e.status == 401}
        return {"spaces": [{"key": p["key"], "name": p.get("name", "")} for p in spaces],
                "suggested": ms.last_jira_project(cfg, ms.jira_meeting(cfg, d)[5])}

    def _job_running(self, key):
        job = self._jira_jobs.get(key)
        return job is not None and job.poll() is None

    def _spawn_jira(self, key, *args):
        self._jira_jobs[key] = subprocess.Popen(ms.self_command("jira-draft", *args), stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    def jira_start(self, sid, project, refresh=False):
        self._session(sid)
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", project):
            return {"ok": False, "message": "Choose a space."}
        if self._job_running(sid):
            return {"ok": False, "message": "Already working on this meeting."}
        self._spawn_jira(sid, sid, "--project", project, *(["--refresh"] if refresh else []))
        return {"ok": True}

    def jira_cancel(self, sid):
        d = self._session(sid)
        if self._job_running(sid):
            self._jira_jobs[sid].terminate()
            self._jira_jobs[sid].wait(10)
        ms.write_json(ms.review_dir(d) / "state.json", {**ms.review_state(d), "state": "stopped", "error": ""})
        return True

    def jira_review(self, sid):
        d = self._session(sid)
        state = ms.review_state(d)
        if state["state"] == "working" and not self._job_running(sid):
            state = {**state, "state": "stopped", "error": "That was interrupted before it finished."}
        cards = []
        for card in ms.review_cards(d):
            if card["status"] == "drafting" and not self._job_running((sid, card["n"])):
                card.update(status="pending", error="That was interrupted before it finished.")
            if card["status"] == "working" and state["state"] != "working":
                continue
            cards.append(ms.card_view(self.cfg, card, state))
        return {"title": ms.jira_meeting(self.cfg, d)[0]["title"], "state": state["state"], "step": state.get("step", ""),
                "error": state.get("error", ""), "needs_signin": state.get("needs_signin", False),
                "project": state.get("project", ""), "done": state.get("done", 0), "total": state.get("total", 0),
                "learned_from": state.get("learned_from", 0), "cards": cards}

    def jira_card_form(self, sid, n):
        return ms.card_form(self.cfg, self._session(sid), int(n))

    def jira_save_card(self, sid, n, edits):
        d = self._session(sid)
        with self._jira_lock:
            return ms.card_save(self.cfg, d, int(n), edits if isinstance(edits, dict) else {})

    def jira_approve(self, sid, n):
        d = self._session(sid)
        with self._jira_lock:
            return ms.card_approve(self.cfg, d, int(n))

    def jira_dismiss(self, sid, n, undo=False):
        d = self._session(sid)
        with self._jira_lock:
            return ms.card_dismiss(d, int(n), bool(undo))

    def jira_make_ticket(self, sid, n):
        """The model suggested a comment or nothing, and you want a ticket: draft one."""
        d = self._session(sid)
        n = int(n)
        with self._jira_lock:
            card = ms.read_json(ms.card_path(d, n))
            if not card or card["status"] != "pending":
                return {"ok": False, "message": "This one has already been handled."}
            ms.write_json(ms.card_path(d, n), {**card, "status": "drafting", "error": ""})
            self._spawn_jira((sid, n), sid, "--card", str(n))
        return {"ok": True}

    def jira_meetings(self):
        out = []
        for m in self.meetings():
            if m["status"] != "done":
                continue
            d = self._session(m["id"])
            cards = ms.review_cards(d)
            items = (ms.read_json(d / "jira.json") or {}).get("items", [])
            out.append({"id": m["id"], "title": m["title"], "started_at": m["started_at"], "in_jira": len(items),
                        "to_review": sum(c["status"] == "pending" for c in cards)})
        return out

    @staticmethod
    def _space(project):
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", project):
            raise ValueError("bad space")
        return project.upper()

    def jira_habits(self):
        cfg = self.cfg
        out = []
        for p in sorted((cfg["work_dir"] / "jira_spaces").glob("*.json")):
            space = ms.read_json(p)
            if space:
                out.append({"project": space["project"], "learned_at": space["learned_at"], "tickets": space["tickets"],
                            "types": ms.space_form(space), "epics": len(space["epics"]), "sprints": len(space["sprints"])})
        return out

    def jira_habit_save(self, project, type_name, values):
        with self._jira_lock:
            return ms.space_save(self.cfg, self._space(project), str(type_name), values)

    def jira_habit_learn(self, project):
        project, cfg = self._space(project), self.cfg
        try:
            client, _ = ms.stored_jira_client(cfg["jira"])
            ms.learn_space(cfg, client, project, client.issue_types(project))
        except ms.JiraError as e:
            return {"ok": False, "message": ms.jira_problem(e)}
        return {"ok": True, "message": f"Learned {project} again. Your corrections were kept."}

    def jira_habit_forget(self, project):
        ms.space_path(self.cfg, self._space(project)).unlink(missing_ok=True)
        return {"ok": True, "message": "Forgotten. It will learn this space again next time."}

    def jira_open(self, key):
        """A ticket in the browser. Only a real issue key, on the connected site."""
        if isinstance(key, str) and ms.ISSUE_KEY.fullmatch(key) and self.cfg["jira"]["base_url"]:
            import webbrowser
            webbrowser.open(f"{self.cfg['jira']['base_url'].rstrip('/')}/browse/{key}")
        return True

    # -- tasks handed to the AI

    @property
    def _tasks(self):
        return ms.task_agent.Tasks(ms, self.cfg)

    def _task_running(self, tid):
        job = self._task_jobs.get(tid)
        return bool(job and job.poll() is None)

    def _spawn_task(self, tid):
        self._task_jobs[tid] = subprocess.Popen(ms.self_command("task-run", tid), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                creationflags=CREATE_NO_WINDOW, start_new_session=sys.platform != "win32")

    def _task_call(self, fn, *args):
        """Runs fn; a TaskError is a sentence for the page."""
        try:
            return {"ok": True, "value": fn(*args)}
        except ms.task_agent.TaskError as e:
            return {"ok": False, "message": str(e)}

    def task_setup(self):
        cfg = self.cfg
        provider = self._tasks.provider()
        info = ms.cloud_llm.PROVIDERS.get(provider, {})
        return {"repos": self._tasks.repos(), "provider": info.get("label", ""), "note": info.get("note", ""),
                "local": cfg["llm_backend"] != "cloud", "gh": bool(ms.cloud_llm.find_command("gh"))}

    def task_repo_suggest(self, path):
        return ms.task_agent.suggest_commands(path) if isinstance(path, str) and Path(path).is_dir() else []

    def task_repo_save(self, path, commands, kind="repo"):
        return self._task_call(self._tasks.save_repo, str(path), commands if isinstance(commands, list) else [], "folder" if kind == "folder" else "repo")

    def task_repo_forget(self, name):
        self._tasks.forget_repo(str(name))
        return True

    def tasks(self):
        return [self._task_view(t) for t in self._tasks.all()]

    def _task_view(self, t):
        if t["status"] == "working" and not self._task_running(t["id"]):
            t = {**t, "status": "attention", "error": "That was interrupted before it finished."}
        return {k: t.get(k) for k in ("id", "meeting", "task", "status", "stage", "understanding", "plan", "questions", "order", "repos",
                                      "round", "results", "prs", "applied", "log", "error", "title", "summary", "created")}

    def task(self, tid):
        return self._task_view(self._tasks.get(tid))

    def task_start(self, sid, task, agreed):
        d = self._session(sid)
        _, meta = ms.session_speakers(d)
        note = Path(meta.get("notes") or "/missing")
        text = note.read_text(encoding="utf-8") if note.exists() else ""
        lines = "\n".join("- " + s for s in ms.note_section(text, "Summary"))
        turns = ms.read_json(d / "transcript.json", []) or []
        said = "\n".join(f"{t['speaker']}: {t['text']}" for t in turns)
        r = self._task_call(self._tasks.new, sid, meta.get("title", "Meeting"), task, f"SUMMARY:\n{lines}\n\nTRANSCRIPT:\n{said}", agreed)
        if r["ok"]:
            self._spawn_task(r["value"]["id"])
            r["value"] = r["value"]["id"]
        return r

    def task_answer(self, tid, repos, answers):
        r = self._task_call(self._tasks.answer, tid, repos if isinstance(repos, list) else [], answers)
        if r["ok"]:
            self._spawn_task(tid)
            r["value"] = tid
        return r

    def task_retry(self, tid, note=""):
        if self._task_running(tid):
            return {"ok": False, "message": "It's already working."}
        r = self._task_call(self._tasks.retry, tid, note)
        if r["ok"]:
            self._spawn_task(tid)
            r["value"] = tid
        return r

    def task_publish_passing(self, tid):
        if self._task_running(tid):
            return {"ok": False, "message": "It's already working."}
        tasks = self._tasks
        t = tasks.get(tid)
        threading.Thread(target=tasks.publish_passing, args=(t,), daemon=True).start()
        return {"ok": True}

    def task_apply(self, tid):
        return self._task_call(self._tasks.apply_reviewed, tid)

    def task_undo(self, tid):
        return self._task_call(self._tasks.undo, tid)

    def task_stop(self, tid):
        if self._task_running(tid):
            ms.task_agent.kill_tree(self._task_jobs[tid])
        tasks = self._tasks
        t = tasks.get(tid)
        if t["status"] == "working":
            t.update(status="stopped", error="")
            tasks.save(t)
        return True

    def task_discard(self, tid):
        self.task_stop(tid)
        self._tasks.discard(tid)
        return True

    def task_open(self, url):
        if isinstance(url, str) and re.fullmatch(r"https://[\w.-]+/[\w./-]+/pull/\d+", url):
            import webbrowser
            webbrowser.open(url)
        return True

    # -- settings

    SETTINGS = {"my_name": str, "language": str, "output_dir": str, "keep_audio": bool, "work_only": bool,
                "notify": bool, "screen_names": bool, "meeting_apps": list, "mic_device": str}

    def settings(self):
        cfg = self.cfg
        out = {k: cfg[k] for k in self.SETTINGS}
        out["output_dir"] = str(cfg["output_dir"])
        out["vocabulary"] = cfg["vocabulary"]
        out["start_at_login"] = starts_at_login()
        out["packaged"] = ms.FROZEN
        return out

    def save_settings(self, values):
        changes = {}
        for k, kind in self.SETTINGS.items():
            if k in values:
                if not isinstance(values[k], kind):
                    return {"ok": False, "message": f"{k} has the wrong type"}
                changes[k] = values[k]
        if "my_name" in changes and not changes["my_name"].strip():
            return {"ok": False, "message": "Your name can't be empty."}
        vocab = values.get("vocabulary")
        ms.save_settings(changes, vocab if isinstance(vocab, dict) else None)
        if "start_at_login" in values:
            start_at_login(bool(values["start_at_login"]))
        (self.cfg["work_dir"] / "reload").touch()
        return {"ok": True, "message": "Saved."}

    def choose_folder(self):
        import webview
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return picked[0] if picked else None


def open_window():
    import webview
    api = Api()
    cfg = ms.load_config()
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)
    window = webview.create_window(APP_NAME, str(UI), js_api=api, width=1100, height=760, min_size=(760, 560))
    api._window = window
    threading.Thread(target=lambda: ms.terms_accepted(cfg) and ms.models_ready(cfg) and api.ensure_recorder(), daemon=True).start()
    webview.start()


def main():
    if len(sys.argv) > 1:
        ms.main()
    else:
        open_window()


if __name__ == "__main__":
    main()
