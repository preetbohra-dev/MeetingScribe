"""Send a finished meeting to where a team already works: Slack, Teams, Gmail or Outlook.

Nothing here runs by itself. A message leaves only because the person pressed a button on that
exact text. Slack and Teams use a channel's webhook link, which is a key to the channel and so
lives in the keychain. Gmail and Outlook get no sign-in at all: a draft opens in the person's own
mailbox and they press Send there. Addresses come from the person, never from the model.
"""

import datetime as dt
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

KEY_SERVICE = "MeetingScribe Connectors"
SERVICES = {"slack": "Slack", "teams": "Teams", "gmail": "Gmail", "outlook": "Outlook"}
WEBHOOK_HOSTS = {"slack": ("hooks.slack.com",),
                 "teams": (".logic.azure.com", ".api.powerplatform.com", ".webhook.office.com")}
OUTLOOK_HOSTS = {"work": "outlook.office.com", "personal": "outlook.live.com"}
MAX_CHANNEL_TEXT = 20_000  # well inside what Slack and a Teams card accept
MAX_DRAFT_LINK = 6_000  # a draft travels in a link, and browsers and mail sites limit those
TEST_MESSAGE = "MeetingScribe is connected to this channel. A meeting appears here only when someone chooses to send it."
EMAIL = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+")
SLACK_ID = re.compile(r"[UW][A-Z0-9]{6,}")
NO_OWNER = re.compile(r"(unassigned|none|tbd|unknown|speaker\s*\d+\b.*)", re.I)


class ConnectorError(Exception):
    """Worded for the person. Never includes a webhook link."""


def check_webhook(service, url):
    """A webhook link must be the vendor's own, so a pasted address can't send meetings elsewhere."""
    if service not in WEBHOOK_HOSTS:
        raise ConnectorError("That service doesn't use a webhook link.")
    try:
        u = urllib.parse.urlsplit(str(url).strip())
        port = u.port
    except ValueError:
        raise ConnectorError(f"That doesn't look like a {SERVICES[service]} webhook link.") from None
    host = (u.hostname or "").lower()
    allowed = any(host == h or (h.startswith(".") and host.endswith(h)) for h in WEBHOOK_HOSTS[service])
    if u.scheme != "https" or not allowed or port not in (None, 443) or u.username or u.password or len(u.path) < 2:
        raise ConnectorError(f"That doesn't look like a {SERVICES[service]} webhook link.")
    return str(url).strip()


def slack_text(text):
    """Escaped so a line in a transcript can't become a mention of the whole channel."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"@(channel|here|everyone)\b", "@​\\1", text, flags=re.I)


def slack_payload(text, mention=""):
    return {"text": (f"<@{mention}> " if mention else "") + slack_text(text), "unfurl_links": False, "unfurl_media": False}


def teams_payload(text):
    """One text block per line, because a card joins single line breaks. Links are written out,
    so a line in a transcript can't dress one up as something else."""
    body = [{"type": "TextBlock", "text": ln.replace("](", "] ("), "wrap": True, "spacing": "None"} for ln in text.splitlines() if ln.strip()]
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4", "body": body, "msteams": {"width": "Full"}}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def deliver(url, payload, ctx):
    """POST to a webhook. Errors name the problem, never the link, which is a secret."""
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(request, timeout=30):
            return
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404, 410):
            raise ConnectorError("That channel's link no longer works. Remove it and add a new one.") from None
        if e.code == 429:
            raise ConnectorError("The service asked to slow down. Try again in a minute.") from None
        raise ConnectorError(f"The service refused the message (error {e.code}).") from None
    except OSError as e:  # URLError is one of these, and so is a timeout
        raise ConnectorError(f"Couldn't reach the service: {type(e).__name__}. Check your network.") from None


def draft_link(service, to, subject, body, account="work"):
    """A compose window in the person's own mailbox. Spaces stay %20: Outlook shows + literally."""
    def q(s):
        return urllib.parse.quote(s, safe="")

    if service == "gmail":
        url = f"https://mail.google.com/mail/?view=cm&fs=1&to={q(','.join(to))}&su={q(subject)}&body={q(body)}"
    else:
        url = f"https://{OUTLOOK_HOSTS[account]}/mail/deeplink/compose?to={q(';'.join(to))}&subject={q(subject)}&body={q(body)}"
    if len(url) > MAX_DRAFT_LINK:
        raise ConnectorError(f"That is too long to open as a draft. Shorten it by about {(len(url) - MAX_DRAFT_LINK) // 3 + 1} characters.")
    return url


def split_owner(item):
    """(owner, task) from a note's "**Owner**: task". No bold name means no owner: better than a guess."""
    item = re.sub(r"^\[[ x]\]\s*", "", item).strip()
    m = re.match(r"\*\*(.+?)\*\*\s*:?\s*(.*)", item, re.S)
    if not m:
        return "", item.replace("**", "")
    return m.group(1).strip().rstrip(":").strip(), m.group(2).strip()


class Connectors:
    """Destinations in work_dir/connectors.json, addresses in contacts.json, and what was sent
    for a meeting in its session folder's sent.json. Webhook links are only in the keychain."""

    def __init__(self, ms, cfg):
        self.ms, self.cfg = ms, cfg
        self.file = cfg["work_dir"] / "connectors.json"
        self.contact_file = cfg["work_dir"] / "contacts.json"

    # -- where things can go

    def destinations(self):
        return self.ms.read_json(self.file, []) or []

    def find(self, did):
        found = next((d for d in self.destinations() if d["id"] == did), None)
        if not found:
            raise ConnectorError("That destination isn't connected any more.")
        return found

    def add_channel(self, service, name, url):
        """Saved only once the channel has really accepted a test message."""
        url = check_webhook(service, url)
        name = " ".join(str(name).split())[:60]
        if not name:
            raise ConnectorError("Give this channel a name, so you can tell it apart when sending.")
        self.post(service, url, TEST_MESSAGE)
        did = f"{service}-{secrets.token_hex(4)}"
        self.ms.keychain("set", did, url, service=KEY_SERVICE)
        self.ms.write_json(self.file, self.destinations() + [{"id": did, "service": service, "name": name}])
        return did

    def set_email(self, service, account="work"):
        """Turn a mailbox on. It needs no credentials, so there is nothing to test."""
        if service not in ("gmail", "outlook") or account not in OUTLOOK_HOSTS:
            raise ConnectorError("Unknown mail service.")
        rest = [d for d in self.destinations() if d["id"] != service]
        self.ms.write_json(self.file, rest + [{"id": service, "service": service, "name": SERVICES[service], "account": account}])

    def remove(self, did):
        found = self.find(did)
        if found["service"] in WEBHOOK_HOSTS:
            self.ms.keychain("delete", did, service=KEY_SERVICE)
        self.ms.write_json(self.file, [d for d in self.destinations() if d["id"] != did])

    def post(self, service, url, text, mention=""):
        if len(text) > MAX_CHANNEL_TEXT:
            raise ConnectorError(f"That is too long for a channel message. Shorten it by {len(text) - MAX_CHANNEL_TEXT} characters.")
        deliver(url, slack_payload(text, mention) if service == "slack" else teams_payload(text), self.ms.tls_context(self.cfg["jira"]))

    # -- who is who

    def contacts(self):
        return self.ms.read_json(self.contact_file, {}) or {}

    def remember(self, name, email="", slack=""):
        book = self.contacts()
        entry = dict(book.get(name, {}))
        if email:
            entry["email"] = email
        if slack:
            entry["slack"] = slack
        if entry != book.get(name):
            book[name] = entry
            self.ms.write_json(self.contact_file, book)

    # -- what to send

    def compose(self, d, meta, text, people):
        """The starting text for a recap and one follow-up per named owner. All of it is editable."""
        cfg = self.cfg
        items = [split_owner(i) for i in self.ms.note_section(text, "Action Items") if not re.match(r"none identified", i, re.I)]
        title = meta.get("title") or "Meeting"
        try:
            day = dt.datetime.fromisoformat(meta.get("started_at") or "").strftime("%A %d %B %Y")
        except ValueError:
            day = ""
        lines = [" - ".join(x for x in (title, day) if x), "", "Summary", *[f"- {s.replace('**', '')}" for s in self.ms.note_section(text, "Summary")]]
        if items:
            lines += ["", "Action items", *[f"- {o}: {t}" if o else f"- {t}" for o, t in items]]
        lines += ["", "Drafted by MeetingScribe from the meeting. It can get details wrong."]
        book, me = self.contacts(), cfg["my_name"].casefold()
        groups = {}
        for owner, task in items:
            if owner and not NO_OWNER.fullmatch(owner) and owner.casefold() not in (me, "me"):
                groups.setdefault(owner, []).append(task)
        followups = [{"owner": o, "email": book.get(o, {}).get("email", ""), "slack": book.get(o, {}).get("slack", ""),
                      "text": "\n".join([f"{o}, these came out of \"{title}\"{' on ' + day if day else ''}:", *[f"- {t}" for t in tasks]])}
                     for o, tasks in groups.items()]
        known = [book[p]["email"] for p in [*people, *groups] if book.get(p, {}).get("email")]
        return {"subject": f"Recap: {title}", "recap": "\n".join(lines), "followups": followups, "to": ", ".join(dict.fromkeys(known)),
                "no_owner": sum(1 for o, _ in items if not o or NO_OWNER.fullmatch(o)), "destinations": self.destinations(), "sent": self.ms.read_json(d / "sent.json", {}) or {}}

    def send(self, d, did, kind, owner, subject, text, to="", slack="", again=False):
        """One approved message to one place. A repeat needs asking for, because the people
        at the other end can't unread it."""
        target, text = self.find(did), str(text).strip()
        if kind not in ("recap", "followup") or not text:
            raise ConnectorError("There is nothing to send.")
        addresses = [a.strip() for a in re.split(r"[,;]", str(to)) if a.strip()]
        for a in addresses:
            if not EMAIL.fullmatch(a):
                raise ConnectorError(f"{a} doesn't look like an email address.")
        slack = str(slack).strip()
        if slack and not SLACK_ID.fullmatch(slack):
            raise ConnectorError("A Slack member ID starts with U or W, for example U012AB3CD.")
        key, log = f"{kind}:{owner}:{did}", self.ms.read_json(d / "sent.json", {}) or {}
        if key in log and not again:
            raise ConnectorError("That was already sent.")
        service = target["service"]
        if service in WEBHOOK_HOSTS:
            url = self.ms.keychain("get", did, service=KEY_SERVICE)
            if not url:
                raise ConnectorError("That channel's link is gone from this computer. Remove it and add it again.")
            self.post(service, check_webhook(service, url), text, slack if service == "slack" and kind == "followup" else "")
            message = f"Sent to {target['name']}." if service == "slack" else f"Teams accepted it for {target['name']}."
        else:
            webbrowser.open(draft_link(service, addresses, str(subject).strip(), text, target.get("account", "work")))
            message = f"Draft opened in {SERVICES[service]}. Press Send there."
        if kind == "followup":
            self.remember(str(owner), addresses[0] if addresses else "", slack)
        log[key] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "name": target["name"], "draft": service not in WEBHOOK_HOSTS}
        self.ms.write_json(d / "sent.json", log)
        return message
