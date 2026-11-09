"""Company AI: what is sent to each provider, what is never sent, and that nothing leaves without agreement.

    python -m pytest tests
"""

import io
import json
import logging
import ssl
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_llm  # noqa: E402
import meeting_scribe as ms  # noqa: E402
import meetingscribe_app as app  # noqa: E402

KEY = "sk-secret-key-123"
META = {"title": "Sync", "source": "Zoom", "started_at": "2026-11-08T10:00:00+05:30", "duration_s": 600, "id": "s1"}


class Answer:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def server(monkeypatch, *replies):
    """urlopen answers with these in turn: a dict, or an HTTP status to fail with."""
    calls, replies = [], list(replies)

    def urlopen(req, timeout=None, context=None):
        calls.append(req)
        reply = replies.pop(0)
        if isinstance(reply, tuple):
            raise urllib.error.HTTPError(req.full_url, reply[0], "no", {"retry-after": "1"}, io.BytesIO(reply[1].encode()))
        return Answer(reply)
    monkeypatch.setattr(cloud_llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(cloud_llm.time, "sleep", lambda _s: None)
    return calls


def client(provider, **settings):
    return cloud_llm.make_client({"provider": provider, **settings}, KEY if cloud_llm.PROVIDERS[provider]["key"] else None)


class FakeClaude:
    """Stands in for the SDK client and records the request."""

    def __init__(self, reply="Notes", stop_reason="end_turn"):
        self.requests = []
        result = NS(stop_reason=stop_reason, content=[NS(type="thinking", thinking="hm"), NS(type="text", text=reply)])

        def create(**kw):
            self.requests.append(kw)
            return result
        self.messages = NS(create=create)
        self.beta = NS(messages=NS(create=create))


def claude(monkeypatch, provider="anthropic", fake=None, **settings):
    fake = fake or FakeClaude()
    monkeypatch.setattr(cloud_llm.ApiClient, "claude_client", lambda self: fake)
    return client(provider, **settings), fake


# -- Claude


def test_claude_request(monkeypatch):
    c, fake = claude(monkeypatch)
    assert c.chat("sys", "hello", 1500) == "Notes"  # thinking blocks are not part of the answer
    r = fake.requests[0]
    assert r["model"] == "claude-opus-5-5" and r["system"] == "sys" and r["messages"][0]["content"] == "hello"
    assert "temperature" not in r  # a 400 on current models
    assert r["fallbacks"] == "default" and r["betas"] == [cloud_llm.FALLBACK_BETA]


def test_a_five_token_question_leaves_room_for_thinking(monkeypatch):
    c, fake = claude(monkeypatch)
    c.chat("sys", "yes or no", 5)
    assert fake.requests[0]["max_tokens"] >= 4096


@pytest.mark.parametrize("provider,settings", [("bedrock", {"region": "us-east-1"}), ("vertex", {"project": "p", "region": "global"}),
                                               ("foundry", {"resource": "r"})])
def test_fallback_is_only_for_the_first_party_api(monkeypatch, provider, settings):
    c, fake = claude(monkeypatch, provider, **settings)
    c.chat("sys", "hello", 100)
    assert "fallbacks" not in fake.requests[0] and "betas" not in fake.requests[0]


def test_fallback_is_not_sent_to_a_model_that_refuses_it(monkeypatch):
    c, fake = claude(monkeypatch, model="claude-haiku-5-5")
    c.chat("sys", "hello", 100)
    assert "fallbacks" not in fake.requests[0]


def test_refusal_is_an_error_not_a_note(monkeypatch):
    c, _ = claude(monkeypatch, fake=FakeClaude(reply="", stop_reason="refusal"))
    with pytest.raises(cloud_llm.CloudError, match="declined"):
        c.chat("sys", "hello", 100)


def test_real_sdk_clients_can_be_built():
    ctx = ssl.create_default_context()
    for provider, settings in [("anthropic", {}), ("foundry", {"resource": "r"})]:
        assert cloud_llm.make_client({"provider": provider, **settings}, KEY, ctx).claude


# -- Gemini and gateways


def test_gemini_request_keeps_the_key_out_of_the_address(monkeypatch):
    calls = server(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "plan", "thought": True}, {"text": "Notes"}]}}]})
    assert client("gemini", model="gemini-x").chat("sys", "hello", 100) == "Notes"
    req = calls[0]
    assert KEY not in req.full_url and req.get_header("X-goog-api-key") == KEY and "/models/gemini-x:generateContent" in req.full_url
    body = json.loads(req.data)
    assert body["systemInstruction"]["parts"][0]["text"] == "sys" and body["generationConfig"]["maxOutputTokens"] >= 4096


def test_gemini_blocked_content_is_an_error(monkeypatch):
    server(monkeypatch, {"promptFeedback": {"blockReason": "OTHER"}})
    with pytest.raises(cloud_llm.CloudError, match="no text"):
        client("gemini", model="m").chat("sys", "hello", 100)


def test_gemini_on_google_cloud_uses_the_sign_in(monkeypatch):
    calls = server(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "Notes"}]}}]})
    monkeypatch.setattr(cloud_llm, "google_token", lambda: "tok")
    client("gemini-vertex", model="m", project="acme", region="europe-west4").chat("sys", "hello", 100)
    assert calls[0].full_url.startswith("https://europe-west4-aiplatform.googleapis.com/v1/projects/acme/locations/europe-west4/")
    assert calls[0].get_header("Authorization") == "Bearer tok"


def gateway(**kw):
    return client("openai", model="m", base_url="https://ai.acme.com/v1", **kw)


OK = {"choices": [{"message": {"content": "Notes"}}]}


def test_gateway_request(monkeypatch):
    calls = server(monkeypatch, OK)
    assert gateway().chat("sys", "hello", 100) == "Notes"
    assert calls[0].full_url == "https://ai.acme.com/v1/chat/completions" and calls[0].get_header("Authorization") == "Bearer " + KEY


def test_azure_uses_its_own_header(monkeypatch):
    calls = server(monkeypatch, OK)
    client("openai", model="m", base_url="https://x.openai.azure.com/openai/v1").chat("sys", "hello", 100)
    assert calls[0].get_header("Api-key") == KEY and calls[0].get_header("Authorization") is None


def test_gateway_adapts_to_what_a_model_refuses(monkeypatch):
    calls = server(monkeypatch, (400, "unsupported parameter max_tokens"), (400, "temperature must be 1"), OK)
    assert gateway().chat("sys", "hello", 100) == "Notes"
    last = json.loads(calls[-1].data)
    assert "max_tokens" not in last and "temperature" not in last and last["max_completion_tokens"] >= 4096


def test_rate_limit_is_retried(monkeypatch):
    calls = server(monkeypatch, (429, "slow down"), (503, "busy"), OK)
    assert gateway().chat("sys", "hello", 100) == "Notes" and len(calls) == 3


def test_a_wrong_key_is_not_retried(monkeypatch):
    calls = server(monkeypatch, (401, "bad key"))
    with pytest.raises(cloud_llm.CloudError, match="401"):
        gateway().chat("sys", "hello", 100)
    assert len(calls) == 1


# -- settings that must be refused


@pytest.mark.parametrize("settings,why", [
    ({"provider": "openai", "model": "m", "base_url": "http://ai.acme.com/v1"}, "https"),
    ({"provider": "gemini"}, "model"),
    ({"provider": "bedrock"}, "region"),
    ({"provider": "nope"}, "unknown"),
    ({"provider": "claude-cli", "model": "x & calc.exe"}, "model name"),
])
def test_bad_settings(settings, why):
    with pytest.raises(ValueError, match=why):
        cloud_llm.make_client(settings, KEY)


def test_a_missing_key_is_refused():
    with pytest.raises(ValueError, match="API key"):
        cloud_llm.make_client({"provider": "anthropic"}, None)


# -- the vendors' own commands


@pytest.fixture
def cli(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        folder = Path(kw["cwd"])
        seen.update(cmd=cmd, kw=kw, files={p.name: p.read_text(encoding="utf-8") for p in folder.iterdir()})
        return subprocess.CompletedProcess(cmd, seen.get("code", 0), seen.get("out", '{"result": "Notes", "response": "Notes"}'), "stderr text")
    monkeypatch.setattr(cloud_llm.subprocess, "run", run)
    monkeypatch.setattr(cloud_llm, "find_command", lambda name: f"/bin/{name}")
    return seen


TOOLS_OFF = {"claude-cli": ["--tools", ""], "gemini-cli": ["--policy"], "copilot-cli": ["--available-tools=none-at-all"]}


@pytest.mark.parametrize("provider", list(TOOLS_OFF))
def test_commands_run_with_tools_off_and_the_transcript_on_stdin(cli, provider):
    settings = {"project": "p"} if provider == "gemini-cli" else {}
    cli["out"] = "Notes" if provider == "copilot-cli" else cli.get("out", '{"result": "Notes", "response": "Notes"}')
    assert client(provider, **settings).chat("sys", "SECRET TRANSCRIPT", 100) == "Notes"
    argv = cli["cmd"]
    assert all(flag in argv for flag in TOOLS_OFF[provider])
    assert "SECRET TRANSCRIPT" in cli["kw"]["input"] and not any("SECRET" in a for a in argv)
    assert set(cli["files"]) <= {"system.txt", "deny-all.toml"}  # an empty folder, apart from our own file


def test_claude_command_skips_personal_instructions(cli):
    client("claude-cli").chat("sys", "hello", 100)
    assert "--safe-mode" in cli["cmd"] and "--no-session-persistence" in cli["cmd"] and cli["files"]["system.txt"] == "sys"


def test_gemini_policy_denies_everything(cli):
    client("gemini-cli", project="acme").chat("sys", "hello", 100)
    policy = cli["files"]["deny-all.toml"]
    assert 'toolName = "*"' in policy and 'decision = "deny"' in policy
    assert cli["kw"]["env"]["GOOGLE_CLOUD_PROJECT"] == "acme"
    assert cli["cmd"][cli["cmd"].index("--policy") + 1].endswith("deny-all.toml")


def test_a_failed_command_says_to_sign_in(cli):
    cli["code"] = 1
    with pytest.raises(cloud_llm.CloudError, match="sign in"):
        client("claude-cli").chat("sys", "hello", 100)


def test_a_command_reporting_an_error_is_not_taken_as_notes(cli):
    cli["out"] = '{"is_error": true, "result": "Not logged in"}'
    with pytest.raises(cloud_llm.CloudError, match="sign in"):
        client("claude-cli").chat("sys", "hello", 100)


def test_a_missing_command_is_explained(monkeypatch):
    monkeypatch.setattr(cloud_llm, "find_command", lambda name: None)
    with pytest.raises(cloud_llm.CloudError, match="not found"):
        client("copilot-cli")


# -- agreement, keys and the pipeline


@pytest.fixture
def cfg(tmp_path):
    c = {**ms.DEFAULTS, "work_dir": tmp_path / "work", "output_dir": tmp_path / "notes", "log_dir": tmp_path / "logs",
         "jira": dict(ms.JIRA_DEFAULTS), "notify": False, "llm_backend": "cloud",
         "cloud_llm": {**ms.CLOUD_DEFAULTS, "provider": "openai", "model": "m", "base_url": "https://ai.acme.com/v1"}}
    c["work_dir"].mkdir()
    c["output_dir"].mkdir()
    return c


@pytest.fixture
def keys(monkeypatch):
    store = {}

    def keychain(action, account, secret=None, service=None):
        assert service == cloud_llm.KEY_SERVICE
        if action == "set":
            store[account] = secret
        elif action == "delete":
            store.pop(account, None)
        return store.get(account) if action == "get" else None
    monkeypatch.setattr(ms, "keychain", keychain)
    return store


def no_network(monkeypatch):
    monkeypatch.setattr(cloud_llm.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("network used"))


def test_nothing_is_sent_without_agreement_even_if_the_file_was_edited(cfg, monkeypatch, keys):
    no_network(monkeypatch)
    keys["openai"] = KEY
    with pytest.raises(cloud_llm.CloudError, match="agreed"):
        ms.LLM(cfg)


def test_agreeing_to_one_provider_does_not_cover_another(cfg, monkeypatch, keys):
    no_network(monkeypatch)
    keys["openai"] = KEY
    ms.record_ai_choice(cfg, "gemini")
    with pytest.raises(cloud_llm.CloudError, match="agreed"):
        ms.LLM(cfg)


def test_without_agreement_the_meeting_still_gets_a_basic_note(cfg, monkeypatch, keys):
    no_network(monkeypatch)
    keys["openai"] = KEY
    turns = [{"start": 0.0, "end": 1.0, "speaker": "Me", "text": " ".join(["word"] * 60)}]
    assert "basic note" in ms.write_note(cfg, dict(META), turns, {}).read_text(encoding="utf-8")


def agreed_llm(cfg, keys):
    keys["openai"] = KEY
    ms.record_ai_choice(cfg, "openai")
    return ms.LLM(cfg)


def test_provider_failure_gives_a_basic_note_and_says_why(cfg, monkeypatch, keys):
    server(monkeypatch, (401, "bad key"))
    told = []
    monkeypatch.setattr(ms, "notify", lambda _c, text: told.append(text))
    turns = [{"start": 0.0, "end": 1.0, "speaker": "Me", "text": " ".join(["word"] * 60)}]
    keys["openai"] = KEY
    ms.record_ai_choice(cfg, "openai")
    assert "basic note" in ms.write_note(cfg, dict(META), turns, {}).read_text(encoding="utf-8")
    assert "401" in told[0]


def test_the_note_says_who_wrote_it(cfg, monkeypatch, keys):
    server(monkeypatch, {"choices": [{"message": {"content": "## Summary\n- x\n\n## Action Items\n- y"}}]})
    keys["openai"] = KEY
    ms.record_ai_choice(cfg, "openai")
    turns = [{"start": 0.0, "end": 1.0, "speaker": "Me", "text": "We ship on Friday."}]
    text = ms.write_note(cfg, dict(META), turns, {}).read_text(encoding="utf-8")
    assert "summarized by company AI" in text and "summarized on this" not in text


def test_failed_confirm_call_keeps_every_line(cfg, monkeypatch, keys):
    llm = agreed_llm(cfg, keys)
    server(monkeypatch, {"choices": [{"message": {"content": "OFF: 1"}}]}, (401, "bad key"))
    segments = [{"start": 0.0, "speaker": "Me", "text": "nice weather"}]
    with pytest.raises(cloud_llm.CloudError):
        ms.work_only(cfg, llm, segments)  # process_session catches this and keeps every line


def test_work_filter_runs_in_parallel_and_keeps_order(cfg):
    class Fake:
        parallel = True

        def chat(self, text, n):
            if n == 400:
                return "OFF: 1, 2, 3"
            return "D" if ">>> Me: drop" in text else "A"
    segments = [{"start": float(i), "speaker": "Me", "text": t} for i, t in enumerate(["drop", "keep", "drop", "work"])]
    kept, dropped = ms.work_only(cfg, Fake(), segments)
    assert [s["text"] for s in kept] == ["keep", "work"] and dropped == 2


# -- the desktop app


@pytest.fixture
def api(tmp_path, monkeypatch, keys):
    p = tmp_path / "config.toml"
    p.write_text(f'work_dir = {json.dumps(str(tmp_path / "w"))}\noutput_dir = {json.dumps(str(tmp_path / "n"))}\n\n[jira]\nbase_url = ""\n', encoding="utf-8")
    monkeypatch.setenv("MEETINGSCRIBE_CONFIG", str(p))
    (tmp_path / "w").mkdir()
    return app.Api()


GATEWAY = {"provider": "openai", "model": "m", "base_url": "https://ai.acme.com/v1"}


def test_new_computer_is_asked(api):
    assert api.setup_state()["ai_chosen"] is False


def test_someone_with_local_models_sees_nothing_new(api):
    cfg = api.cfg
    (cfg["work_dir"] / "models_ready.json").write_text(json.dumps(ms.model_plan(cfg)))
    assert api.setup_state()["ai_chosen"] is True


def test_save_needs_agreement(api, monkeypatch):
    no_network(monkeypatch)
    assert not api.cloud_save(GATEWAY, KEY, False)["ok"]
    assert api.cfg["llm_backend"] != "cloud"


def test_a_failed_test_changes_nothing(api, monkeypatch, keys):
    server(monkeypatch, (401, "bad key"))
    r = api.cloud_save(GATEWAY, KEY, True)
    assert not r["ok"] and "401" in r["message"]
    assert api.cfg["llm_backend"] != "cloud" and not keys and not ms.ai_choice(api.cfg)


def test_saving_switches_and_the_key_stays_out_of_files_page_and_logs(api, monkeypatch, keys, caplog):
    server(monkeypatch, OK, OK)
    with caplog.at_level(logging.DEBUG, logger="meetingscribe"):
        assert api.cloud_save(GATEWAY, KEY, True)["ok"]
    cfg = api.cfg
    assert cfg["llm_backend"] == "cloud" and cfg["cloud_llm"]["model"] == "m" and keys["openai"] == KEY
    assert ms.ai_choice(cfg)["provider"] == "openai" and ms.LLM(cfg).chat("hello") == "Notes"
    assert KEY not in ms.config_path().read_text(encoding="utf-8")
    assert KEY not in json.dumps(api.cloud_state()) and KEY not in caplog.text


def test_models_are_not_downloaded_again_after_switching_to_company_ai(api, monkeypatch):
    cfg = api.cfg
    (cfg["work_dir"] / "models_ready.json").write_text(json.dumps(ms.model_plan({**cfg, "llm_backend": "llamacpp"})))
    server(monkeypatch, OK)
    api.cloud_save(GATEWAY, KEY, True)
    assert api.setup_state()["models_ok"] is True and not any("gguf" in str(x).lower() for x in ms.model_plan(api.cfg))


def test_going_back_to_local_and_forgetting_the_key(api, monkeypatch, keys):
    server(monkeypatch, OK)
    api.cloud_save(GATEWAY, KEY, True)
    api.use_local(True)
    assert api.cfg["llm_backend"] == ms.plat.MODEL_DEFAULTS["llm_backend"] and not keys and ms.ai_choice(api.cfg)["provider"] == "local"
