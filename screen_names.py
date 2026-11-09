"""Reading people's names off the meeting window, the second opinion on who is who.

During a call the recorder looks at the meeting window a few times. The picture is read
for text and thrown away; only the names survive, in screen.json next to the recording:

    {"t": seconds into the call, "active": name on the highlighted tile or None, "roster": [names]}

From those, roster() says who was visibly there and votes() which voice is whose. Nothing
here touches the screen or the operating system: plat.window_image and plat.read_text do.
"""

import re
import unicodedata

import numpy as np

# Colors of the "this person is talking" outline, as ((r, g, b), tolerance).
HIGHLIGHTS = {
    "Microsoft Teams": [((127, 133, 245), 45), ((91, 95, 199), 40)],
    "Zoom": [((35, 217, 89), 60), ((250, 220, 60), 50)],
    "Google Meet": [((138, 180, 248), 45), ((26, 115, 232), 45)],
}
ALL_HIGHLIGHTS = [c for cs in HIGHLIGHTS.values() for c in cs]

# What a meeting window says that is not a person.
NOT_NAMES = {
    "mute", "unmute", "camera", "video", "share", "leave", "end", "chat", "people", "participants",
    "raise", "react", "more", "view", "apps", "rooms", "record", "recording", "stop", "start",
    "join", "present", "reactions", "captions", "settings", "microsoft teams", "teams", "zoom",
    "google meet", "meet", "webex", "host", "guest", "you", "me", "everyone", "gallery", "speaker",
    "invite", "security", "summary", "notes", "whiteboard", "help", "search", "calendar", "files",
    "activity", "calls", "copilot", "take control", "breakout rooms", "more actions",
}
_TAGS = re.compile(r"\s*[\(\[](?:guest|you|me|host|co-?host|organi[sz]er|presenter|unverified|external)[\)\]]",
                   re.I)


def looks_like_name(text):
    """One to three capitalized words, with no digits and no "Speaker 2". Scripts without
    capitals pass on letters alone."""
    words = text.split()
    if not 1 <= len(words) <= 3 or len(text) > 40 or re.match(r"(?i)speaker\s*\d*$", text):
        return False
    for w in words:
        if not all(unicodedata.category(c)[0] in "LM" or c in "-'.’" for c in w):
            return False
        if w[0].isascii() and not w[0].isupper():
            return False
    return True


def clean_name(text):
    """The person's name in a line of screen text, or None if the line isn't one."""
    name = " ".join(_TAGS.sub("", text).split()).strip(" -:|")
    if name.casefold() in NOT_NAMES or not looks_like_name(name):
        return None
    return None if any(w.casefold() in ("mute", "unmute", "share") for w in name.split()) else name


def fold(name):
    """Case and accents ignored, for comparing what OCR read at different moments."""
    return "".join(c for c in unicodedata.normalize("NFKD", name.casefold())
                   if not unicodedata.combining(c))


def find_highlight(img, service=None, min_w=200, min_h=120):
    """(left, top, right, bottom) of the outlined tile in an H x W x 3 RGB image, or None.
    An outline shows as long straight runs of its color along all four edges with a mostly
    different inside, which tells it apart from a colored button or a shared slide."""
    img = img.astype(np.int16)
    for color, tol in HIGHLIGHTS.get(service, ALL_HIGHLIGHTS):
        mask = (np.abs(img - np.array(color, np.int16)).max(axis=2) <= tol)
        rows, cols = np.flatnonzero(mask.sum(axis=1) >= min_w), np.flatnonzero(mask.sum(axis=0) >= min_h)
        if len(rows) < 2 or len(cols) < 2:
            continue
        t, b, l, r = int(rows.min()), int(rows.max()), int(cols.min()), int(cols.max())
        if r - l < min_w or b - t < min_h:
            continue
        band = 4
        edges = [mask[t:t + band, l:r].any(axis=0).mean(), mask[b - band + 1:b + 1, l:r].any(axis=0).mean(),
                 mask[t:b, l:l + band].any(axis=1).mean(), mask[t:b, r - band + 1:r + 1].any(axis=1).mean()]
        inside = mask[t + band:b - band, l + band:r - band].mean()
        if min(edges) >= 0.6 and inside < 0.25:
            return l, t, r, b
    return None


def read_snapshot(img, lines, service=None):
    """{"active", "roster"} from a picture and its text lines [{"text", "x", "y", "w", "h"}]."""
    box = find_highlight(img, service)
    active = None
    if box:
        l, t, r, b = box
        lower = t + (b - t) * 0.6
        inside = [ln for ln in lines
                  if l <= ln["x"] + ln["w"] / 2 <= r and lower <= ln["y"] + ln["h"] / 2 <= b]
        for ln in sorted(inside, key=lambda ln: (-ln["y"], ln["x"])):
            active = clean_name(ln["text"])
            if active:
                break
    roster = sorted({n for n in (clean_name(ln["text"]) for ln in lines) if n})
    return {"active": active, "roster": roster}


def roster(looks, me=""):
    """Names on screen in at least two looks: one misread frame or a passing word is not
    someone at the meeting. A single look counts when there was only one."""
    seen, spelling = {}, {}
    for look in looks:
        for n in look.get("roster", []):
            k = fold(n)
            seen[k] = seen.get(k, 0) + 1
            spelling.setdefault(k, n)
    need = 2 if len(looks) > 1 else 1
    return [spelling[k] for k, c in seen.items() if c >= need and k != fold(me)]


def votes(looks, segments, me):
    """{voice label: name} where the highlighted tile and the voice agree. At each look the
    person must be the only remote voice heard around that moment and you must be silent.
    A voice needs two looks at least 10 s apart, one candidate only, and a name can belong
    to one voice only; anything contested yields nothing."""
    ballots, spelling = {}, {}
    for look in looks:
        if not look.get("active"):
            continue
        lo, hi = look["t"] - 0.75, look["t"] + 0.75
        around = [s for s in segments if s["start"] < hi and s["end"] > lo]
        labels = {s["speaker"] for s in around}
        if len(labels) != 1 or me in labels:
            continue
        k = fold(look["active"])
        spelling[k] = look["active"]
        ballots.setdefault(labels.pop(), {}).setdefault(k, []).append(look["t"])
    out = {}
    for label, names in ballots.items():
        if len(names) != 1:
            continue
        k, times = next(iter(names.items()))
        if len(times) >= 2 and max(times) - min(times) >= 10 and sum(k in n for n in ballots.values()) == 1:
            out[label] = spelling[k]
    return out
