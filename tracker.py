"""One list of open action items across every meeting.

The notes are the record. A box ticked in a note counts, and ticking one here ticks it in the
note. The same work raised again by the same owner is one item that was carried over. Work
marked done and raised again later is new work.
"""

import datetime as dt
import difflib
import hashlib
import re
from pathlib import Path

from connectors import split_owner

SIMILAR = 0.8  # how alike two tasks must read to be the same work


def plain(task):
    """A task without its due date, punctuation or case, for comparing."""
    task = re.sub(r"\([^)]*\)\s*$", "", task)
    return " ".join(re.findall(r"\w+", task.casefold()))


def same_work(a, b):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR


class Tracker:
    def __init__(self, ms, cfg):
        self.ms, self.cfg = ms, cfg
        self.file = cfg["work_dir"] / "tracker.json"

    def _state(self):
        state = self.ms.read_json(self.file, {}) or {}
        return {"done": state.get("done", {}), "dismissed": state.get("dismissed", {})}

    def _occurrences(self):
        """Every action item in every note, oldest meeting first."""
        for d, meta in reversed(self.ms.done_sessions(self.cfg)):
            note = Path(meta.get("notes") or "/missing")
            if not note.exists():
                continue
            for raw in self.ms.note_section(note.read_text(encoding="utf-8"), "Action Items"):
                owner, task = split_owner(raw)
                if not task or task.startswith("None identified") or raw.startswith("None identified"):
                    continue
                yield {"session": d.name, "title": meta.get("title", "Meeting"), "started_at": meta.get("started_at", ""),
                       "owner": owner or "Unassigned", "task": task, "plain": plain(task), "raw": raw,
                       "ticked": raw.startswith("[x]"), "note": note}

    def _clusters(self):
        state, clusters = self._state(), []

        def closed_at(c):
            return state["done"].get(c["id"]) or c["ticked_at"]

        for o in self._occurrences():
            home = None
            for c in reversed(clusters):
                alike = self.ms.same_person(c["owner"], o["owner"]) and same_work(c["plain"], o["plain"])
                if alike and (not closed_at(c) or o["started_at"][:19] <= closed_at(c)[:19]):
                    home = c
                    break
            if not home:
                key = hashlib.sha1(f"{o['session']}|{o['plain']}".encode("utf-8")).hexdigest()[:16]
                home = {"id": key, "owner": o["owner"], "plain": o["plain"], "seen": [], "ticked_at": ""}
                clusters.append(home)
            home.update(task=o["task"], plain=o["plain"], owner=o["owner"])
            home["seen"].append(o)
            if o["ticked"]:
                home["ticked_at"] = o["started_at"]
        for c in clusters:
            c["status"] = "dismissed" if c["id"] in state["dismissed"] else "done" if closed_at(c) else "open"
        return clusters

    def items(self, include_closed=False):
        """Open items, longest waiting first; with include_closed, the finished ones as well."""
        today, out = dt.date.today(), []
        for c in self._clusters():
            if c["status"] != "open" and not include_closed:
                continue
            first = c["seen"][0]["started_at"]
            try:
                waiting = (today - dt.date.fromisoformat(first[:10])).days
            except ValueError:
                waiting = 0
            out.append({"id": c["id"], "owner": c["owner"], "task": c["task"], "status": c["status"],
                        "mine": self.ms.same_person(c["owner"], self.cfg["my_name"]), "days": max(0, waiting),
                        "carried": len({s["session"] for s in c["seen"]}) - 1,
                        "meetings": [{"id": s["session"], "title": s["title"], "started_at": s["started_at"]} for s in c["seen"]]})
        return sorted(out, key=lambda i: -i["days"])

    def set_status(self, item_id, status):
        """status is "open", "done" or "dismissed". Done is also ticked in the notes."""
        if status not in ("open", "done", "dismissed"):
            raise ValueError("bad status")
        cluster = next((c for c in self._clusters() if c["id"] == item_id), None)
        if not cluster:
            raise ValueError("no such item")
        state = self._state()
        for book in state.values():
            book.pop(item_id, None)
        if status != "open":
            state[status][item_id] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.ms.write_json(self.file, state)
        for o in cluster["seen"]:
            self._tick(o, status == "done")
        return status

    @staticmethod
    def _tick(occurrence, done):
        """Best effort: the list above is what counts if the note has since been edited by hand."""
        body = re.sub(r"^\[[ x]\]\s*", "", occurrence["raw"])
        wanted = f"- [{'x' if done else ' '}] {body}"
        note = occurrence["note"]
        try:
            text = note.read_text(encoding="utf-8")
            changed = re.sub(rf"(?m)^- \[[ x]\]\s*{re.escape(body)}[ \t]*$", lambda _: wanted, text, count=1)
            if changed != text:
                note.write_text(changed, encoding="utf-8")
        except OSError:
            pass
