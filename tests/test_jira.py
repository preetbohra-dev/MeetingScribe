"""The app's Jira screen, against a stand-in Jira server over real HTTP.

    python -m pytest tests
"""

import argparse
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402

SID = "2026-11-08_100000"
FIELDS = [
    {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string", "system": "summary"}},
    {"fieldId": "description", "name": "Description", "required": False, "schema": {"type": "string"}},
    {"fieldId": "priority", "name": "Priority", "required": False, "schema": {"type": "priority"},
     "allowedValues": [{"id": "1", "name": "High"}, {"id": "2", "name": "Low"}]},
    {"fieldId": "labels", "name": "Labels", "required": False, "schema": {"type": "array", "items": "string", "system": "labels"}},
    {"fieldId": "duedate", "name": "Due date", "required": False, "schema": {"type": "date"}},
    {"fieldId": "components", "name": "Components", "required": False, "schema": {"type": "array", "items": "component"},
     "allowedValues": [{"id": "7", "name": "Validation"}, {"id": "8", "name": "Reports"}]},
    {"fieldId": "customfield_1", "name": "Story points", "required": False, "schema": {"type": "number"}},
    {"fieldId": "customfield_2", "name": "Sponsor", "required": False, "schema": {"type": "user"}},
    {"fieldId": "assignee", "name": "Assignee", "required": False, "schema": {"type": "user"}},
    {"fieldId": "customfield_3", "name": "Environment", "required": False, "schema": {"type": "string"}},
    {"fieldId": "customfield_4", "name": "Team", "required": False, "schema": {"type": "option"},
     "allowedValues": [{"id": "31", "value": "Data"}, {"id": "32", "value": "Ops"}]},
    {"fieldId": "parent", "name": "Parent", "required": False, "schema": {"type": "issuelink"}},
    {"fieldId": "customfield_5", "name": "Sprint", "required": False,
     "schema": {"type": "array", "items": "json", "custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
]
BUG_FIELDS = [f for f in FIELDS if f["fieldId"] != "parent"] + [{"fieldId": "environment", "name": "Environment", "required": True, "schema": {"type": "string"}}]
TICKETS = {
    "EDA-101": {"summary": "Nightly reporting jobs fail", "status": ("In Progress", "indeterminate")},
    "OPS-9": {"summary": "Old firewall change", "status": ("Done", "done")},
}


class Jira:
    """Just enough of Jira's REST API, recording every write."""

    def __init__(self):
        self.posts, self.token_ok, self.refuse, self.next_key = [], True, None, 200
        self.history, self.epics, self.searches = [], [], []
        jira = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def issue(self, key):
                t = TICKETS[key]
                return {"key": key, "fields": {"summary": t["summary"], "updated": "2026-11-01", "description": "",
                                               "status": {"name": t["status"][0], "statusCategory": {"key": t["status"][1]}},
                                               "issuetype": {"name": "Task"}, "assignee": None, "project": {"key": key.split("-")[0]}}}

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                p, q = u.path.replace("/rest/api/2", ""), urllib.parse.parse_qs(u.query)
                if not jira.token_ok or self.headers.get("Authorization") != "Bearer s3cr3t-tok":
                    return self.send(401, {"errorMessages": ["no"]})
                if p == "/myself":
                    return self.send(200, {"displayName": "Asha Rao", "name": "asha"})
                if p == "/project":
                    return self.send(200, [{"key": "EDA", "name": "Enterprise Data"}, {"key": "OPS", "name": "Operations"}])
                if p == "/issue/createmeta/EDA/issuetypes":
                    return self.send(200, {"issueTypes": [{"id": "10", "name": "Task"}, {"id": "11", "name": "Bug"}], "total": 2})
                if p.startswith("/issue/createmeta/EDA/issuetypes/"):
                    f = BUG_FIELDS if p.endswith("/11") else FIELDS
                    return self.send(200, {"fields": f, "total": len(f)})
                if p == "/search":
                    jql, hits = q["jql"][0], []
                    jira.searches.append(jql)
                    if jql.startswith('project = "EDA" AND issuetype = Epic'):
                        return self.send(200, {"issues": jira.epics, "total": len(jira.epics)})
                    if jql.startswith('project = "EDA"'):
                        return self.send(200, {"issues": jira.history, "total": len(jira.history)})
                    if "reporting" in jql:
                        hits = ["EDA-101"]
                    if "firewall" in jql:
                        hits = ["OPS-9"]
                    return self.send(200, {"issues": [self.issue(k) for k in hits], "total": len(hits)})
                if p == "/user/assignable/search":
                    name = q["username"][0]
                    return self.send(200, [{"displayName": "Dan Ortiz", "name": "dan"}] if name == "Dan Ortiz" else [])
                if p.endswith("/comment"):
                    return self.send(200, {"total": 2, "comments": [
                        {"author": {"displayName": "Old"}, "created": "2026-10-01T09:00", "body": "first"},
                        {"author": {"displayName": "Dan Ortiz"}, "created": "2026-11-02T09:00", "body": "jobs still failing"}]})
                if p.startswith("/issue/"):
                    return self.send(200, self.issue(p.split("/")[2]))
                self.send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                p = self.path.replace("/rest/api/2", "")
                if not jira.token_ok:
                    return self.send(401, {"errorMessages": ["no"]})
                if jira.refuse:
                    return self.send(400, jira.refuse)
                jira.posts.append((p, body))
                if p == "/issue":
                    jira.next_key += 1
                    return self.send(201, {"key": f"EDA-{jira.next_key}"})
                self.send(201, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def created(self):
        return [b["fields"] for p, b in self.posts if p == "/issue"]


CANDS = [
    {"title": "Validate store_sales row counts", "details": "Compare counts", "owner": "Me", "due": "2026-11-09", "kind": "Task",
     "priority": "High", "keywords": ["store_sales"], "evidence": "Me: I'll validate the row counts"},
    {"title": "Repoint the reporting jobs", "details": "Move them", "owner": "Dan Ortiz", "kind": "Task", "keywords": ["reporting"],
     "evidence": "the reporting jobs still fail"},
    {"title": "Firewall rule", "details": "Open it", "owner": "Speaker 2", "kind": "Task", "keywords": ["firewall"],
     "evidence": "the firewall rule"},
]


class FakeLLM:
    def __init__(self, cfg=None):
        pass

    def chat(self, prompt, max_tokens=1500):
        if prompt.startswith("Write a Jira"):
            return json.dumps({"summary": "Do: " + re.search(r"title: (.*)", prompt).group(1), "description": "h3. Context\nWhy",
                               "priority": "High", "labels": ["delta"], "components": ["Validation"]})
        if prompt.startswith("A meeting produced"):
            if "title: Repoint" in prompt:
                return json.dumps({"labels": {"EDA-101": "same"}, "reason": "EDA-101 is this work."})
            return json.dumps({"labels": {"OPS-9": "same"}, "recurring": False, "reason": "OPS-9 already did it."})
        return json.dumps(CANDS)


@pytest.fixture
def jira():
    j = Jira()
    yield j
    j.server.shutdown()


@pytest.fixture
def api(tmp_path, monkeypatch, jira):
    p = tmp_path / "config.toml"
    p.write_text(f'my_name = "Me"\nnotify = false\nwork_dir = {json.dumps(str(tmp_path / "w"))}\n'
                 f'output_dir = {json.dumps(str(tmp_path / "n"))}\nlog_dir = {json.dumps(str(tmp_path / "l"))}\n\n'
                 '[jira]\nsearch_days = 30   # how far back\n', encoding="utf-8")
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(p))
    secrets = {}
    monkeypatch.setattr(ms, "keychain", lambda action, account, secret=None: (
        secrets.get(account) if action == "get" else secrets.__setitem__(account, secret) if action == "set" else secrets.pop(account, None)))
    monkeypatch.setattr(ms, "LLM", FakeLLM)
    monkeypatch.setattr(ms.plat, "lower_priority", lambda: None)
    monkeypatch.setattr(ms.plat, "background_process", lambda: None)
    a = app.Api()
    a.secrets = secrets

    def spawn(key, *args):  # the worker, in this process
        parser = argparse.Namespace(session=args[0], project="", refresh="--refresh" in args, card=None)
        if "--project" in args:
            parser.project = args[args.index("--project") + 1]
        if "--card" in args:
            parser.card = int(args[args.index("--card") + 1])
        ms.cmd_jira_draft(ms.load_config(), parser)
    monkeypatch.setattr(a, "_spawn_jira", spawn)
    cfg = a.cfg
    cfg["work_dir"].mkdir(parents=True)
    ms.record_terms(cfg)
    d = cfg["work_dir"] / "sessions" / SID
    d.mkdir(parents=True, exist_ok=True)
    cfg["output_dir"].mkdir()
    note = cfg["output_dir"] / "note.md"
    note.write_text("# Sync\n\n## Summary\n- x\n\n---\n\n## Transcript\nMe: hi\n", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({"title": "Sync", "status": "done", "started_at": "2026-11-08T10:00:00+05:30",
                                                "notes": str(note)}), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps([{"start": 1, "end": 2, "speaker": "Me", "text": "hi"}]), encoding="utf-8")
    a.d = d
    return a


def connect(api, jira):
    r = api.jira_sign_in(jira.url, "bearer", "", "s3cr3t-tok")
    assert r["ok"], r
    return r


def review(api, jira):
    connect(api, jira)
    assert api.jira_start(SID, "EDA")["ok"]
    r = api.jira_review(SID)
    assert r["state"] == "ready", r
    return r


def card(api, n):
    return next(c for c in api.jira_review(SID)["cards"] if c["n"] == n)


def all_text(root):
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in Path(root).rglob("*") if p.is_file())


# -- signing in


def test_good_sign_in_is_saved_without_the_secret_in_any_file(api, jira, tmp_path):
    r = connect(api, jira)
    assert "Asha Rao" in r["message"]
    assert api.secrets[f"{jira.url}#bearer"] == "s3cr3t-tok"
    assert "s3cr3t-tok" not in all_text(tmp_path)
    assert api.cfg["jira"]["base_url"] == jira.url and api.cfg["jira"]["auth"] == "bearer"
    assert api.jira_status()["name"] == "Asha Rao"


def test_jira_settings_keep_the_comments_in_the_file(api, jira):
    connect(api, jira)
    assert "search_days = 30   # how far back" in ms.config_path().read_text(encoding="utf-8")


def test_rejected_sign_in_saves_nothing(api, jira):
    assert not api.jira_sign_in(jira.url, "bearer", "", "bad")["ok"]
    assert api.secrets == {} and api.cfg["jira"]["base_url"] == ""


@pytest.mark.parametrize("site,auth,email,secret", [
    ("http://example.com", "bearer", "", "x"), ("", "bearer", "", "x"), ("https://a.b", "nonsense", "", "x"),
    ("https://a.b", "bearer", "", "   "), ("https://a.b", "basic", "not-an-email", "x"), ("https://a.b", "cookie", "", "a\nb: c")])
def test_bad_sign_in_details_are_refused_before_any_request(api, site, auth, email, secret):
    r = api.jira_sign_in(site, auth, email, secret)
    assert not r["ok"] and api.secrets == {}


def test_disconnect_removes_the_saved_secret(api, jira):
    connect(api, jira)
    api.jira_forget()
    assert api.secrets == {} and api.jira_status()["error"]


def test_a_long_cookie_is_stored_in_pieces_and_comes_back_whole(monkeypatch):
    import keyring
    import keyring.errors
    store = {}

    def put(service, name, value):
        assert len(value) <= 1280, "Windows refuses this"
        store[name] = value

    def delete(service, name):
        if name not in store:
            raise keyring.errors.PasswordDeleteError()
        del store[name]
    monkeypatch.setattr(keyring, "set_password", put)
    monkeypatch.setattr(keyring, "get_password", lambda s, n: store.get(n))
    monkeypatch.setattr(keyring, "delete_password", delete)
    cookie = "JSESSIONID=" + "x" * 8000
    ms.keychain("set", "site#cookie", cookie)
    assert ms.keychain("get", "site#cookie") == cookie and len(store) > 2
    ms.keychain("set", "site#cookie", "short")
    assert ms.keychain("get", "site#cookie") == "short" and list(store) == ["site#cookie"]
    ms.keychain("set", "site#cookie", cookie)
    ms.keychain("delete", "site#cookie")
    assert store == {}


def test_spaces_suggest_the_last_one_used(api, jira):
    connect(api, jira)
    review_state = api.jira_spaces(SID)
    assert [s["key"] for s in review_state["spaces"]] == ["EDA", "OPS"]
    api.jira_start(SID, "EDA")
    assert api.jira_spaces(SID)["suggested"] == "EDA"


# -- drafting


def test_drafting_sends_nothing_to_jira(api, jira):
    review(api, jira)
    assert jira.posts == []


def test_each_card_gets_the_models_suggestion(api, jira):
    cards = review(api, jira)["cards"]
    assert [c["action"] for c in cards] == ["create", "comment", "skip"]
    assert cards[0]["ticket"]["space"] == "EDA" and cards[0]["evidence"] == "I'll validate the row counts"
    assert cards[1]["key"] == "EDA-101" and "Sync" in cards[1]["comment"]
    assert cards[2]["related"][0]["key"] == "OPS-9" and "ticket" not in cards[2]


def test_no_secret_in_the_review_files(api, jira):
    review(api, jira)
    assert "s3cr3t-tok" not in all_text(ms.review_dir(api.d))


def test_without_a_sign_in_the_worker_asks_for_one(api):
    api.jira_start(SID, "EDA")
    r = api.jira_review(SID)
    assert r["state"] == "failed" and r["needs_signin"]


def test_a_model_failure_is_reported(api, jira, monkeypatch):
    connect(api, jira)

    class Broken(FakeLLM):
        def __init__(self, cfg=None):
            raise RuntimeError("out of memory")
    monkeypatch.setattr(ms, "LLM", Broken)
    api.jira_start(SID, "EDA")
    r = api.jira_review(SID)
    assert r["state"] == "failed" and "out of memory" in r["error"] and not r["needs_signin"]


def test_an_interrupted_worker_is_not_shown_as_working(api, jira):
    review(api, jira)
    ms.write_json(ms.review_dir(api.d) / "state.json", {**ms.review_state(api.d), "state": "working"})
    assert api.jira_review(SID)["state"] == "stopped"


@pytest.mark.parametrize("project", ["", "A B", "../x", 5, None])
def test_a_bad_space_is_refused(api, project):
    assert not api.jira_start(SID, project)["ok"]


@pytest.mark.parametrize("sid", ["..", "../../x", "a/b", "", None, 5])
def test_a_bad_meeting_id_is_refused(api, sid):
    with pytest.raises(ValueError):
        api.jira_review(sid)


# -- approving


def test_approve_creates_the_ticket_with_the_right_fields(api, jira):
    review(api, jira)
    r = api.jira_approve(SID, 0)
    assert r["ok"] and "EDA-201" in r["message"]
    f = jira.created()[0]
    assert f["project"] == {"key": "EDA"} and f["issuetype"] == {"id": "10"}
    assert f["summary"].startswith("Do: Validate") and f["priority"] == {"id": "1"} and f["duedate"] == "2026-11-09"
    assert f["components"] == [{"id": "7"}] and set(f["labels"]) == {"delta", "meetingscribe"}
    assert f["assignee"] == {"name": "asha"}


def test_approve_records_it_and_updates_the_note(api, jira):
    review(api, jira)
    api.jira_approve(SID, 0)
    c = card(api, 0)
    assert c["status"] == "created" and c["result"] == "EDA-201"
    text = Path(json.loads((api.d / "session.json").read_text())["notes"]).read_text(encoding="utf-8")
    assert "EDA-201" in text and text.index("## Jira") < text.index("## Transcript")
    assert json.loads((api.d / "jira.json").read_text(encoding="utf-8"))["items"][0]["key"] == "EDA-201"


def test_a_double_click_creates_one_ticket(api, jira):
    review(api, jira)
    results = []
    threads = [threading.Thread(target=lambda: results.append(api.jira_approve(SID, 0))) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(jira.created()) == 1 and sum(r["ok"] for r in results) == 1


def test_no_means_nothing_reaches_jira_and_undo_puts_it_back(api, jira):
    review(api, jira)
    api.jira_dismiss(SID, 0)
    assert not api.jira_approve(SID, 0)["ok"] and jira.posts == []
    assert api.jira_dismiss(SID, 0, True)["ok"]
    assert api.jira_approve(SID, 0)["ok"]


def test_reading_the_meeting_again_cannot_create_a_duplicate(api, jira):
    review(api, jira)
    api.jira_approve(SID, 0)
    api.jira_start(SID, "EDA", True)
    assert card(api, 0)["status"] == "created"
    assert not api.jira_approve(SID, 0)["ok"] and len(jira.created()) == 1


def test_a_refused_ticket_stays_on_the_card_with_jiras_reason(api, jira):
    review(api, jira)
    jira.refuse = {"errors": {"summary": "Summary is too long"}}
    r = api.jira_approve(SID, 0)
    assert not r["ok"] and "Summary is too long" in r["message"] and card(api, 0)["status"] == "pending"
    jira.refuse = None
    assert api.jira_approve(SID, 0)["ok"]


def test_an_expired_sign_in_sends_you_to_connect(api, jira):
    review(api, jira)
    jira.token_ok = False
    r = api.jira_approve(SID, 0)
    assert not r["ok"] and r["needs_signin"] and card(api, 0)["status"] == "pending"


def test_no_answer_warns_it_may_have_gone_through(api, jira, monkeypatch):
    review(api, jira)
    real = ms.JiraClient.create

    def lost(self, fields):
        raise ms.JiraError("cannot reach Jira: timed out")
    monkeypatch.setattr(ms.JiraClient, "create", lost)
    r = api.jira_approve(SID, 0)
    assert not r["ok"] and "Check Jira before trying again" in r["message"]
    monkeypatch.setattr(ms.JiraClient, "create", real)


# -- editing


def test_an_edit_is_what_gets_created(api, jira):
    review(api, jira)
    assert api.jira_save_card(SID, 0, {"fields": {"summary": "My better title", "priority": "Low"}})["ok"]
    assert jira.posts == []
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert f["summary"] == "My better title" and f["priority"] == {"id": "2"}


def test_a_value_jira_does_not_allow_blocks_the_ticket(api, jira):
    review(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"priority": "Whenever"}})
    c = card(api, 0)
    assert c["ticket"]["blocked"] and any(r["problem"] for r in c["ticket"]["rows"])
    assert not api.jira_approve(SID, 0)["ok"] and jira.posts == []


def test_removing_a_required_field_blocks_the_ticket(api, jira):
    review(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"summary": ""}})
    assert card(api, 0)["ticket"]["blocked"]


def test_a_bad_date_blocks_the_ticket(api, jira):
    review(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"duedate": "next week"}})
    assert card(api, 0)["ticket"]["blocked"]


def test_clearing_an_optional_field_leaves_it_out(api, jira):
    review(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"priority": ""}})
    api.jira_approve(SID, 0)
    assert "priority" not in jira.created()[0]


def test_the_assignee_is_looked_up_in_jira(api, jira):
    review(api, jira)
    api.jira_save_card(SID, 0, {"assignee": "Dan Ortiz"})
    assert card(api, 0)["ticket"]["assignee"] == "Dan Ortiz"
    api.jira_save_card(SID, 0, {"assignee": "Nobody Here"})
    assert "not found" in card(api, 0)["ticket"]["assignee"]
    api.jira_approve(SID, 0)
    assert "assignee" not in jira.created()[0]


def test_changing_type_keeps_values_and_asks_for_newly_required_fields(api, jira):
    review(api, jira)
    summary = card(api, 0)["ticket"]["rows"][0]["value"]
    api.jira_save_card(SID, 0, {"type": "11"})
    c = card(api, 0)
    assert c["ticket"]["type"] == "Bug" and c["ticket"]["rows"][0]["value"] == summary and c["ticket"]["blocked"]
    api.jira_save_card(SID, 0, {"fields": {"environment": "PROD"}})
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert f["issuetype"] == {"id": "11"} and f["environment"] == "PROD"


def test_an_unknown_type_is_refused(api, jira):
    review(api, jira)
    assert not api.jira_save_card(SID, 0, {"type": "999"})["ok"]


def test_the_form_describes_each_field(api, jira):
    review(api, jira)
    form = {f["id"]: f for f in api.jira_card_form(SID, 0)["fields"]}
    assert form["summary"]["required"] and form["description"]["kind"] == "long"
    assert form["priority"]["kind"] == "select" and form["priority"]["options"] == ["High", "Low"]
    assert form["components"]["kind"] == "multi" and form["components"]["value"] == ["Validation"]
    assert form["duedate"]["kind"] == "date" and form["customfield_1"]["kind"] == "number" and form["customfield_1"]["more"]
    assert "customfield_2" not in form and "assignee" not in form


@pytest.mark.parametrize("edits", ["nope", 5, None, {"fields": "x"}, {"fields": {"summary": 5}}, {"fields": {"project": "HACK"}},
                                   {"fields": {"nosuchfield": "x"}}, {"fields": {"labels": [1, 2]}}])
def test_junk_from_the_page_changes_nothing(api, jira, edits):
    review(api, jira)
    before = card(api, 0)["ticket"]
    api.jira_save_card(SID, 0, edits)
    assert card(api, 0)["ticket"] == before


def test_a_handled_card_cannot_be_edited(api, jira):
    review(api, jira)
    api.jira_approve(SID, 0)
    assert not api.jira_save_card(SID, 0, {"fields": {"summary": "late"}})["ok"]


# -- comments and the other suggestions


def test_approve_posts_the_comment_and_does_not_create(api, jira):
    review(api, jira)
    r = api.jira_approve(SID, 1)
    assert r["ok"] and jira.created() == []
    path, body = jira.posts[0]
    assert path == "/issue/EDA-101/comment" and "Posted by MeetingScribe" in body["body"]
    assert card(api, 1)["status"] == "commented"


def test_an_edited_comment_is_what_gets_posted(api, jira):
    review(api, jira)
    assert api.jira_save_card(SID, 1, {"comment": "My words"})["ok"]
    api.jira_approve(SID, 1)
    assert jira.posts[0][1] == {"body": "My words"}


def test_an_empty_comment_is_refused(api, jira):
    review(api, jira)
    assert not api.jira_save_card(SID, 1, {"comment": "  "})["ok"]


def test_a_comment_can_become_a_new_ticket(api, jira):
    review(api, jira)
    assert api.jira_make_ticket(SID, 1)["ok"]
    assert card(api, 1)["ticket"]
    api.jira_approve(SID, 1)
    f = jira.created()[0]
    assert "Repoint" in f["summary"] and f["assignee"] == {"name": "dan"} and not [p for p, _ in jira.posts if "comment" in p]


def test_a_skipped_one_can_be_drafted_anyway_with_nobody_assigned(api, jira):
    review(api, jira)
    assert not api.jira_approve(SID, 2)["ok"] and jira.posts == []
    api.jira_make_ticket(SID, 2)
    assert card(api, 2)["ticket"]["assignee"].startswith("Unassigned")


def test_related_tickets_are_linked_to_the_new_one(api, jira, monkeypatch):
    connect(api, jira)

    class Related(FakeLLM):
        def chat(self, prompt, max_tokens=1500):
            if prompt.startswith("A meeting produced"):
                return json.dumps({"labels": {"EDA-101": "related", "OPS-9": "related"}, "reason": "Close."})
            return super().chat(prompt, max_tokens)
    monkeypatch.setattr(ms, "LLM", Related)
    api.jira_start(SID, "EDA")
    assert api.jira_approve(SID, 1)["ok"]
    link = next(b for p, b in jira.posts if p == "/issueLink")
    assert link["inwardIssue"] == {"key": "EDA-201"} and link["outwardIssue"] == {"key": "EDA-101"}


def test_text_in_other_scripts_survives(api, jira):
    connect(api, jira)
    CANDS[0]["evidence"] = "मैं जाँच करूँगा"
    try:
        api.jira_start(SID, "EDA")
        assert card(api, 0)["evidence"] == "मैं जाँच करूँगा"
        assert "जाँच" in (api.d / "jira.json").read_text(encoding="utf-8")
    finally:
        CANDS[0]["evidence"] = "Me: I'll validate the row counts"


def test_the_list_counts_what_is_waiting_and_done(api, jira):
    review(api, jira)
    api.jira_approve(SID, 0)
    row = api.jira_meetings()[0]
    assert row["to_review"] == 2 and row["in_jira"] == 1


def test_only_a_real_key_is_opened(api, jira, monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    connect(api, jira)
    for bad in ("javascript:alert(1)", "../x", 5, None):
        api.jira_open(bad)
    api.jira_open("EDA-1")
    assert opened == [f"{jira.url}/browse/EDA-1"]


# -- the terminal still works the same way


def test_the_terminal_command_still_creates_and_comments(api, jira, monkeypatch, capsys):
    connect(api, jira)

    answers = iter(["EDA", "y", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    args = argparse.Namespace(session=SID, project=None, refresh=False, dry_run=False, forget=False)
    ms.cmd_jira(api.cfg, args)
    out = capsys.readouterr().out
    assert "Created EDA-201" in out and "Commented on EDA-101" in out
    assert len(jira.created()) == 1 and [p for p, _ in jira.posts if p == "/issue/EDA-101/comment"]


# -- learned from the Jira skill that has been used on the real site


def test_a_comment_card_shows_what_it_follows_on_from(api, jira):
    c = review(api, jira)["cards"][1]
    assert c["latest"] == ["Dan Ortiz", "2026-11-02", "jobs still failing"]


def test_text_containing_the_sign_in_is_never_sent(api, jira):
    review(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"summary": "paste s3cr3t-tok oops"}})
    r = api.jira_approve(SID, 0)
    assert not r["ok"] and "sign-in" in r["message"] and jira.posts == [] and card(api, 0)["status"] == "pending"
    api.jira_save_card(SID, 1, {"comment": "the cookie is s3cr3t-tok"})
    assert not api.jira_approve(SID, 1)["ok"] and jira.posts == []


def test_the_window_is_hidden_from_the_page_bridge():
    public = [k for k in vars(app.Api()) if not k.startswith("_")]
    assert public == ["download"]


# -- learning how a space writes tickets


def past(i, kind="Task", **fields):
    return {"key": f"EDA-{i}", "fields": {"issuetype": {"name": kind}, "summary": f"Past ticket {i}", "labels": [],
                                          "description": "h3. Background\nx\nh3. Definition of done\ny", **fields}}


def team_history(n=20):
    return [past(i, customfield_3="PROD", customfield_4={"value": "Data"}, components=[{"name": "Reports"}],
                 labels=["team-x"], customfield_1=3, customfield_5=[{"id": 77, "name": "Sprint 9", "state": "active"}]) for i in range(n)]


class Capturing(FakeLLM):
    prompts = []

    def chat(self, prompt, max_tokens=1500):
        Capturing.prompts.append(prompt)
        return super().chat(prompt, max_tokens)


def learned(api, jira, monkeypatch=None):
    jira.history = team_history()
    return review(api, jira)


def test_learning_finds_what_the_team_always_sets(api, jira):
    learned(api, jira)
    habits = ms.load_space(api.cfg, "EDA")["types"]["Task"]
    assert habits["n"] == 20 and habits["defaults"]["customfield_3"] == "PROD" and habits["defaults"]["customfield_4"] == "Data"
    assert habits["defaults"]["components"] == ["Reports"] and habits["defaults"]["labels"] == ["team-x"]
    assert habits["headings"] == ["Background", "Definition of done"] and "customfield_1" in habits["usual"]


def test_learning_only_reads(api, jira):
    learned(api, jira)
    assert jira.posts == []


def test_it_never_learns_from_its_own_tickets(api, jira):
    jira.history = team_history() + [past(100 + i, labels=["meetingscribe"], customfield_3="WRONG") for i in range(40)]
    review(api, jira)
    assert ms.load_space(api.cfg, "EDA")["tickets"] == 20


def test_too_few_tickets_teach_nothing(api, jira):
    jira.history = team_history(3)
    review(api, jira)
    assert ms.load_space(api.cfg, "EDA")["types"] == {}
    assert card(api, 0)["ticket"]


def test_mixed_values_do_not_become_a_default(api, jira):
    jira.history = [past(i, customfield_4={"value": "Data" if i % 2 else "Ops"}) for i in range(20)]
    review(api, jira)
    habits = ms.load_space(api.cfg, "EDA")["types"]["Task"]
    assert "customfield_4" not in habits["defaults"] and "customfield_4" in habits["usual"]


def test_a_learned_default_lands_on_the_ticket(api, jira):
    learned(api, jira)
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert f["customfield_3"] == "PROD" and f["customfield_4"] == {"id": "31"} and "team-x" in f["labels"]


def test_the_model_still_decides_where_a_default_is_only_learned(api, jira):
    learned(api, jira)
    api.jira_approve(SID, 0)
    assert jira.created()[0]["components"] == [{"id": "7"}]


def test_a_correction_beats_the_model(api, jira):
    learned(api, jira)
    assert api.jira_habit_save("EDA", "Task", {"components": ["Reports", "Validation"]})["ok"]
    api.jira_start(SID, "EDA", True)
    api.jira_approve(SID, 0)
    assert jira.created()[0]["components"] == [{"id": "8"}, {"id": "7"}]


def test_the_prompt_gets_the_teams_style(api, jira, monkeypatch):
    Capturing.prompts = []
    monkeypatch.setattr(ms, "LLM", Capturing)
    learned(api, jira)
    fill = next(x for x in Capturing.prompts if x.startswith("Write a Jira"))
    assert '"h3. Background", "h3. Definition of done"' in fill and '"h3. What needs doing"' not in fill
    assert "Past ticket 0" in fill and "[customfield_1]" in fill


def test_with_nothing_learned_the_prompt_is_unchanged(api, jira, monkeypatch):
    Capturing.prompts = []
    monkeypatch.setattr(ms, "LLM", Capturing)
    review(api, jira)
    fill = next(x for x in Capturing.prompts if x.startswith("Write a Jira"))
    assert "How this team writes" not in fill and "[epic]" not in fill and '"h3. What needs doing"' in fill


def test_a_made_up_optional_value_does_not_block_the_ticket(api, jira, monkeypatch):
    class Invents(FakeLLM):
        def chat(self, prompt, max_tokens=1500):
            if prompt.startswith("Write a Jira"):
                return json.dumps({"summary": "S", "priority": "Medium", "components": ["Validation", "Nonsense"]})
            return super().chat(prompt, max_tokens)
    monkeypatch.setattr(ms, "LLM", Invents)
    c = review(api, jira)["cards"][0]
    assert not c["ticket"]["blocked"]
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert "priority" not in f and f["components"] == [{"id": "7"}]


def test_a_made_up_value_falls_back_to_the_learned_one(api, jira, monkeypatch):
    class Invents(FakeLLM):
        def chat(self, prompt, max_tokens=1500):
            if prompt.startswith("Write a Jira"):
                return json.dumps({"summary": "S", "priority": "Medium"})
            return super().chat(prompt, max_tokens)
    monkeypatch.setattr(ms, "LLM", Invents)
    jira.history = [past(i, priority={"name": "Low"}) for i in range(20)]
    review(api, jira)
    api.jira_approve(SID, 0)
    assert jira.created()[0]["priority"] == {"id": "2"}


def test_a_bad_required_value_is_still_shown_to_you(api, jira, monkeypatch):
    required = [dict(f, required=True) if f["fieldId"] == "priority" else f for f in FIELDS]
    monkeypatch.setattr(sys.modules[__name__], "FIELDS", required)

    class Invents(FakeLLM):
        def chat(self, prompt, max_tokens=1500):
            if prompt.startswith("Write a Jira"):
                return json.dumps({"summary": "S", "priority": "Medium"})
            return super().chat(prompt, max_tokens)
    monkeypatch.setattr(ms, "LLM", Invents)
    assert review(api, jira)["cards"][0]["ticket"]["blocked"]


class PicksEpic(FakeLLM):
    def chat(self, prompt, max_tokens=1500):
        if prompt.startswith("Write a Jira"):
            return json.dumps({"summary": "S", "epic": "EDA-5 Migration"})
        return super().chat(prompt, max_tokens)


def test_the_model_picks_an_epic_and_the_only_active_sprint_is_used(api, jira, monkeypatch):
    monkeypatch.setattr(ms, "LLM", PicksEpic)
    jira.epics = [past(5, "Epic", summary="Migration")]
    learned(api, jira)
    c = card(api, 0)
    assert {"name": "Epic", "value": "EDA-5", "problem": None} in c["ticket"]["rows"]
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert f["parent"] == {"key": "EDA-5"} and f["customfield_5"] == 77


def test_an_epic_the_space_does_not_have_is_ignored(api, jira, monkeypatch):
    class Bad(FakeLLM):
        def chat(self, prompt, max_tokens=1500):
            if prompt.startswith("Write a Jira"):
                return json.dumps({"summary": "S", "epic": "HACK-1"})
            return super().chat(prompt, max_tokens)
    monkeypatch.setattr(ms, "LLM", Bad)
    jira.epics = [past(5, "Epic", summary="Migration")]
    learned(api, jira)
    api.jira_approve(SID, 0)
    assert "parent" not in jira.created()[0]


def test_epic_and_sprint_can_be_changed_only_to_real_ones(api, jira):
    jira.epics = [past(5, "Epic", summary="Migration")]
    learned(api, jira)
    plan = api.jira_card_form(SID, 0)["plan"]
    assert plan["epic"]["options"][0]["value"] == "EDA-5" and plan["sprint"]["options"][0]["value"] == "77"
    api.jira_save_card(SID, 0, {"epic": "EDA-5", "sprint": ""})
    api.jira_save_card(SID, 0, {"epic": "HACK-9", "sprint": "999"})
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert f["parent"] == {"key": "EDA-5"} and "customfield_5" not in f


def test_server_style_epic_link_and_sprint_text(api, jira, monkeypatch):
    shaped = [dict(f, schema={"type": "any", "custom": "com.pyxis.greenhopper.jira:gh-epic-link"}, fieldId="customfield_9", name="Epic Link")
              if f["fieldId"] == "parent" else f for f in FIELDS]
    monkeypatch.setattr(sys.modules[__name__], "FIELDS", shaped)
    monkeypatch.setattr(ms, "LLM", PicksEpic)
    jira.epics = [past(5, "Epic", summary="Migration")]
    jira.history = [past(i, customfield_5=["com.atlassian.greenhopper.service.sprint.Sprint@1[id=42,rapidViewId=1,state=ACTIVE,name=S4]"]) for i in range(10)]
    review(api, jira)
    api.jira_approve(SID, 0)
    f = jira.created()[0]
    assert f["customfield_9"] == "EDA-5" and f["customfield_5"] == 42


def test_remembering_an_edit_applies_to_the_next_draft(api, jira):
    learned(api, jira)
    r = api.jira_save_card(SID, 0, {"fields": {"customfield_4": "Ops"}, "remember": True})
    assert "will do the same" in r["message"]
    api.jira_start(SID, "EDA", True)
    api.jira_approve(SID, 0)
    assert jira.created()[0]["customfield_4"] == {"id": "32"}


def test_an_edit_is_not_remembered_unless_you_say_so(api, jira):
    learned(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"customfield_4": "Ops"}})
    assert ms.load_space(api.cfg, "EDA")["custom"] == {}


def test_ticket_specific_fields_are_never_remembered(api, jira):
    learned(api, jira)
    api.jira_save_card(SID, 0, {"fields": {"summary": "One off", "description": "x", "duedate": "2026-12-01", "priority": "Low", "labels": "a"}, "remember": True})
    assert ms.load_space(api.cfg, "EDA")["custom"] == {}


def test_a_month_old_space_is_learned_again_and_a_recent_one_is_not(api, jira):
    learned(api, jira)
    count = lambda: sum(1 for j in jira.searches if j.startswith('project = "EDA" ORDER'))
    assert count() == 1
    api.jira_start(SID, "EDA")
    assert count() == 1
    space = ms.load_space(api.cfg, "EDA")
    space["learned_at"] = "2026-01-01T00:00:00"
    ms.save_space(api.cfg, "EDA", space)
    api.jira_start(SID, "EDA")
    assert count() == 2


def test_a_failed_lesson_does_not_stop_the_review(api, jira, monkeypatch):
    def boom(*a, **k):
        raise ms.JiraError("search is down")
    monkeypatch.setattr(ms, "learn_space", boom)
    assert review(api, jira)["cards"][0]["ticket"]


def test_learning_again_keeps_your_corrections(api, jira):
    learned(api, jira)
    api.jira_habit_save("EDA", "Task", {"customfield_3": "TEST"})
    assert api.jira_habit_learn("EDA")["ok"]
    rows = {r["id"]: r for r in api.jira_habits()[0]["types"][0]["fields"]}
    assert rows["customfield_3"]["value"] == "TEST" and rows["customfield_3"]["yours"]


def test_setting_a_correction_back_clears_it(api, jira):
    learned(api, jira)
    api.jira_habit_save("EDA", "Task", {"customfield_3": "TEST"})
    api.jira_habit_save("EDA", "Task", {"customfield_3": "PROD"})
    assert ms.load_space(api.cfg, "EDA")["custom"]["Task"] == {}


def test_clearing_a_learned_default_turns_it_off(api, jira):
    learned(api, jira)
    api.jira_habit_save("EDA", "Task", {"customfield_3": ""})
    api.jira_start(SID, "EDA", True)
    api.jira_approve(SID, 0)
    assert "customfield_3" not in jira.created()[0]


def test_a_correction_jira_would_refuse_is_refused(api, jira):
    learned(api, jira)
    assert not api.jira_habit_save("EDA", "Task", {"customfield_4": "Nope"})["ok"]


@pytest.mark.parametrize("values", ["x", None, 5, {"customfield_3": 5}, {"nosuch": "x"}, {"summary": "x"}])
def test_junk_corrections_change_nothing(api, jira, values):
    learned(api, jira)
    api.jira_habit_save("EDA", "Task", values)
    assert ms.load_space(api.cfg, "EDA")["custom"].get("Task", {}) == {}


def test_forgetting_a_space(api, jira):
    learned(api, jira)
    api.jira_habit_forget("EDA")
    assert api.jira_habits() == []


@pytest.mark.parametrize("project", ["../../x", "", None, 5, "a/b"])
def test_a_bad_space_name_is_refused(api, project):
    for fn in (api.jira_habit_learn, api.jira_habit_forget):
        with pytest.raises(ValueError):
            fn(project)


def test_changing_type_brings_that_types_defaults(api, jira):
    jira.history = team_history() + [past(50 + i, "Bug", customfield_3="QA") for i in range(10)]
    review(api, jira)
    api.jira_save_card(SID, 0, {"type": "11"})
    assert "QA" in [r["value"] for r in card(api, 0)["ticket"]["rows"]]


def test_a_kind_the_space_lacks_uses_its_busiest_type(api, jira, monkeypatch):
    CANDS[0]["kind"] = "Story"
    try:
        jira.history = [past(i, "Bug", environment="QA") for i in range(10)]
        assert review(api, jira)["cards"][0]["ticket"]["type"] == "Bug"
    finally:
        CANDS[0]["kind"] = "Task"


def test_the_learned_file_has_no_sign_in(api, jira):
    learned(api, jira)
    assert "s3cr3t-tok" not in ms.space_path(api.cfg, "EDA").read_text(encoding="utf-8")
