"""The shape a meeting's note takes, and the questions you ask of meetings again and again.

Every template keeps ## Summary and ## Action Items, which the rest of the app reads, and adds
its own sections between them. A template is picked by words in the meeting's title.
"""

import json
import re

MAX_SECTIONS = 6
RESERVED = {"summary", "action items", "transcript", "jira", "voices"}

GENERAL = {"id": "general", "name": "General", "words": [], "focus": "", "sections": []}
BUILT_IN = [
    GENERAL,
    {"id": "one-on-one", "name": "1:1", "words": ["1:1", "1-1", "1 on 1", "one on one", "one-on-one"],
     "focus": "Cover how the work is going, support asked for, and anything the two people agreed.",
     "sections": [{"heading": "Feedback", "what": "feedback given in either direction"},
                  {"heading": "Growth", "what": "career or development topics"}]},
    {"id": "standup", "name": "Stand-up", "words": ["standup", "stand-up", "stand up", "daily sync", "daily scrum", "scrum"],
     "focus": "Give one bullet per person: what they are working on.",
     "sections": [{"heading": "Blockers", "what": "anything stopping someone, and who can unblock it"}]},
    {"id": "planning", "name": "Planning", "words": ["planning", "roadmap", "kickoff", "kick-off"],
     "focus": "Say what was agreed for the period being planned.",
     "sections": [{"heading": "Scope", "what": "work committed to, and work left out"},
                  {"heading": "Risks", "what": "risks and dependencies raised"}]},
    {"id": "incident", "name": "Incident review", "words": ["incident", "postmortem", "post-mortem", "sev1", "sev 1", "sev2", "sev 2", "rca"],
     "focus": "Say what broke, for whom, and how it was fixed.",
     "sections": [{"heading": "Timeline", "what": "what happened and when, using only times that were stated"},
                  {"heading": "Root cause", "what": "the cause, or say that it is still unknown"},
                  {"heading": "Prevention", "what": "what will stop this happening again"}]},
    {"id": "customer", "name": "Customer call", "words": ["discovery", "sales call", "customer call", "client call", "prospect", "qbr"],
     "focus": "Write from the point of view of understanding the customer.",
     "sections": [{"heading": "Customer needs", "what": "what they want and why"},
                  {"heading": "Objections", "what": "concerns and pushback they raised"},
                  {"heading": "Next step", "what": "what was agreed to happen next"}]},
]
QUESTIONS = ["What did we decide?", "What risks came up?", "Write a short status update for my manager."]


class TemplateError(Exception):
    """A sentence for the person editing templates."""


def _file(cfg):
    return cfg["work_dir"] / "templates.json"


def _load(cfg):
    try:
        data = json.loads(_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save(cfg, data):
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)
    _file(cfg).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def all_templates(cfg):
    """Yours first, so one of yours wins when both match a title."""
    return (_load(cfg).get("templates") or []) + BUILT_IN


def find(cfg, template_id):
    return next((t for t in all_templates(cfg) if t["id"] == template_id), None)


def choose(cfg, title):
    """The template whose words appear in this title, as whole words; General if none does."""
    title = (title or "").casefold()
    for t in all_templates(cfg):
        if any(re.search(rf"(?<!\w){re.escape(w.casefold())}(?!\w)", title) for w in t["words"] if w):
            return t
    return GENERAL


def add(cfg, name, words, focus, sections):
    """sections are lines of "Heading: what goes in it". Returns the new template's id."""
    name = " ".join(str(name).split())
    if not name or len(name) > 40:
        raise TemplateError("Give the template a name of up to 40 characters.")
    parsed = []
    for line in sections:
        heading, _, what = str(line).partition(":")
        heading = " ".join(heading.split())
        if not heading:
            continue
        if not re.fullmatch(r"[\w][\w &/'-]{0,39}", heading):
            raise TemplateError(f"“{heading}” can only use letters, numbers and spaces, up to 40 characters.")
        if heading.casefold() in RESERVED:
            raise TemplateError(f"“{heading}” is already part of every note. Choose another heading.")
        parsed.append({"heading": heading, "what": what.strip()[:200] or heading})
    if len(parsed) > MAX_SECTIONS:
        raise TemplateError(f"Use at most {MAX_SECTIONS} sections, so a small AI model can still fill them in.")
    data = _load(cfg)
    mine = data.get("templates") or []
    template_id = "my-" + (re.sub(r"[^\w]+", "-", name.casefold()).strip("-") or "template")
    if any(t["name"].casefold() == name.casefold() for t in all_templates(cfg)):
        raise TemplateError("There is already a template with that name.")
    mine.append({"id": template_id, "name": name, "words": [w for w in (" ".join(str(x).split()) for x in words) if w][:20],
                 "focus": " ".join(str(focus).split())[:400], "sections": parsed, "mine": True})
    _save(cfg, {**data, "templates": mine})
    return template_id


def remove(cfg, template_id):
    data = _load(cfg)
    _save(cfg, {**data, "templates": [t for t in data.get("templates") or [] if t["id"] != template_id]})


def questions(cfg):
    saved = _load(cfg).get("questions")
    return QUESTIONS if saved is None else saved


def save_questions(cfg, lines):
    kept = [q for q in (" ".join(str(x).split())[:300] for x in lines) if q][:12]
    _save(cfg, {**_load(cfg), "questions": kept})
    return kept


def prompt_parts(template):
    """(focus, extra sections) to put in the note writer's instructions. The extra sections come
    before Action Items, so that the action item clean-up never touches them."""
    focus = f" {template['focus']}" if template["focus"] else ""
    extra = "".join(f"\n## {s['heading']}\n- Short bullets: {s['what']}. If there is nothing, write \"- None.\"\n" for s in template["sections"])
    return focus, extra
