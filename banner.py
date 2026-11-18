"""The banner: a small window in the top corner of the screen, above every other window, from
the moment a call is noticed until recording stops. First it asks whether everyone on the
call agreed to be recorded, and nothing is recorded until you say so. Then it says the call
is being recorded, for as long as it is.

It has no close button, refuses to close, and no setting turns it off. The recorder starts
it (`meeting_scribe.py banner`) and starts it again if it ends anyway. It closes itself when
the recorder is neither asking nor recording.
"""

import sys
import time

import meeting_scribe as ms

WIDTH, ASK_HEIGHT, REC_HEIGHT, MARGIN = 440, 176, 72, 24

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
* { box-sizing: border-box; margin: 0; }
html, body { height: 100%; overflow: hidden; }
body { background: #18181b; color: #f4f4f5; font: 14px/1.4 -apple-system, "Segoe UI", system-ui, sans-serif;
  border: 2px solid #e5483a; border-radius: 10px; padding: 12px 16px; user-select: none; cursor: default; }
.top { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: #e5483a; flex: none; }
.rec .dot { animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .35; } }
.grow { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.soft { color: #a1a1aa; font-size: 12.5px; }
p { margin-top: 6px; }
.row { display: flex; gap: 8px; margin-top: 10px; }
button { font: inherit; font-weight: 600; border-radius: 7px; padding: 7px 12px; border: 1px solid #3f3f46; background: #27272a; color: #f4f4f5; cursor: pointer; }
button.primary { background: #e5483a; border-color: #e5483a; color: #fff; }
button:disabled { opacity: .5; }
</style></head>
<body><div id="view"></div>
<script>
const $view = document.getElementById("view");
let shown = "";
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  e.append(...kids.filter(k => k !== null && k !== undefined));
  return e;
}
function clock(iso) {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 1000));
  return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map((n, i) => i ? String(n).padStart(2, "0") : n).join(":").replace(/^0:/, "");
}
async function answer(button, name) {
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  if (!(await pywebview.api[name]())) document.querySelectorAll("button").forEach(b => b.disabled = false);
}
async function tick() {
  const s = await pywebview.api.state();
  if (s.state !== "asking" && s.state !== "recording") return pywebview.api.done();
  const call = (s.service || "Meeting") + (s.title ? ": " + s.title : "");
  if (s.state === "asking") {
    if (shown === "asking:" + call) return;
    shown = "asking:" + call;
    document.body.className = "";
    await pywebview.api.fit("asking");
    $view.replaceChildren(
      el("div", {className: "top"}, el("span", {className: "dot"}), el("span", {className: "grow", textContent: call})),
      el("p", {textContent: "Not recording. Did everyone on the call agree to be recorded?"}),
      s.notice ? el("p", {className: "soft", textContent: "You could say: “" + s.notice + "”"}) : null,
      el("div", {className: "row"},
        el("button", {className: "primary", id: "agree", textContent: "Everyone agreed, record", onclick: e => answer(e.target, "agree")}),
        el("button", {id: "decline", textContent: "Don't record", onclick: e => answer(e.target, "decline")})));
    return;
  }
  if (!shown.startsWith("recording:")) {
    shown = "recording:";
    document.body.className = "rec";
    await pywebview.api.fit("recording");
    $view.replaceChildren(
      el("div", {className: "top"}, el("span", {className: "dot"}), el("span", {className: "grow", id: "what"}), el("span", {id: "clock"})),
      el("div", {className: "soft", id: "agreed"}));
  }
  document.getElementById("what").textContent = "Recording · " + call;
  document.getElementById("clock").textContent = s.started_at ? clock(s.started_at) : "";
  document.getElementById("agreed").textContent = s.agreed_at
    ? "Everyone agreed at " + new Date(s.agreed_at).toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"}) + ". This stays on screen while recording."
    : "This stays on screen while recording.";
}
window.addEventListener("pywebviewready", () => { tick(); setInterval(tick, 1000); });
</script></body></html>"""


class Api:
    def __init__(self, cfg):
        self._cfg = cfg
        self._window = None  # underscore: pywebview exposes every public attribute to the page

    def state(self):
        keep_shown()
        s = ms.read_json(self._cfg["work_dir"] / "status.json") or {}
        if time.time() - s.get("time", 0) > 10:  # the recorder has stopped
            return {"state": "off"}
        out = {k: s.get(k) for k in ("state", "service", "title", "started_at")}
        out["notice"] = self._cfg["consent_notice"]
        if s.get("state") == "recording" and s.get("session"):
            consent = ms.read_json(self._cfg["work_dir"] / "sessions" / s["session"] / "consent.json") or {}
            out["agreed_at"] = consent.get("time")
        return out

    def agree(self):
        return bool(ms.agree_to_record(self._cfg))

    def decline(self):
        return bool(ms.decline_recording(self._cfg))

    def fit(self, kind):
        self._window.resize(WIDTH, ASK_HEIGHT if kind == "asking" else REC_HEIGHT)

    def done(self):
        self._window.events.closing -= refuse
        self._window.destroy()


def refuse():
    return False  # returning False from closing keeps the window open


TITLE = "MeetingScribe: recording notice"


def keep_shown():
    """Called every second: a minimized or hidden banner is put back, without taking focus.
    On a Mac the window is shown again by the minimized event in run()."""
    if sys.platform != "win32":
        return
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd and (user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd)):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13)  # topmost again; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE


def run(cfg):
    import webview
    api = Api(cfg)
    screen = webview.screens[0]
    window = webview.create_window(TITLE, html=PAGE, js_api=api, width=WIDTH, height=ASK_HEIGHT,
                                   x=screen.width - WIDTH - MARGIN, y=MARGIN * 2, frameless=True, easy_drag=True,
                                   on_top=True, resizable=False, focus=False, min_size=(WIDTH, REC_HEIGHT),
                                   background_color="#18181b")
    api._window = window
    window.events.closing += refuse
    window.events.minimized += window.restore
    webview.start()
