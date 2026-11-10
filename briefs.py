"""What to know before a meeting: what was said last time, and what is still open.

A brief is put together from your own notes, with no AI, so it can't invent anything and takes no
time. The calendar is one private iCal link, kept in the keychain. That needs no sign-in or admin.
Only a list of upcoming events is saved, and the link is only ever used to read it.
"""

import datetime as dt
import difflib
import re
import urllib.error
import urllib.request

from connectors import KEY_SERVICE
from tracker import Tracker

ACCOUNT = "calendar"
DAYS_AHEAD = 14
SYNC_EVERY_S = 15 * 60
MAX_CALENDAR = 30 * 2**20
LAST_MEETINGS = 3
STALE = re.compile(r"\b(weekly|daily|monthly|biweekly|bi-weekly|meeting|sync|call|invitation|updated|fw|re)\b|\d+|[^\w\s]")


class BriefError(Exception):
    """A sentence for the page."""


def calendar_link(url):
    """The https form of a pasted calendar link. webcal:// is the same thing under another name."""
    url = str(url).strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not re.fullmatch(r"https://[^\s/]+/\S*", url, re.I):
        raise BriefError("That doesn't look like a calendar link. It should start with https:// or webcal://.")
    return url


def read_calendar(url, context, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "MeetingScribe", "Accept": "text/calendar, */*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as r:
            if not r.geturl().lower().startswith("https://"):
                raise BriefError("That link redirected away from a secure address, so it was not used.")
            data = r.read(MAX_CALENDAR + 1)
    except urllib.error.HTTPError as e:
        raise BriefError(f"The calendar service answered {e.code}. Check the link is still published.") from None
    except (urllib.error.URLError, OSError, ValueError) as e:  # never the link itself: it is a secret
        raise BriefError(f"The calendar could not be reached ({type(e).__name__}). A company network may be blocking it.") from None
    if len(data) > MAX_CALENDAR:
        raise BriefError("That calendar is too large to read.")
    if b"BEGIN:VCALENDAR" not in data[:4096].upper():
        raise BriefError("That link didn't return a calendar. It may be a web page rather than the iCal link.")
    return data


def attendees(event):
    found = event.get("ATTENDEE") or []
    people = []
    for a in found if isinstance(found, list) else [found]:
        name = a.params.get("CN") or str(a).split(":", 1)[-1]
        name = " ".join(str(name).split())
        if name and name not in people:
            people.append(name)
    return people


def parse_events(data, start, end):
    """Timed events between two aware datetimes, repeats expanded, soonest first. All-day
    entries and cancelled events are left out, since neither is a meeting to prepare for."""
    import icalendar
    import recurring_ical_events
    try:
        occurrences = recurring_ical_events.of(icalendar.Calendar.from_ical(data)).between(start, end)
    except Exception as e:  # a calendar that doesn't parse is reported, not a crash
        raise BriefError(f"That calendar could not be read ({type(e).__name__}).") from None
    out = []
    for e in occurrences:
        begin = e.decoded("DTSTART")
        if not isinstance(begin, dt.datetime) or str(e.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin = (begin if begin.tzinfo else begin.astimezone()).astimezone()
        finish = e.decoded("DTEND") if e.get("DTEND") else begin
        finish = finish.astimezone() if isinstance(finish, dt.datetime) else begin
        title = " ".join(str(e.get("SUMMARY", "")).split()) or "Untitled"
        out.append({"id": f"{e.get('UID', title)}|{begin.isoformat()}", "title": title, "start": begin.isoformat(timespec="seconds"),
                    "end": finish.isoformat(timespec="seconds"), "people": attendees(e)})
    return sorted(out, key=lambda x: x["start"])


def series_name(title):
    """A title without what changes between meetings of the same series."""
    return " ".join(STALE.sub(" ", (title or "").casefold()).split())


def same_series(a, b):
    a, b = series_name(a), series_name(b)
    if not a or not b:
        return False
    return a == b or (len(a) >= 4 and (a in b or b in a)) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


class Briefs:
    def __init__(self, ms, cfg):
        self.ms, self.cfg = ms, cfg
        self.file = cfg["work_dir"] / "calendar.json"
        self.told_file = cfg["work_dir"] / "briefed.json"

    def _cache(self):
        return self.ms.read_json(self.file, {}) or {}

    def connected(self):
        return bool(self._cache().get("connected"))

    def connect(self, url):
        """Reads the calendar first, so a wrong link is never saved."""
        url = calendar_link(url)
        self._fetch(url, connected=True)
        self.ms.keychain("set", ACCOUNT, url, service=KEY_SERVICE)

    def disconnect(self):
        self.ms.keychain("delete", ACCOUNT, service=KEY_SERVICE)
        self.file.unlink(missing_ok=True)
        self.told_file.unlink(missing_ok=True)

    def _fetch(self, url, connected):
        now = dt.datetime.now().astimezone()
        data = read_calendar(url, self.ms.model_store.context(self.cfg["jira"]["ca_bundle"]))
        events = parse_events(data, now - dt.timedelta(hours=1), now + dt.timedelta(days=DAYS_AHEAD))
        self.ms.write_json(self.file, {"connected": connected, "fetched": now.isoformat(timespec="seconds"), "events": events, "error": ""})
        return events

    def sync(self, force=False):
        """Refresh the saved events if they are old enough. A failure keeps the old list and says why."""
        cache = self._cache()
        if not cache.get("connected"):
            return
        try:
            age = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(cache["fetched"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = SYNC_EVERY_S
        if age < SYNC_EVERY_S and not force:
            return
        url = self.ms.keychain("get", ACCOUNT, service=KEY_SERVICE)
        try:
            if not url:
                raise BriefError("The calendar link is gone from this computer. Connect it again.")
            self._fetch(url, connected=True)
        except BriefError as e:
            self.ms.write_json(self.file, {**cache, "fetched": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "error": str(e)})

    def upcoming(self):
        cache, now = self._cache(), dt.datetime.now().astimezone().isoformat(timespec="seconds")
        return {"connected": bool(cache.get("connected")), "error": cache.get("error", ""), "fetched": cache.get("fetched", ""),
                "events": [e for e in cache.get("events", []) if e["end"][:19] >= now[:19]][:40]}

    def event(self, event_id):
        return next((e for e in self._cache().get("events", []) if e["id"] == event_id), None)

    def brief(self, title, people=()):
        """The last meetings of this series or with these people, and what is still open from them."""
        ms, cfg = self.ms, self.cfg
        me = cfg["my_name"]
        others = [p for p in people if not ms.same_person(p, me)]
        wanted = (title or "").casefold().strip()
        by_title, by_people = [], []
        for d, meta in ms.done_sessions(cfg):
            speakers, _ = ms.session_speakers(d)
            shared = [p for p in others if any(ms.same_person(p, s) for s in speakers)]
            if same_series(title, meta.get("title", "")) or (wanted and wanted in meta.get("title", "").casefold()):
                by_title.append((d, meta))
            elif len(shared) * 2 >= max(1, len(others)) and shared:
                by_people.append((d, meta))
        picked = (by_title + by_people)[:LAST_MEETINGS]
        last, series = [], {d.name for d, _ in by_title}
        for d, meta in picked:
            note = meta.get("notes")
            text = ""
            if note:
                try:
                    with open(note, encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    pass
            last.append({"id": d.name, "title": meta.get("title", "Meeting"), "started_at": meta.get("started_at", ""),
                         "summary": ms.note_section(text, "Summary")[:6], "same_series": d.name in series})
        ids = {d.name for d, _ in by_title + by_people}
        names = list(others) + ([title] if not others and title else [])
        still_open = [i for i in Tracker(ms, cfg).items()
                      if ids & {m["id"] for m in i["meetings"]} or any(ms.same_person(i["owner"], n) for n in names)]
        return {"title": title, "people": list(people), "last": last, "open": still_open[:20], "something": bool(last or still_open)}

    def due_notices(self, minutes):
        """Events starting within minutes that have something worth reading, each only once."""
        now = dt.datetime.now().astimezone()
        told = self.ms.read_json(self.told_file, []) or []
        notices = []
        for e in self._cache().get("events", []):
            left = (dt.datetime.fromisoformat(e["start"]) - now).total_seconds() / 60
            if 0 <= left <= minutes and e["id"] not in told:
                told.append(e["id"])
                if self.brief(e["title"], e["people"])["something"]:
                    notices.append(f"{e['title']} starts in {max(1, round(left))} min. Your brief is under Upcoming.")
        if notices or told:
            self.ms.write_json(self.told_file, told[-200:])
        return notices
