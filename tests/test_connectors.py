"""Slack, Teams, Gmail and Outlook: what is sent, where, and what can't go wrong silently.

    python -m pytest tests
"""

import http.server
import json
import ssl
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connectors as cn  # noqa: E402
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402

SLACK = "https://hooks.slack.com/services/T000/B000/secretsecret"
TEAMS = "https://prod-16.eastus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=secret"
NOTE = """# Migration sync

## Summary
- Migration **finished** last night.

## Action Items
- [ ] **Priya Shah**: update the reporting jobs (Thursday)
- [ ] **Priya Shah**: tell security
- [ ] **Me**: book the review
- [ ] **Speaker 3 (maybe Dan)**: raise the firewall change
- [ ] **Unassigned**: find an owner
- [ ] Nobody bold here

---
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(f'my_name = "Me"\nnotify = false\nwork_dir = {json.dumps(str(tmp_path / "w"))}\noutput_dir = {json.dumps(str(tmp_path / "n"))}\n', encoding="utf-8")
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(p))
    (tmp_path / "w" / "sessions" / "s1").mkdir(parents=True)
    (tmp_path / "n").mkdir()
    note = tmp_path / "n" / "m.md"
    note.write_text(NOTE, encoding="utf-8")
    (tmp_path / "w" / "sessions" / "s1" / "session.json").write_text(json.dumps(
        {"title": "Migration sync", "status": "done", "started_at": "2026-10-28T14:00:00", "notes": str(note)}), encoding="utf-8")
    vault, posts, opened = {}, [], []

    def keychain(action, account, secret=None, service=None):
        assert service == cn.KEY_SERVICE
        if action == "set":
            vault[account] = secret
        elif action == "delete":
            vault.pop(account, None)
        return vault.get(account) if action == "get" else None

    monkeypatch.setattr(ms, "keychain", keychain)
    monkeypatch.setattr(cn, "deliver", lambda url, payload, ctx: posts.append((url, payload)))
    monkeypatch.setattr(cn.webbrowser, "open", opened.append)

    class World:
        api = app.Api()
    World.vault, World.posts, World.opened = vault, posts, opened
    return World


def slack(w):
    assert w.api.connector_add_channel("slack", "#eng", SLACK)["ok"]
    w.posts.clear()
    return w.api.connector_state()["destinations"][0]["id"]


def send(w, did, kind="recap", owner="", text="hello", to="", slack_id="", again=False):
    return w.api.connector_send("s1", did, kind, owner, "Subject", text, to, slack_id, again)


# -- connecting


@pytest.mark.parametrize("service,url", [
    ("slack", "http://hooks.slack.com/services/a/b/c"), ("slack", "https://hooks.slack.com.evil.test/services/a"),
    ("slack", "https://evil.test/hooks.slack.com"), ("slack", "https://user:pw@hooks.slack.com/services/a"),
    ("slack", "https://hooks.slack.com:8443/services/a"), ("slack", TEAMS), ("teams", SLACK), ("teams", "https://logic.azure.com.evil.test/x"),
    ("gmail", SLACK), ("slack", "not a link"), ("slack", "https://[bad/x")])
def test_only_the_vendors_own_webhook_is_accepted(world, service, url):
    r = world.api.connector_add_channel(service, "x", url)
    assert not r["ok"] and not world.posts and not world.vault and url not in r["message"]


@pytest.mark.parametrize("url", [SLACK])
def test_a_channel_is_saved_after_its_test_message_and_the_link_stays_secret(world, url):
    assert world.api.connector_add_channel("slack", "#eng", url)["ok"]
    assert world.posts[0][0] == url and cn.TEST_MESSAGE in world.posts[0][1]["text"]
    assert list(world.vault.values()) == [url]
    state = world.api.connector_state()
    assert "secret" not in json.dumps(state) and "secret" not in (world.api.cfg["work_dir"] / "connectors.json").read_text()


def test_a_failed_test_message_saves_nothing(world, monkeypatch):
    def refuse(*a):
        raise cn.ConnectorError("That channel's link no longer works.")
    monkeypatch.setattr(cn, "deliver", refuse)
    assert not world.api.connector_add_channel("slack", "#eng", SLACK)["ok"]
    assert not world.vault and world.api.connector_state()["destinations"] == []


def test_removing_a_channel_forgets_its_link(world):
    did = slack(world)
    assert world.api.connector_remove(did)["ok"]
    assert not world.vault and world.api.connector_state()["destinations"] == []


# -- what is composed


def test_recap_and_follow_ups_come_from_the_note(world):
    c = world.api.connector_compose("s1")
    assert "Migration finished last night" in c["recap"] and "**" not in c["recap"] and "Wednesday 28 October 2026" in c["recap"]
    assert [f["owner"] for f in c["followups"]] == ["Priya Shah"]
    assert "update the reporting jobs" in c["followups"][0]["text"] and "tell security" in c["followups"][0]["text"]
    assert "book the review" not in c["followups"][0]["text"]
    assert c["no_owner"] == 3


def test_no_address_is_invented(world):
    c = world.api.connector_compose("s1")
    assert c["to"] == "" and c["followups"][0]["email"] == "" and c["followups"][0]["slack"] == ""


def test_an_address_used_once_is_remembered(world):
    world.api.connector_set_email("gmail")
    assert send(world, "gmail", "followup", "Priya Shah", to="priya@x.test")["ok"]
    c = world.api.connector_compose("s1")
    assert c["followups"][0]["email"] == "priya@x.test" and c["to"] == "priya@x.test"


# -- sending


def test_nothing_is_sent_by_composing(world):
    slack(world)
    world.api.connector_compose("s1")
    assert not world.posts and not world.opened


def test_the_edited_text_is_what_goes(world):
    did = slack(world)
    assert send(world, did, text="my own words")["ok"]
    assert world.posts[0] == (SLACK, {"text": "my own words", "unfurl_links": False, "unfurl_media": False})


def test_sending_twice_needs_asking(world):
    did = slack(world)
    assert send(world, did)["ok"]
    r = send(world, did)
    assert not r["ok"] and "already" in r["message"] and len(world.posts) == 1
    assert send(world, did, again=True)["ok"] and len(world.posts) == 2


def test_a_failed_send_is_not_recorded(world, monkeypatch):
    did = slack(world)

    def refuse(*a):
        raise cn.ConnectorError("no")
    monkeypatch.setattr(cn, "deliver", refuse)
    assert not send(world, did)["ok"]
    monkeypatch.setattr(cn, "deliver", lambda *a: None)
    assert send(world, did)["ok"]


def test_follow_ups_are_tracked_per_person(world):
    did = slack(world)
    assert send(world, did, "followup", "Priya")["ok"] and send(world, did, "followup", "Dan")["ok"]


def test_unknown_destination_and_meeting_are_refused(world):
    assert not send(world, "slack-nope")["ok"]
    with pytest.raises(ValueError):
        world.api.connector_send("../../etc", "x", "recap", "", "", "t")


def test_a_spoken_line_cannot_ping_the_channel(world):
    payload = cn.slack_payload("<!channel> @here & <https://evil.test|click> @everyone")
    assert "<" not in payload["text"] and "&lt;!channel&gt;" in payload["text"] and "&amp;" in payload["text"]
    assert "@here" not in payload["text"] and "@everyone" not in payload["text"]


def test_a_follow_up_mentions_only_a_saved_id(world):
    did = slack(world)
    assert send(world, did, "followup", "Priya", slack_id="U012AB3CD")["ok"]
    assert world.posts[0][1]["text"].startswith("<@U012AB3CD> ")
    r = send(world, did, "followup", "Dan", slack_id="<!channel>")
    assert not r["ok"] and len(world.posts) == 1


def test_teams_gets_a_card_with_one_block_per_line(world):
    p = cn.teams_payload("One\n\n- two [x](https://evil.test)")
    att = p["attachments"][0]
    assert p["type"] == "message" and att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert [b["text"] for b in att["content"]["body"]] == ["One", "- two [x] (https://evil.test)"]


def test_too_long_for_a_channel_is_refused(world):
    did = slack(world)
    r = send(world, did, text="x" * (cn.MAX_CHANNEL_TEXT + 5))
    assert not r["ok"] and "Shorten" in r["message"] and not world.posts


# -- drafts


def test_gmail_opens_a_draft_and_sends_nothing(world):
    world.api.connector_set_email("gmail")
    r = send(world, "gmail", text="line one\nline two", to="a@x.test, b@x.test")
    assert r["ok"] and "Press Send" in r["message"] and not world.posts
    u = urllib.parse.urlsplit(world.opened[0])
    q = urllib.parse.parse_qs(u.query)
    assert u.netloc == "mail.google.com" and q["to"] == ["a@x.test,b@x.test"] and q["su"] == ["Subject"] and q["body"] == ["line one\nline two"]


@pytest.mark.parametrize("account,host", [("work", "outlook.office.com"), ("personal", "outlook.live.com")])
def test_outlook_uses_the_account_chosen(world, account, host):
    world.api.connector_set_email("outlook", account)
    assert send(world, "outlook", text="two words", to="a@x.test;b@x.test")["ok"]
    assert world.opened[0].startswith(f"https://{host}/mail/deeplink/compose?") and "two%20words" in world.opened[0] and "+" not in world.opened[0]


@pytest.mark.parametrize("bad", ["not-an-address", "a@x.test\nBcc: b@x.test", "<a@x.test>"])
def test_bad_addresses_are_refused(world, bad):
    world.api.connector_set_email("gmail")
    assert not send(world, "gmail", to=bad)["ok"] and not world.opened


def test_a_draft_too_long_for_a_link_says_so(world):
    world.api.connector_set_email("gmail")
    r = send(world, "gmail", text="x" * 7000)
    assert not r["ok"] and "too long" in r["message"] and not world.opened


# -- the wire


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(Hook.status)
        if Hook.status == 302:
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/stolen")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def hook():
    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    Hook.got = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/services/SECRET"
    server.shutdown()


@pytest.mark.parametrize("status", [200, 202])
def test_deliver_posts_json(hook, status):
    Hook.status = status
    cn.deliver(hook, {"text": "hi"}, ssl.create_default_context())
    assert Hook.got == [{"text": "hi"}]


@pytest.mark.parametrize("status,words", [(404, "no longer works"), (410, "no longer works"), (429, "slow down"), (500, "500"), (302, "302")])
def test_deliver_errors_never_show_the_link_or_follow_it(hook, status, words):
    Hook.status = status
    with pytest.raises(cn.ConnectorError) as e:
        cn.deliver(hook, {"text": "hi"}, ssl.create_default_context())
    assert words in str(e.value) and "SECRET" not in str(e.value) and len(Hook.got) == 1


def test_deliver_says_when_it_cannot_connect():
    with pytest.raises(cn.ConnectorError, match="Couldn't reach"):
        cn.deliver("http://127.0.0.1:9/x", {}, ssl.create_default_context())
