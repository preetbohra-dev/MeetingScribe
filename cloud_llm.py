"""Company-provided AI for writing notes, for people who don't want a local model.

Only the text of a prompt is sent. Audio, voiceprints and screen pictures stay on the computer.
Nothing here runs unless the user picked a provider and agreed to send it transcripts.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("meetingscribe")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
KEY_SERVICE = "MeetingScribe AI"
CLAUDE_MODEL = "claude-opus-5-5"
# Models that accept the API's "default" refusal fallback; others answer 400 if it is sent.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SEAT_NOTE = "Uses your own signed-in {} command. Check that your company's plan allows other apps to run it."
UNTESTED = "Not yet tested against a real account."

# fields: what the user must fill in. key: whether a secret is stored. model: the default, "" = ask.
PROVIDERS = {
    "anthropic": {"label": "Claude: Anthropic API key", "key": True, "model": CLAUDE_MODEL, "fields": [], "optional": ["base_url"]},
    "bedrock": {"label": "Claude: Amazon Bedrock", "key": False, "model": "anthropic." + CLAUDE_MODEL, "fields": ["region"]},
    "vertex": {"label": "Claude: Google Cloud", "key": False, "model": CLAUDE_MODEL, "fields": ["project", "region"]},
    "foundry": {"label": "Claude: Microsoft Foundry", "key": True, "model": CLAUDE_MODEL, "fields": ["resource"]},
    "gemini": {"label": "Gemini: API key", "key": True, "model": "", "fields": []},
    "gemini-vertex": {"label": "Gemini: Google Cloud", "key": False, "model": "", "fields": ["project", "region"]},
    "openai": {"label": "Company gateway (OpenAI-compatible)", "key": True, "model": "", "fields": ["base_url"]},
    "claude-cli": {"label": "Claude seat: signed-in claude command", "key": False, "model": "", "fields": [],
                   "note": SEAT_NOTE.format("claude")},
    "gemini-cli": {"label": "Gemini seat: signed-in gemini command", "key": False, "model": "", "fields": ["project"],
                   "note": SEAT_NOTE.format("gemini") + " " + UNTESTED},
    "copilot-cli": {"label": "Copilot seat: signed-in copilot command", "key": False, "model": "", "fields": [],
                    "note": SEAT_NOTE.format("copilot") + " " + UNTESTED},
}


class CloudError(Exception):
    """A provider problem, worded so it can be shown to the user."""


def https_only(url):
    """Keys and transcripts only travel over HTTPS. Plain http is allowed for this computer alone."""
    u = urllib.parse.urlparse(url)
    return bool(u.hostname) and (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")))


def check_settings(s):
    """Raises ValueError if these settings can't be used. CLI arguments go through cmd.exe on
    Windows, so the model name is held to the characters a model name really has."""
    p = PROVIDERS.get(s.get("provider"))
    if not p:
        raise ValueError(f"unknown AI provider {s.get('provider')!r}")
    model = s.get("model") or p["model"]
    if not re.fullmatch(r"[\w.:/@-]*", model):
        raise ValueError("That is not a model name.")
    if not model and not s["provider"].endswith("-cli"):
        raise ValueError("Enter the model to use.")
    for field in p["fields"]:
        if not s.get(field):
            raise ValueError(f"Enter the {field.replace('_', ' ')}.")
    if s.get("base_url") and not https_only(s["base_url"]):
        raise ValueError("The address must start with https:// so your key and transcripts are encrypted.")


def make_client(s, key=None, ctx=None):
    """A client for settings s. key is the stored secret, ctx the TLS context."""
    check_settings(s)
    if PROVIDERS[s["provider"]]["key"] and not key:
        raise ValueError("Enter your API key.")
    return CliClient(s) if s["provider"].endswith("-cli") else ApiClient(s, key, ctx)


def room(max_tokens):
    """Reasoning models spend max_tokens on thinking too; the app's 5-token yes/no calls would come back empty."""
    return max(max_tokens * 4, 4096)


class ApiClient:
    def __init__(self, s, key, ctx):
        self.s, self.key, self.ctx = s, key, ctx
        self.provider = s["provider"]
        self.model = s.get("model") or PROVIDERS[self.provider]["model"]
        self.claude = None
        if self.provider in ("anthropic", "bedrock", "vertex", "foundry"):
            try:
                self.claude = self.claude_client()
            except Exception as e:  # no AWS or Google sign-in on this computer, usually
                raise CloudError(f"{PROVIDERS[self.provider]['label']}: {type(e).__name__}: {str(e)[:300]}") from e

    def chat(self, system, user, max_tokens):
        try:
            if self.claude:
                return self.chat_claude(system, user, max_tokens)
            if self.provider == "openai":
                return self.chat_openai(system, user, max_tokens)
            return self.chat_gemini(system, user, max_tokens)
        except CloudError:
            raise
        except Exception as e:  # an SDK or sign-in error; its text never holds the key
            raise CloudError(f"{PROVIDERS[self.provider]['label']}: {type(e).__name__}: {str(e)[:300]}") from e

    def claude_client(self):
        import anthropic
        common = {"max_retries": 4, "timeout": 600.0, "http_client": anthropic.DefaultHttpxClient(verify=self.ctx or True)}
        s = self.s
        if self.provider == "bedrock":
            return anthropic.AnthropicBedrockMantle(aws_region=s["region"], **common)
        if self.provider == "vertex":
            return anthropic.AnthropicVertex(project_id=s["project"], region=s["region"], **common)
        if self.provider == "foundry":
            return anthropic.AnthropicFoundry(api_key=self.key, resource=s["resource"], **common)
        return anthropic.Anthropic(api_key=self.key, base_url=s.get("base_url") or None, **common)

    def chat_claude(self, system, user, max_tokens):
        # No temperature: current Claude models answer 400 to it.
        request = {"model": self.model, "max_tokens": room(max_tokens), "system": system,
                   "messages": [{"role": "user", "content": user}]}
        if self.provider == "anthropic" and self.model in FALLBACK_MODELS and not self.s.get("base_url"):
            r = self.claude.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
        else:
            r = self.claude.messages.create(**request)
        if r.stop_reason == "refusal":
            raise CloudError("Claude declined to process this text.")
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        if not text:
            raise CloudError(f"Claude returned no text (stop reason {r.stop_reason}).")
        return text

    def chat_gemini(self, system, user, max_tokens):
        s, path = self.s, f"/models/{urllib.parse.quote(self.model, safe='')}:generateContent"
        if self.provider == "gemini":
            url, headers = "https://generativelanguage.googleapis.com/v1beta" + path, {"x-goog-api-key": self.key}
        else:
            host = "aiplatform.googleapis.com" if s["region"] == "global" else f"{s['region']}-aiplatform.googleapis.com"
            url = (f"https://{host}/v1/projects/{urllib.parse.quote(s['project'], safe='')}/locations/"
                   f"{urllib.parse.quote(s['region'], safe='')}/publishers/google{path}")
            headers = {"Authorization": "Bearer " + google_token()}
        answer = self.post(url, headers, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": room(max_tokens)}})
        parts = ((answer.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            raise CloudError("Gemini returned no text; it may have blocked this meeting's content.")
        return text

    def chat_openai(self, system, user, max_tokens):
        base = self.s["base_url"].rstrip("/")
        azure = (urllib.parse.urlparse(base).hostname or "").endswith(".azure.com")
        headers = {"api-key": self.key} if azure else {"Authorization": "Bearer " + self.key}
        body = {"model": self.model, "temperature": 0.2, "max_tokens": room(max_tokens),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        # Gateways differ, and some models refuse these two settings, so one is changed and tried again.
        for _ in range(3):
            try:
                answer = self.post(base + "/chat/completions", headers, body)
                break
            except CloudError as e:
                if "max_tokens" in str(e) and "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                elif "temperature" in str(e) and "temperature" in body:
                    del body["temperature"]
                else:
                    raise
        else:
            raise CloudError("The company gateway refused every form of the request.")
        text = ((answer.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not text or not text.strip():
            raise CloudError("The company gateway returned no text.")
        return text.strip()

    def post(self, url, headers, body):
        """POST JSON, retrying the temporary failures: rate limits, server errors, a dropped connection."""
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "MeetingScribe/1.0", **headers}
        for attempt in range(5):
            wait = 2 ** attempt
            try:
                req = urllib.request.Request(url, data, headers, method="POST")
                with urllib.request.urlopen(req, timeout=600, context=self.ctx) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code != 429 and e.code < 500:
                    raise CloudError(f"The provider answered {e.code}: {detail}") from e
                problem = f"The provider answered {e.code}: {detail}"
                try:
                    wait = min(float(e.headers.get("retry-after") or wait), 120)
                except ValueError:
                    pass
            except urllib.error.URLError as e:
                problem = f"Could not reach the provider: {e.reason}"
            if attempt < 4:
                log.warning("AI request failed, trying again in %.0f s: %s", wait, problem[:120])
                time.sleep(wait)
        raise CloudError(problem)


def google_token():
    """A token from the Google Cloud sign-in already on this computer."""
    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# Every tool stays off on these routes. They are coding agents that can run commands, and a meeting
# transcript is text anyone on the call could have dictated.
DENY_ALL_POLICY = '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n'


class CliClient:
    def __init__(self, s):
        self.s = s
        self.command = s["provider"].removesuffix("-cli")
        self.exe = find_command(self.command)
        if not self.exe:
            raise CloudError(f"The {self.command} command was not found. Install it and sign in, then try again.")

    def arguments(self, folder, system):
        """(arguments, text to put before the prompt on stdin), with tools off."""
        model = self.s.get("model")
        if self.command == "claude":
            prompt = Path(folder) / "system.txt"
            prompt.write_text(system, encoding="utf-8")
            return ["-p", "--safe-mode", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                    "--output-format", "json", "--system-prompt-file", str(prompt),
                    *(["--model", model] if model else [])], ""
        if self.command == "gemini":
            policy = Path(folder) / "deny-all.toml"
            policy.write_text(DENY_ALL_POLICY, encoding="utf-8")
            return ["-p", "Follow the instructions above.", "--policy", str(policy), "-e", "none", "--skip-trust",
                    "-o", "json", *(["-m", model] if model else [])], system + "\n\n"
        return ["-s", "--available-tools=none-at-all", "--deny-tool=shell", "--deny-tool=write", "--deny-tool=read",
                "--no-custom-instructions", "--disable-builtin-mcps", "--no-ask-user", "--no-auto-update",
                *(["--model", model] if model else [])], system + "\n\n"

    def chat(self, system, user, max_tokens):
        # An empty folder, so no project's instruction files load.
        with tempfile.TemporaryDirectory(prefix="meetingscribe-ai-") as folder:
            args, before = self.arguments(folder, system)
            return self.execute(args, before + user, folder)

    def execute(self, args, text, cwd, timeout=1800):
        """Run the command with text on stdin: it can be longer than a command line, and other
        programs can read command lines. Returns the model's answer."""
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(Path(self.exe).parent), *extra_paths(), env.get("PATH", "")])
        if self.s.get("project"):
            env["GOOGLE_CLOUD_PROJECT"] = self.s["project"]
        try:
            r = subprocess.run([self.exe, *args], input=text, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=cwd, env=env, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise CloudError(f"The {self.command} command took too long.") from e
        if r.returncode:
            log.warning("%s exited with %s: %s", self.command, r.returncode, (r.stderr or r.stdout)[-300:])
            raise CloudError(f"The {self.command} command failed. Open a terminal, run {self.command} and sign in, then try again.")
        return self.reply(r.stdout)

    def reply(self, out):
        if self.command == "copilot":
            text = out
        else:
            try:
                answer = json.loads(out[out.index("{"):])
            except ValueError as e:
                raise CloudError(f"The {self.command} command gave an answer that could not be read.") from e
            if answer.get("is_error") or answer.get("error"):
                raise CloudError(f"The {self.command} command reported an error. Open a terminal, run {self.command} and sign in, then try again.")
            text = answer.get("result" if self.command == "claude" else "response")
        if not text or not text.strip():
            raise CloudError(f"The {self.command} command returned no text.")
        return text.strip()


def extra_paths():
    """Where these commands get installed. A packaged Mac app starts with almost no PATH."""
    home = Path.home()
    if sys.platform == "win32":
        return [str(Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "npm"), str(home / ".local/bin")]
    return [str(home / ".local/bin"), str(home / ".npm-global/bin"), "/opt/homebrew/bin", "/usr/local/bin"]


def find_command(name):
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(extra_paths()))
