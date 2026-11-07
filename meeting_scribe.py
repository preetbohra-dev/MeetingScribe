#!/usr/bin/env python3
"""
MeetingScribe: a zero-touch, 100% on-device Microsoft Teams meeting recorder.

  run              Background daemon. Watches for Teams calls, records two tracks
                   (Me = microphone, Others = Teams audio via ScreenCaptureKit),
                   and hands each finished recording to the processing pipeline.
  process DIR      Transcribe (mlx-whisper) -> merge -> summarize (mlx-lm/Ollama)
                   -> write Markdown. The daemon runs this automatically.
  status           Show permissions, Teams audio state and detected windows.
  test-capture N   Record N seconds of both tracks and report levels.
  download-models  One-time model download (the only command that uses network).
  jira             Review a meeting's action items and push the ones you approve
                   to Jira (opt-in; you approve every ticket and comment).

Recording and processing never talk to a remote host. Model loading runs with
HF_HUB_OFFLINE=1, and the Ollama backend refuses any non-loopback URL. The only
network traffic is the `jira` command, and only to the [jira].base_url you set.
"""

import os
import sys

if not (len(sys.argv) > 1 and sys.argv[1] == "download-models"):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"  # every model is public; never send a saved token

import argparse
import base64
import datetime as dt
import difflib
import getpass
import json
import logging
import logging.handlers
import math
import re
import ssl
import subprocess
import tempfile
import textwrap
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import numpy as np

import platforms

plat = platforms.load()
HERE = Path(__file__).resolve().parent
TARGET_SR = 16000  # Whisper's native rate; both tracks are stored at this rate.

DEFAULTS = {
    "output_dir": "~/Documents/Meeting Notes",
    **plat.DIRS,  # work_dir and log_dir
    # "teams" = only audio produced by Teams processes; "all" = all system audio.
    "capture_mode": "teams",
    # "tap" = Core Audio process tap (audio only, no screen-capture indicator);
    # "screencapturekit" = older path, makes macOS show its screen-recording glow.
    "system_audio_backend": "tap",
    "teams_bundle_prefix": "com.microsoft.teams",
    "mic_device": "",  # substring of an input device name; "" = system default
    "start_after_s": 4,  # Teams must hold the mic this long before we start
    "stop_after_s": 20,  # ...and release it this long before we stop
    "window_keepalive_max_s": 600,  # a meeting window alone keeps a call alive this long
    "min_meeting_s": 60,  # shorter recordings (e.g. device preview) are discarded
    "notify": True,
    "keep_audio": True,  # keep a stereo WAV (L = Me, R = Others) next to the notes
    "my_name": "Me",
    "others_name": "Others",
    "language": "",  # "" = auto-detect, or e.g. "en"
    "mlx_llm_model": "mlx-community/Qwen2.5-14B-Instruct-4bit",
    # llamacpp: Hugging Face repo + file of a GGUF model.
    "gguf_model": "bartowski/Qwen2.5-14B-Instruct-GGUF/Qwen2.5-14B-Instruct-Q4_K_M.gguf",
    "ollama_model": "qwen2.5:14b",
    "ollama_url": "http://127.0.0.1:11434",
    "llm_chunk_chars": 32000,
    # Drop small talk and personal conversation; notes and saved transcript keep only work.
    "work_only": True,
    "work_filter_chunk_chars": 8000,  # lines per filter call; small models do better on less
    # mlx model for the work filter; "" = mlx_llm_model, loaded once for both steps.
    # Llama 3.1 8B dropped terse work lines as small talk; Qwen 2.5 14B got every line
    # of a labelled test set right, so it is the default for both.
    "work_filter_model": "",
    # Suspected echo / low-confidence / mis-heard lines are kept and tagged in the
    # note. Set True to delete them instead.
    "drop_suspect_lines": False,
    "speaker_id": True,  # split the Others track by voice and match known voiceprints
    "voice_model": "speechbrain/spkrec-ecapa-voxceleb",  # Windows: WeSpeaker ResNet34 (ONNX)
    "speaker_cluster_threshold": 0.6,  # cosine distance; lower = more, finer speakers
    "speaker_match_threshold": 0.5,  # cosine similarity needed to reuse a stored name
    "speaker_merge_similarity": 0.7,  # clusters whose voices are this similar are one person
    "speaker_min_seconds": 15,  # voices with less talk time are folded into the closest voice
    "max_speakers": 0,  # hard cap on remote speakers; 0 = no cap
    "meeting_window_exclude_regex": r"^(Chat|Activity|Calendar|Teams|Calls|Files|Apps|OneDrive|Copilot|Settings|Assignments|Approvals|People|Notifications|Window)\b",
    # Last, so they win: per-OS asr_backend ("mlx" / "faster-whisper"), whisper_model,
    # llm_backend ("mlx" / "llamacpp" / "ollama"), and on Windows gguf_model (sized to
    # RAM) and voice_model. See platforms/macos.py and platforms/windows.py.
    **plat.MODEL_DEFAULTS,
}

# [jira] table in config.toml. Secrets never live here, only in the macOS Keychain or
# Windows Credential Manager (keyring).
JIRA_DEFAULTS = {
    "base_url": "",  # e.g. "https://yourcompany.atlassian.net"
    "deployment": "cloud",  # "cloud" (*.atlassian.net) or "server" (Server / Data Center)
    "auth": "",  # "bearer" (personal access token), "basic" (email + API token), "cookie"; "" = ask
    "email": "",  # for basic auth
    "project": "",  # suggested space (project) key; you confirm it on every run
    "search_days": 365,  # related-ticket search looks at tickets updated this recently
    "search_limit": 8,  # most relevant existing tickets the model compares per candidate
    "labels": ["meetingscribe"],  # added to every ticket created
    "verify_ssl": True,
    "ssl_strict": False,  # False = accept Zscaler-style proxy CAs (chain and hostname still checked)
    "ca_bundle": "",
    "timeout_seconds": 30,
}

log = logging.getLogger("meetingscribe")

DISCLAIMER_VERSION = 1
DISCLAIMER = """MeetingScribe: terms of use

MeetingScribe is provided "as is", without warranty of any kind. By using it you accept that:

* You alone are responsible for how you use it: for telling everyone in a meeting that it is
  being recorded and getting their consent where required, for consent to create and keep
  voiceprints (biometric data), for what you push to Jira, and for following every law,
  regulation and company policy that applies to you and the people you record (for example
  call-recording consent laws, GDPR, BIPA and your employer's rules).
* The developer, and the developer's employer, are not responsible or liable for your use of
  MeetingScribe or for any loss, claim or damage that results from it.
* MeetingScribe is not an official tool of any company, and nothing about it is legal advice.
  If in doubt, ask your legal or privacy team before using it."""


def terms_accepted(cfg):
    p = cfg["work_dir"] / "terms_accepted.json"
    try:
        return json.loads(p.read_text()).get("version") == DISCLAIMER_VERSION
    except (OSError, ValueError):
        return False


def accept_terms(cfg):
    """Shows the terms and records acceptance. Returns False if declined or not interactive."""
    if terms_accepted(cfg):
        return True
    print(DISCLAIMER + "\n")
    if not sys.stdin.isatty():
        return False
    if input('Type "I agree" to accept, anything else to cancel: ').strip().lower() != "i agree":
        return False
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)
    (cfg["work_dir"] / "terms_accepted.json").write_text(json.dumps(
        {"version": DISCLAIMER_VERSION, "accepted_at": dt.datetime.now().isoformat(timespec="seconds"),
         "user": getpass.getuser()}))
    return True


def load_config():
    cfg = dict(DEFAULTS)
    path = HERE / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    cfg["jira"] = {**JIRA_DEFAULTS, **cfg.get("jira", {})}
    for k in ("output_dir", "work_dir", "log_dir"):
        cfg[k] = Path(cfg[k]).expanduser()
    return cfg


def setup_logging(cfg, name):
    cfg["log_dir"].mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        cfg["log_dir"] / f"{name}.log", maxBytes=5_000_000, backupCount=3
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def notify(cfg, text):
    if cfg["notify"]:
        plat.notify(text)


def resample(x, sr_in, sr_out):
    if sr_in == sr_out or len(x) == 0:
        return x
    g = math.gcd(int(sr_in), int(sr_out))
    up, down = int(sr_out) // g, int(sr_in) // g
    try:
        from scipy.signal import resample_poly
        return resample_poly(x, up, down).astype(np.float32)
    except ImportError:  # Windows: Smart App Control blocks scipy's DLLs
        return resample_poly_np(x, up, down).astype(np.float32)


def resample_poly_np(x, up, down):
    """scipy.signal.resample_poly(x, up, down) in numpy: same Kaiser(5) low-pass, same
    delay compensation and output length, computed one polyphase branch per output."""
    x = np.asarray(x, np.float64)
    half = 10 * max(up, down)
    k = np.arange(-half, half + 1)
    h = np.sinc(k / max(up, down)) * np.kaiser(2 * half + 1, 5.0)
    h = h / h.sum() * up
    n_out = -(-len(x) * up // down)
    pre = down - half % down
    h = np.concatenate([np.zeros(pre), h])
    start = (half + pre) // down
    taps = -(-len(h) // up)
    phases = np.zeros(taps * up)
    phases[:len(h)] = h
    phases = phases.reshape(taps, up).T  # phases[p, m] = h[p + m * up]
    pos = np.arange(start, start + n_out) * down
    j = (pos // up)[:, None] - np.arange(taps)[None, :]
    xs = np.where((j >= 0) & (j < len(x)), x[np.clip(j, 0, len(x) - 1)], 0.0)
    return (phases[pos % up] * xs).sum(axis=1)


def meeting_window_title(cfg):
    exclude = re.compile(cfg["meeting_window_exclude_regex"], re.I)
    for _, title in plat.teams_windows():
        clean = re.sub(r"\s*\|\s*Microsoft Teams.*$", "", title).strip()
        if clean and not exclude.search(clean) and clean.lower() != "microsoft teams":
            return clean
    return None


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

class TrackWriter:
    """Appends mono int16 PCM at TARGET_SR to a raw file. Each chunk is placed on a
    shared wall-clock timeline: if a source pauses (ScreenCaptureKit sends nothing
    while Teams is silent), the gap is filled with silence so both tracks stay
    aligned for the chronological merge. Raw PCM survives a crash mid-meeting."""

    def __init__(self, path, t0):
        self.path, self.t0 = path, t0
        self.f = open(path, "ab")
        self.samples = 0
        self.peak = 0.0
        self.last_write = None
        self.lock = threading.Lock()

    def write(self, x, sr, arrived):
        x = resample(np.asarray(x, dtype=np.float32), sr, TARGET_SR)
        with self.lock:
            if self.f is None:
                return
            start = int((arrived - self.t0) * TARGET_SR) - len(x)
            gap = start - self.samples
            if gap > TARGET_SR // 4:  # ignore normal callback jitter
                self.f.write(np.zeros(gap, np.int16).tobytes())
                self.samples += gap
            self.f.write((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
            self.samples += len(x)
            self.peak = max(self.peak, float(np.max(np.abs(x))) if len(x) else 0.0)
            self.last_write = arrived

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None


def close_stream(stream, timeout=3.0):
    """Abort and close a PortAudio stream without risking the daemon: Core Audio's
    AudioOutputUnitStop can occasionally deadlock, so it runs on a throwaway thread."""
    def _close():
        try:
            stream.abort()
            stream.close()
        except Exception as e:
            log.warning("mic stop: %s", e)

    t = threading.Thread(target=_close, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.warning("Core Audio did not release the mic stream within %.0f s; abandoning it", timeout)


class MicCapture:
    def __init__(self, writer, device_hint):
        self.writer, self.device_hint = writer, device_hint
        self.stream = None
        self.last_cb = None

    def _device(self):
        import sounddevice as sd
        if not self.device_hint:
            return None
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and self.device_hint.lower() in d["name"].lower():
                return i
        return None

    def start(self):
        import sounddevice as sd
        sd._terminate()  # refresh PortAudio's device list (headset plugged in, etc.)
        sd._initialize()
        dev = self._device()
        info = sd.query_devices(dev, "input")

        def cb(indata, frames, t, status):
            self.last_cb = time.monotonic()
            self.writer.write(indata[:, 0].copy(), self.sr, self.last_cb)

        for sr in (TARGET_SR, int(info["default_samplerate"])):
            try:
                self.sr = sr
                self.stream = sd.InputStream(device=dev, channels=1, samplerate=sr,
                                             dtype="float32", callback=cb, blocksize=0)
                self.stream.start()
                break
            except Exception as e:
                log.warning("mic open at %s Hz failed: %s", sr, e)
                self.stream = None
        if self.stream is None:
            raise RuntimeError("could not open microphone")
        self.last_cb = time.monotonic()
        log.info("mic capture started on '%s' @ %d Hz", info["name"], self.sr)

    def stop(self):
        if self.stream:
            close_stream(self.stream)
            self.stream = None


class Recording:
    def __init__(self, cfg, session_dir, title):
        self.cfg, self.dir = cfg, session_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        self.meta = {"status": "recording", "title": title or "Teams meeting",
                     "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                     "sample_rate": TARGET_SR}
        self._save_meta()
        self.mic_w = TrackWriter(self.dir / "mic.pcm", self.t0)
        self.sys_w = TrackWriter(self.dir / "system.pcm", self.t0)
        self.mic = MicCapture(self.mic_w, cfg["mic_device"])
        self.sys = plat.system_audio_capture(cfg, self.sys_w)
        self.warned = set()

    def _save_meta(self):
        tmp = self.dir / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2))
        tmp.replace(self.dir / "session.json")

    def start(self):
        try:
            self.sys.start()
        except Exception as e:
            log.error("system audio capture failed to start: %s", e)
        try:
            self.mic.start()
        except Exception as e:
            log.error("mic capture failed to start: %s", e)

    def set_title(self, title):
        if title and self.meta["title"] == "Teams meeting":
            self.meta["title"] = title
            self._save_meta()

    def watchdog(self, teams_output_active, mic_device_changed):
        now = time.monotonic()
        elapsed = now - self.t0
        if mic_device_changed or (self.mic.stream and now - (self.mic.last_cb or now) > 3):
            log.info("restarting mic capture (device changed or stalled)")
            self.mic.stop()
            try:
                self.mic.start()
            except Exception as e:
                log.error("mic restart failed: %s", e)
        if elapsed > 8 and self.mic_w.samples and self.mic_w.peak == 0 and "mic0" not in self.warned:
            self.warned.add("mic0")
            log.error("microphone delivers pure silence: Microphone permission is probably denied")
        if self.sys.stopped:
            log.info("restarting system audio capture")
            self.sys.stop()
            try:
                self.sys.start()
            except Exception as e:
                log.error("system audio restart failed: %s", e)
        # Teams is playing audio but nothing reaches us: fall back to all system audio.
        if (self.sys.mode == "teams" and elapsed > 30 and teams_output_active
                and self.sys_w.peak < 1e-4):
            log.warning("Teams is playing audio but per-app capture is silent; "
                        "switching this recording to all-system-audio capture")
            self.sys.stop()
            self.sys.mode = "all"
            try:
                self.sys.start()
            except Exception as e:
                log.error("system audio restart failed: %s", e)
        if (self.sys.mode == "all" and elapsed > 90 and teams_output_active
                and self.sys_w.peak < 1e-4 and "sys0" not in self.warned):
            self.warned.add("sys0")
            log.error("system audio is pure silence even when tapping everything: the "
                      "'System Audio Recording Only' permission is probably denied")

    def stop(self):
        self.sys.stop()
        self.mic.stop()
        self.mic_w.close()
        self.sys_w.close()
        dur = time.monotonic() - self.t0
        self.meta.update(status="recorded", duration_s=round(dur, 1),
                         ended_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        self._save_meta()
        return dur


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------

class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sessions = cfg["work_dir"] / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.probe = plat.AudioProbe()
        self.rec = None
        self.mic_since = None
        self.last_mic = 0.0
        self.last_signal = 0.0
        self.held_by_window = False
        self.queue = []
        self.worker = None
        self.mic_dev = self.probe.default_input_device()

    def check_permissions(self):
        plat.request_permissions(self.cfg)
        try:  # opening the mic once triggers the Microphone prompt on first launch
            import sounddevice as sd
            probe = sd.InputStream(channels=1, samplerate=TARGET_SR)
            probe.start()
            time.sleep(0.3)
            close_stream(probe)
        except Exception as e:
            log.error("microphone not available: %s", e)

    def recover(self):
        for meta_path in sorted(self.sessions.glob("*/session.json")):
            meta = json.loads(meta_path.read_text())
            if meta.get("status") == "recording":  # we crashed or the computer lost power mid-call
                sizes = [p.stat().st_size for p in meta_path.parent.glob("*.pcm")]
                meta["status"] = "recorded"
                meta["duration_s"] = round(max(sizes, default=0) / 2 / TARGET_SR, 1)
                meta_path.write_text(json.dumps(meta, indent=2))
            if meta.get("status") == "recorded":
                log.info("re-queueing unprocessed session %s", meta_path.parent.name)
                self.queue.append(meta_path.parent)

    def tick(self):
        now = time.monotonic()
        mic_active, out_active = self.probe.teams_state(self.cfg)
        title = meeting_window_title(self.cfg)
        if mic_active:
            self.last_mic = now

        if self.rec is None:
            self.mic_since = (self.mic_since or now) if mic_active else None
            if self.mic_since and now - self.mic_since >= self.cfg["start_after_s"]:
                self.start_recording(title)
        else:
            # A meeting-looking window only counts while Teams still has its call audio
            # open; after hang-up the window title can linger (e.g. the chat stays selected).
            window_ok = (title and out_active
                         and now - self.last_mic < self.cfg["window_keepalive_max_s"])
            if mic_active or window_ok:
                self.last_signal = now
            if window_ok and not mic_active and not self.held_by_window:
                log.info("Teams released the mic; window '%s' + Teams audio keep recording", title)
            self.held_by_window = bool(window_ok and not mic_active)
            self.rec.set_title(title)
            dev = self.probe.default_input_device()
            changed, self.mic_dev = dev != self.mic_dev, dev
            self.rec.watchdog(out_active, changed)
            if now - self.last_signal >= self.cfg["stop_after_s"]:
                self.stop_recording()

        self.pump_queue()

    def start_recording(self, title):
        sid = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        log.info("meeting detected (%s); recording session %s", title or "untitled", sid)
        self.rec = Recording(self.cfg, self.sessions / sid, title)
        self.rec.start()
        self.last_signal = time.monotonic()
        notify(self.cfg, f"Recording Teams meeting{': ' + title if title else ''}")

    def stop_recording(self):
        rec, self.rec, self.mic_since = self.rec, None, None
        dur = rec.stop()
        log.info("meeting ended after %.0f s", dur)
        if dur < self.cfg["min_meeting_s"]:
            log.info("recording shorter than min_meeting_s; discarding")
            for p in rec.dir.iterdir():
                p.unlink()
            rec.dir.rmdir()
            return
        notify(self.cfg, "Meeting ended. Transcribing and summarizing locally…")
        self.queue.append(rec.dir)

    def pump_queue(self):
        if self.worker and self.worker.poll() is None:
            return
        if self.worker:
            log.info("processing finished with exit code %s", self.worker.returncode)
            self.worker = None
        if self.queue:
            d = self.queue.pop(0)
            log.info("processing %s", d.name)
            self.worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                            "process", str(d)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self):
        self.check_permissions()
        self.recover()
        log.info("daemon started; watching for Teams meetings")
        try:
            while True:
                try:
                    self.tick()
                except Exception:
                    log.exception("tick failed")
                plat.run_loop(1.0)
        finally:
            if self.rec:
                self.stop_recording()


# --------------------------------------------------------------------------
# Processing pipeline: transcribe -> merge -> summarize -> markdown
# --------------------------------------------------------------------------

HALLUCINATIONS = {"thank you.", "thanks for watching!", "thanks for watching.", "you",
                  "bye.", "thank you for watching.", "subtitles by the amara.org community"}


def voiced_frames(x, sr=TARGET_SR, frame_s=0.03):
    """Energy gate: one bool per frame_s frame, True where someone is talking."""
    hop = int(sr * frame_s)
    n = len(x) // hop
    if n == 0:
        return np.zeros(0, bool)
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    return db > max(np.percentile(db, 10) + 8, -60)


def speech_regions(x, sr=TARGET_SR, frame_s=0.03, pad_s=1.0, merge_gap_s=1.5):
    """Energy gate -> [start, end, start, end, ...] for Whisper's clip_timestamps.
    Skipping silence makes transcription much faster and stops Whisper from
    hallucinating text into long muted stretches."""
    active = np.flatnonzero(voiced_frames(x, sr, frame_s))
    if len(active) == 0:
        return []
    regions, start, prev = [], active[0], active[0]
    for i in active[1:]:
        if (i - prev) * frame_s > merge_gap_s:
            regions.append((start, prev))
            start = i
        prev = i
    regions.append((start, prev))
    total = len(x) / sr
    out = []
    for a, b in regions:
        s, e = max(0.0, a * frame_s - pad_s), min(total, (b + 1) * frame_s + pad_s)
        if e - s >= 0.4:
            out += [round(float(s), 2), round(float(e), 2)]
    return out


_whisper = {}  # faster-whisper model, loaded once per process


def faster_whisper_model(cfg):
    if "model" not in _whisper:
        import ctranslate2
        from faster_whisper import WhisperModel
        if ctranslate2.get_cuda_device_count() > 0:
            try:
                log.info("loading %s on the GPU", cfg["whisper_model"])
                _whisper["model"] = WhisperModel(cfg["whisper_model"], device="cuda",
                                                 compute_type="float16", local_files_only=True)
                return _whisper["model"]
            except Exception as e:  # e.g. cuBLAS / cuDNN libraries missing
                log.warning("GPU Whisper failed (%s); using the CPU", e)
        log.info("loading %s on the CPU", cfg["whisper_model"])
        _whisper["model"] = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8",
                                         local_files_only=True)
    return _whisper["model"]


def whisper_transcribe(cfg, audio, **kwargs):
    """{"segments": [...]} in mlx-whisper's shape, from whichever backend is configured."""
    if cfg["asr_backend"] == "mlx":
        import mlx_whisper
        return mlx_whisper.transcribe(audio, path_or_hf_repo=cfg["whisper_model"], verbose=None,
                                      **kwargs)
    if cfg["asr_backend"] != "faster-whisper":
        raise ValueError(f"unknown asr_backend {cfg['asr_backend']!r}")
    segments, _ = faster_whisper_model(cfg).transcribe(audio, vad_filter=False, **kwargs)
    return {"segments": [{"start": s.start, "end": s.end, "text": s.text,
                          "no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob,
                          "words": [{"start": w.start, "end": w.end, "word": w.word}
                                    for w in s.words or []]}
                         for s in segments]}


def release_whisper():
    """Free Whisper's memory before the LLM loads."""
    _whisper.clear()
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.model = None
        mx.clear_cache()
    except Exception:
        pass


def transcribe_track(cfg, audio, speaker):
    clips = speech_regions(audio)
    if not clips:
        log.info("%s track: no speech detected", speaker)
        return []
    log.info("%s track: transcribing %.0f s of speech", speaker,
             sum(clips[i + 1] - clips[i] for i in range(0, len(clips), 2)))
    kwargs = dict(condition_on_previous_text=False, clip_timestamps=clips, word_timestamps=True,
                  hallucination_silence_threshold=2.0)
    if cfg["language"]:
        kwargs["language"] = cfg["language"]
    result = whisper_transcribe(cfg, audio, **kwargs)
    segs = []
    for s in result.get("segments", []):
        text = s["text"].strip()
        if not text:
            continue
        flag = None
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0:
            flag = "low confidence"
        elif text.lower() in HALLUCINATIONS and s["end"] - s["start"] < 3:
            flag = "possible mis-hearing"
        if flag:
            log.info("%s: %s [%s] %r", speaker, flag, ts(s["start"]), text)
            if cfg["drop_suspect_lines"]:
                continue
        segs.append({"start": float(s["start"]), "end": float(s["end"]),
                     "speaker": speaker, "text": text, "flag": flag,
                     "words": [(float(w["start"]), float(w["end"]), w["word"])
                               for w in s.get("words", [])]})
    return segs


# --------------------------------------------------------------------------
# Speaker identification: voice embeddings -> clusters -> voiceprint library
# --------------------------------------------------------------------------

EMBED_WIN_S, EMBED_HOP_S = 2.0, 1.0
# A window needs this much actual speech to shape a voice or a voiceprint. Shorter
# ones ("okay", "mm-hmm", a breath) mostly fingerprint the Teams codec and room
# noise, so they look alike across people: they join the closest voice afterwards.
SOLID_VOICED_S = 1.2


def kaldi_fbank(x, sr=TARGET_SR, n_mels=80):
    """Kaldi log-mel filterbank (25 ms Hamming frames every 10 ms, no dither), mean
    removed per window: the input WeSpeaker models were trained on, in numpy."""
    x = np.asarray(x, np.float64) * 32768
    flen, shift, nfft = int(0.025 * sr), int(0.010 * sr), 512
    n = 1 + (len(x) - flen) // shift
    fr = x[np.arange(flen)[None, :] + shift * np.arange(n)[:, None]]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = fr - 0.97 * np.concatenate([fr[:, :1], fr[:, :-1]], axis=1)  # pre-emphasis
    power = np.abs(np.fft.rfft(fr * np.hamming(flen), nfft)) ** 2
    mel = lambda f: 1127 * np.log(1 + f / 700)  # noqa: E731
    lo, hi = mel(20.0), mel(sr / 2)
    left = lo + np.arange(n_mels)[:, None] * (hi - lo) / (n_mels + 1)
    width = (hi - lo) / (n_mels + 1)
    bins = mel(sr / nfft * np.arange(nfft // 2))[None, :]
    banks = np.maximum(0, np.minimum((bins - left) / width, (left + 2 * width - bins) / width))
    banks = np.pad(banks, ((0, 0), (0, 1)))  # the Nyquist bin gets no weight
    fb = np.log(np.maximum(power @ banks.T, np.finfo(np.float32).eps))
    return (fb - fb.mean(axis=0)).astype(np.float32)


class VoiceEncoder:
    """Speaker embeddings from cfg["voice_model"] in the local HF cache: ECAPA-TDNN
    (SpeechBrain, torch) on macOS; WeSpeaker ResNet34 (ONNX Runtime) on Windows, where
    Smart App Control blocks torch's DLLs."""

    def __init__(self, cfg):
        from huggingface_hub import snapshot_download
        src = Path(snapshot_download(cfg["voice_model"], local_files_only=True))
        self.onnx = next(src.glob("*.onnx"), None)
        if self.onnx:
            import onnxruntime as ort
            self.model = ort.InferenceSession(str(self.onnx), providers=["CPUExecutionProvider"])
            self.input = self.model.get_inputs()[0].name
        else:
            from speechbrain.inference.speaker import EncoderClassifier
            self.model = EncoderClassifier.from_hparams(
                source=str(src), savedir=str(cfg["work_dir"] / "models" / "ecapa"),
                run_opts={"device": "cpu"})

    def embed(self, windows):
        out = []
        if self.onnx:
            for i in range(0, len(windows), 64):
                feats = np.stack([kaldi_fbank(w) for w in windows[i:i + 64]])
                out.append(self.model.run(None, {self.input: feats})[0])
        else:
            import torch
            with torch.no_grad():
                for i in range(0, len(windows), 64):
                    batch = torch.from_numpy(np.stack(windows[i:i + 64]))
                    out.append(self.model.encode_batch(batch).squeeze(1).numpy())
        e = np.concatenate(out)
        return e / np.linalg.norm(e, axis=1, keepdims=True)


class VoiceLibrary:
    """Named voiceprints in work_dir/voices.json: {name: {"embedding": [...], "n": k}}."""

    def __init__(self, cfg):
        self.path = cfg["work_dir"] / "voices.json"
        self.voices = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.voices))
        tmp.replace(self.path)

    def add(self, name, vec, weight=1):
        v = np.asarray(vec, dtype=np.float64)
        if name in self.voices:  # running mean of every meeting this voice was confirmed in
            old, n = np.asarray(self.voices[name]["embedding"]), self.voices[name]["n"]
            v = old * n + v * weight
            weight += n
        self.voices[name] = {"embedding": (v / np.linalg.norm(v)).tolist(), "n": weight}

    def best_match(self, vec, exclude):
        best, sim = None, -1.0
        for name, v in self.voices.items():
            if name in exclude:
                continue
            s = float(np.dot(vec, v["embedding"]))
            if s > sim:
                best, sim = name, s
        return best, sim


def average_linkage_clusters(emb, t):
    """Labels 0..k-1 for fcluster(linkage(emb, "average", metric="cosine"), t,
    criterion="distance"): unit vectors joined while their groups' mean cosine distance
    is at most t. scipy where it loads; on Windows, where Smart App Control blocks
    scipy's DLLs, the same clustering by nearest-neighbour chain in numpy."""
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
        return fcluster(linkage(emb, "average", metric="cosine"), t=t, criterion="distance") - 1
    except ImportError:
        return average_linkage_np(emb, t)


def average_linkage_np(emb, t):
    n = len(emb)
    d = 1 - np.clip(emb @ emb.T, -1, 1)
    np.fill_diagonal(d, np.inf)
    size, parent, alive, chain = np.ones(n), np.arange(n), np.ones(n, bool), []
    for _ in range(n - 1):
        if not chain:
            chain = [int(np.argmax(alive))]
        while True:
            a = chain[-1]
            b = int(np.argmin(d[a]))
            if len(chain) > 1 and d[a, chain[-2]] <= d[a, b]:
                b = chain[-2]
            if len(chain) > 1 and b == chain[-2]:
                break
            chain.append(b)
        chain = chain[:-2]
        if d[a, b] <= t:
            parent[parent == b] = a
        row = (size[a] * d[a] + size[b] * d[b]) / (size[a] + size[b])
        d[a], d[:, a] = row, row
        d[b], d[:, b] = np.inf, np.inf
        d[a, a] = np.inf
        alive[b] = False
        size[a] += size[b]
    return np.unique(parent, return_inverse=True)[1]


def diarize(cfg, audio, encoder):
    """Cluster 2 s voice-embedding windows over the speech regions.
    Returns (window_centers, window_labels, {label: centroid}, {label: seconds})."""
    clips = speech_regions(audio)
    centers = []
    for s, e in zip(clips[::2], clips[1::2]):
        if e - s < 0.8:
            continue
        if e - s <= EMBED_WIN_S:
            centers.append((s + e) / 2)
        else:
            centers += list(np.arange(s + EMBED_WIN_S / 2, e - EMBED_WIN_S / 2 + 1e-6, EMBED_HOP_S))
    if not centers:
        return [], [], {}, {}
    w = int(EMBED_WIN_S * TARGET_SR)
    padded = np.pad(audio, (0, max(0, w - len(audio))))
    windows = []
    for c in centers:
        a = min(max(0, int(c * TARGET_SR) - w // 2), len(padded) - w)
        windows.append(padded[a:a + w])
    emb = encoder.embed(windows)
    voiced = voiced_frames(audio)
    fps = 1 / 0.03
    solid = np.array([voiced[int((c - EMBED_WIN_S / 2) * fps):int((c + EMBED_WIN_S / 2) * fps)].sum() / fps
                      >= SOLID_VOICED_S for c in centers])
    if solid.sum() < 2:  # hardly any real speech: use what there is
        solid[:] = True
    se = emb[solid]
    if len(se) == 1:
        sl = np.zeros(1, int)
    else:
        sl = average_linkage_clusters(se, cfg["speaker_cluster_threshold"])
    sl = tidy_clusters(cfg, se, sl)
    cents = np.stack([se[sl == k].mean(axis=0) for k in range(sl.max() + 1)])
    labels = np.empty(len(emb), int)
    labels[solid] = sl
    if (~solid).any():
        labels[~solid] = np.argmax(emb[~solid] @ cents.T, axis=1)
    centroids, seconds = {}, {}
    for k in sorted(set(labels.tolist())):
        c = se[sl == k].mean(axis=0)  # voiceprints come from solid speech only
        centroids[k] = c / np.linalg.norm(c)
        seconds[k] = float((labels == k).sum() * EMBED_HOP_S)
    return np.array(centers), labels, centroids, seconds


def tidy_clusters(cfg, emb, labels):
    """2 s windows of compressed Teams audio are noisy, so raw clustering splits each
    voice into a few big clusters plus dozens of 2-6 s scraps (laughs, "yeah",
    crosstalk). Merge clusters that sound alike, then hand the windows of any cluster
    that talked too little to the closest surviving voice. Returns labels 0..n-1,
    largest cluster first."""
    def unit(v):
        return v / np.linalg.norm(v)
    groups = {k: list(np.flatnonzero(labels == k)) for k in set(labels.tolist())}
    cent = {k: unit(emb[ix].mean(axis=0)) for k, ix in groups.items()}
    # 1. Merge the most similar pair while it clears the threshold.
    while len(groups) > 1:
        ks = list(groups)
        sims = [(float(cent[a] @ cent[b]), a, b) for i, a in enumerate(ks) for b in ks[i + 1:]]
        s, a, b = max(sims)
        if s < cfg["speaker_merge_similarity"]:
            break
        groups[a] += groups.pop(b)
        cent.pop(b)
        cent[a] = unit(emb[groups[a]].mean(axis=0))
    # 2. Drop clusters below the talk-time floor (and beyond max_speakers).
    secs = {k: len(ix) * EMBED_HOP_S for k, ix in groups.items()}
    keep = sorted((k for k in groups if secs[k] >= cfg["speaker_min_seconds"]), key=lambda k: -secs[k])
    keep = keep or [max(secs, key=secs.get)]
    if cfg["max_speakers"] > 0:
        keep = keep[:cfg["max_speakers"]]
    out = np.empty(len(labels), int)
    for new, k in enumerate(keep):
        out[groups[k]] = new
    for k, ix in groups.items():
        if k not in keep:
            for i in ix:
                out[i] = max(range(len(keep)), key=lambda n: float(emb[i] @ cent[keep[n]]))
    return out


def identify(cfg, centroids, seconds, library):
    """Map cluster ids to names: known voiceprints first (most-talking speaker picks
    first, each name used once), everyone else becomes Speaker 1, 2, ..."""
    names, info, used, n_unknown = {}, {}, set(), 0
    for k in sorted(centroids, key=lambda k: -seconds[k]):
        name, sim = library.best_match(centroids[k], used)
        if name and sim >= cfg["speaker_match_threshold"]:
            used.add(name)
            names[k] = name
            known = True
        else:
            n_unknown += 1
            names[k] = f"Speaker {n_unknown}"
            known = False
        info[names[k]] = {"centroid": centroids[k].tolist(), "seconds": round(seconds[k]),
                          "known": known, "similarity": round(sim, 3) if name else None}
    return names, info


VOICES_BEGIN, VOICES_END = "<!-- unrecognized-voices -->", "<!-- /unrecognized-voices -->"
SAMPLE_SECONDS = 10


def save_voice_samples(session_dir, audio, segments, speakers):
    """For each unrecognized voice, keep a ~10 s clip of its longest lines (so you can
    hear who it is, even with keep_audio off) plus the first thing it said."""
    vdir = session_dir / "voices"
    for name, info in speakers.items():
        mine = [s for s in segments if s["speaker"] == name]
        if not mine:
            continue
        first = min(mine, key=lambda s: s["start"])
        text = first["text"] if len(first["text"]) <= 90 else first["text"][:87].rstrip() + "…"
        info["first"] = [first["start"], text.replace("|", "/")]
        if info["known"]:
            continue
        picked, total = [], 0.0
        for s in sorted(mine, key=lambda s: s["start"] - s["end"]):  # longest first
            if total >= SAMPLE_SECONDS:
                break
            picked.append(s)
            total += s["end"] - s["start"]
        gap = np.zeros(int(0.4 * TARGET_SR), np.float32)
        clip = np.concatenate(sum(([audio[int(s["start"] * TARGET_SR):int(s["end"] * TARGET_SR)], gap]
                                   for s in sorted(picked, key=lambda s: s["start"])), []))
        clip = clip[: (SAMPLE_SECONDS + 3) * TARGET_SR]
        vdir.mkdir(exist_ok=True)
        path = vdir / f"{safe_filename(name)}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_SR)
            w.writeframes((np.clip(clip, -1, 1) * 32767).astype(np.int16).tobytes())
        info["sample"] = path.name


def voices_section(speakers):
    """The note's "who is who" table for voices that still need a name ([] if none)."""
    unknown = [n for n, s in speakers.items() if not s["known"]]
    if not unknown:
        return []
    md = [VOICES_BEGIN, "## Unrecognized voices", "",
          "| Voice | First heard | They said |", "|---|---|---|"]
    md += [f"| {n} | {ts(speakers[n]['first'][0])} | “{speakers[n]['first'][1]}” |"
           for n in unknown if speakers[n].get("first")]
    md += ["", f"Run `{plat.CTL} listen` to hear each voice, then `{plat.CTL} name \"{unknown[0]}\" "
           "\"Full Name\"`. Named voices are recognized automatically in future meetings.",
           VOICES_END, ""]
    return md


def split_by_speaker(segments, centers, labels, names):
    """Give every word the speaker of the nearest embedding window, then cut each
    Whisper segment wherever the speaker changes."""
    out = []
    for seg in segments:
        words = seg["words"] or [(seg["start"], seg["end"], seg["text"])]
        tagged = []
        for ws, we, word in words:
            i = int(np.argmin(np.abs(centers - (ws + we) / 2)))
            tagged.append([ws, we, word, names[labels[i]]])
        # Whisper usually starts a new segment when the speaker changes, and 2 s
        # embedding windows blur the first/last word of a turn into the neighbour.
        # So a segment keeps one speaker unless another speaker owns a substantial
        # stretch of it (>= 1.5 s and 4 words); shorter runs are absorbed.
        while True:
            runs = []
            for t in tagged:
                if runs and runs[-1][-1][3] == t[3]:
                    runs[-1].append(t)
                else:
                    runs.append([t])
            short = [i for i, r in enumerate(runs)
                     if len(r) < 4 or r[-1][1] - r[0][0] < 1.5]
            if len(runs) == 1 or not short:
                break
            i = min(short, key=lambda i: runs[i][-1][1] - runs[i][0][0])
            nbrs = [j for j in (i - 1, i + 1) if 0 <= j < len(runs)]
            into = max(nbrs, key=lambda j: runs[j][-1][1] - runs[j][0][0])
            for t in runs[i]:
                t[3] = runs[into][0][3]
        cur = None
        for ws, we, word, spk in tagged:
            if cur and cur["speaker"] == spk:
                cur["text"] += word
                cur["end"] = we
            else:
                cur = {"start": ws, "end": we, "speaker": spk, "text": word,
                       "flag": seg.get("flag")}
                out.append(cur)
    for s in out:
        s["text"] = s["text"].strip()
    return [s for s in out if s["text"]]


def mark_echo(cfg, me, others):
    """If you're on speakers, your mic also hears the other side. Flag (or, with
    drop_suspect_lines, drop) "Me" segments that overlap in time with, and read
    like, a remote segment."""
    kept = []
    for m in me:
        echo = None
        for o in others:
            if o["start"] > m["end"]:
                break
            # Acoustic echo is simultaneous with the original, so require real overlap.
            # (Adjacent lines, e.g. the Teams Echo bot replaying you, are not echo.)
            overlap = min(m["end"], o["end"]) - max(m["start"], o["start"])
            shorter = min(m["end"] - m["start"], o["end"] - o["start"])
            if overlap >= 0.5 * shorter and difflib.SequenceMatcher(
                    None, m["text"].lower(), o["text"].lower()).ratio() > 0.6:
                echo = o
                break
        if echo:
            log.info("possible speaker echo: [%s] %r (matches %r)",
                     ts(m["start"]), m["text"], echo["text"])
            if cfg["drop_suspect_lines"]:
                continue
            m["flag"] = "possible echo of the remote side"
        kept.append(m)
    return kept


def merge_turns(segments, gap=1.5):
    turns = []
    for s in sorted(segments, key=lambda s: s["start"]):
        if (turns and turns[-1]["speaker"] == s["speaker"] and s["start"] - turns[-1]["end"] <= gap
                and turns[-1].get("flag") == s.get("flag")):  # keep flagged lines separate
            turns[-1]["text"] += " " + s["text"]
            turns[-1]["end"] = max(turns[-1]["end"], s["end"])
        else:
            turns.append(dict(s))
    return turns


def ts(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


SYSTEM_PROMPT = ("You are a meticulous meeting assistant. Use only information present in the "
                 "transcript. Never invent names, dates, numbers, or decisions.")

FINAL_INSTRUCTIONS = """The material below comes from a Microsoft Teams meeting. Lines labelled "{me}" are the person these notes belong to. Every other label is one remote participant, identified by voice: a real name when the voice was recognized, otherwise "Speaker 1", "Speaker 2", and so on. Keep "Speaker N" labels as they are unless that person's name is clearly stated in the conversation.

Respond in Markdown with exactly these two sections and nothing before or after them:

## Summary
- 4 to 10 concise bullets: topics discussed, decisions made, open questions.

## Action Items
- [ ] **Owner**: task (due date if one was stated)
Include every task someone volunteered for, agreed to, or was asked to do. Use "{me}" for tasks I committed to, otherwise the speaker label or name of whoever took the task, or "Unassigned". If there are none, write "- None identified."
"""

CHUNK_INSTRUCTIONS = """This is part {i} of {n} of a Microsoft Teams meeting transcript. "{me}" is the note-taker; every other label is one remote participant. Extract terse bullets of: key points, decisions, and action items (with owner if known). Output bullets only."""

WORK_FILTER_INSTRUCTIONS = """Below are numbered lines from a Microsoft Teams work meeting. Find the lines that are NOT work related.

Not work related: small talk and personal conversation, such as weekend stories, family, health, food, sports, weather, jokes, personal plans, gossip, "how are you" greetings, and chit-chat while waiting for people to join.

Work related (never list these): projects, tasks, tickets, data, code, systems, customers, deadlines, decisions, status updates, questions about the work, and meeting logistics ("can you see my screen", "let's get started"). Holidays, vacations and time off are work related when they affect schedules, change freezes, releases or coverage. Short replies such as "yes", "okay", "makes sense" or "sorry" belong to whatever they answer. When unsure, treat the line as work related.

Answer with exactly one line: OFF: followed by the numbers or ranges of the lines that are not work related, e.g. "OFF: 3, 7-9". If every line is work related, answer "OFF: none"."""

# Asking for a category, not "work or not", keeps an 8B model from calling terse work
# questions ("What did the DBS say?") small talk. Only C and D are dropped.
WORK_CONFIRM_INSTRUCTIONS = """Here is an excerpt from a Microsoft Teams work meeting. Classify the line marked >>> into one category:

A) Work: anything about projects, systems, servers, tables, data, tickets, tasks, customers, status, questions to a colleague, who is available or out, schedules, holidays or change freezes that affect the work, or running the meeting ("let's get started", "I'll send it in the chat").
B) Brief reply or filler: "yes", "okay", "makes sense", "thank you", "sorry", "mm-hmm", or a garbled fragment.
C) Greeting or goodbye: "hi", "how are you", "doing good", "bye", "see you".
D) Personal small talk: someone's private life, weekend, family, health, weather, sports, food, or jokes unrelated to work.

{excerpt}

If the line could be A, answer A. Answer with the letter only."""


class LLM:
    def __init__(self, cfg, mlx_model=None):
        self.cfg = cfg
        self.backend = cfg["llm_backend"]
        if self.backend == "mlx":
            from mlx_lm import load
            mlx_model = mlx_model or cfg["mlx_llm_model"]
            log.info("loading %s", mlx_model)
            self.model, self.tok = load(mlx_model)
        elif self.backend == "llamacpp":
            from llama_cpp import Llama
            path = gguf_path(cfg)
            log.info("loading %s", path.name)
            self.model = Llama(model_path=str(path), n_ctx=16384, n_gpu_layers=-1, verbose=False)
        elif self.backend == "ollama":
            host = urllib.parse.urlparse(cfg["ollama_url"]).hostname
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError(f"refusing non-local Ollama URL {cfg['ollama_url']}")
        else:
            raise ValueError(f"unknown llm_backend {self.backend!r}")

    def chat(self, user, max_tokens=1500):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        if self.backend == "mlx":
            from mlx_lm import generate
            from mlx_lm.sample_utils import make_sampler
            prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
            return generate(self.model, self.tok, prompt=prompt, max_tokens=max_tokens,
                            sampler=make_sampler(temp=0.2), verbose=False).strip()
        if self.backend == "llamacpp":
            r = self.model.create_chat_completion(messages=msgs, temperature=0.2, max_tokens=max_tokens)
            return r["choices"][0]["message"]["content"].strip()
        body = json.dumps({"model": self.cfg["ollama_model"], "messages": msgs, "stream": False,
                           "options": {"temperature": 0.2, "num_ctx": 16384,
                                       "num_predict": max_tokens}}).encode()
        req = urllib.request.Request(self.cfg["ollama_url"].rstrip("/") + "/api/chat", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.loads(r.read())["message"]["content"].strip()


def gguf_path(cfg, download=False):
    """Local file of cfg["gguf_model"] ("owner/repo/file.gguf") in the Hugging Face cache."""
    from huggingface_hub import hf_hub_download
    owner, repo, filename = cfg["gguf_model"].split("/", 2)
    return Path(hf_hub_download(f"{owner}/{repo}", filename, local_files_only=not download))


def filter_is_summary_model(cfg):
    return (cfg["llm_backend"] != "mlx"
            or cfg["work_filter_model"] in ("", cfg["mlx_llm_model"]))


def work_only(cfg, llm, segments):
    """Keep only work discussion: the model lists the lines that are small talk or
    personal, batch by batch, then each candidate is re-checked with the lines around
    it and dropped only if that confirms it. A batch whose answer can't be parsed is
    kept whole, so a confused model never deletes work content wholesale."""
    segs = sorted(segments, key=lambda s: s["start"])
    lines = [f"[{i}] {s['speaker']}: {s['text']}\n" for i, s in enumerate(segs, 1)]
    candidates, limit, start = set(), cfg["work_filter_chunk_chars"], 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and (end == start or size + len(lines[end]) <= limit):
            size += len(lines[end])
            end += 1
        answer = llm.chat(WORK_FILTER_INSTRUCTIONS + "\n\n" + "".join(lines[start:end]), 400)
        m = re.search(r"OFF:\s*(.*)", answer, re.I | re.S)
        if not m:
            log.warning("work filter: no usable answer for lines %d-%d; keeping them", start + 1, end)
        else:
            for a, b in re.findall(r"(\d+)(?:\s*[-–]\s*(\d+))?", m.group(1)):
                candidates.update(n - 1 for n in range(int(a), int(b or a) + 1) if start < n <= end)
        start = end
    drop = set()
    for i in sorted(candidates):
        excerpt = "".join((">>> " if j == i else "    ") + lines[j][lines[j].index("]") + 2:]
                          for j in range(max(0, i - 3), min(len(lines), i + 3)))
        answer = llm.chat(WORK_CONFIRM_INSTRUCTIONS.format(excerpt=excerpt.rstrip()), 5)
        if answer.strip()[:1].upper() in ("C", "D"):
            drop.add(i)
    log.info("work filter: dropped %d of %d lines as not work related", len(drop), len(segs))
    return [s for i, s in enumerate(segs) if i not in drop], len(drop)


def summarize(cfg, transcript, llm):
    names = {"me": cfg["my_name"], "others": cfg["others_name"]}
    limit = cfg["llm_chunk_chars"]
    if len(transcript) <= limit:
        material = "TRANSCRIPT:\n" + transcript
    else:  # map-reduce for long meetings
        lines, chunks, cur = transcript.splitlines(keepends=True), [], ""
        for line in lines:
            if len(cur) + len(line) > limit and cur:
                chunks.append(cur)
                cur = ""
            cur += line
        chunks.append(cur)
        notes = []
        for i, c in enumerate(chunks, 1):
            log.info("summarizing chunk %d/%d", i, len(chunks))
            notes.append(f"### Part {i}\n" + llm.chat(
                CHUNK_INSTRUCTIONS.format(i=i, n=len(chunks), **names) + "\n\n" + c, 1000))
        material = "NOTES FROM EACH PART OF THE MEETING:\n" + "\n\n".join(notes)
    out = llm.chat(FINAL_INSTRUCTIONS.format(**names) + "\n" + material)
    i = out.find("## Summary")
    out = out[i:] if i >= 0 else out
    # Small models often drop the checkbox syntax; make every action item a task.
    head, sep, items = out.partition("## Action Items")
    items = re.sub(r"(?m)^\s*[-*]\s+(?!\[[ x]\])(?!None identified)", "- [ ] ", items)
    return head + sep + items


def safe_filename(s):
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", s).strip()
    return re.sub(r"\s+", " ", s)[:80] or "Teams meeting"


def process_session(cfg, session_dir):
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text())
    duration = meta.get("duration_s", 0)
    log.info("processing %s (%s, %.0f min)", session_dir.name, meta["title"], duration / 60)

    def load(name):
        p = session_dir / name
        return (np.fromfile(p, dtype=np.int16).astype(np.float32) / 32768.0
                if p.exists() else np.zeros(0, np.float32))

    mic, sysa = load("mic.pcm"), load("system.pcm")

    me = transcribe_track(cfg, mic, cfg["my_name"])
    others = transcribe_track(cfg, sysa, cfg["others_name"])
    release_whisper()
    me = mark_echo(cfg, me, others)

    speakers = {}
    if cfg["speaker_id"] and others:
        try:
            log.info("identifying speakers on the %s track", cfg["others_name"])
            library = VoiceLibrary(cfg)
            centers, labels, centroids, seconds = diarize(cfg, sysa, VoiceEncoder(cfg))
            if len(centers):
                names, speakers = identify(cfg, centroids, seconds, library)
                others = split_by_speaker(others, centers, labels, names)
                log.info("speakers: %s", {n: (s["seconds"], s["similarity"])
                                          for n, s in speakers.items()})
                for name, s in speakers.items():  # confident matches refine the voiceprint
                    if s["known"] and s["similarity"] >= cfg["speaker_match_threshold"] + 0.1:
                        library.add(name, s["centroid"])
                library.save()
        except Exception:
            log.exception("speaker identification failed; falling back to '%s'",
                          cfg["others_name"])
            speakers = {}

    segments, dropped, llm = me + others, None, None
    if cfg["work_only"] and segments:
        flt = LLM(cfg, cfg["work_filter_model"])
        segments, dropped = work_only(cfg, flt, segments)
        if filter_is_summary_model(cfg):
            llm = flt  # one model load serves both steps
        else:
            del flt
            try:  # free the filter model before the summary model loads
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass
    if speakers:  # after filtering, so voice samples and quotes come from work lines only
        try:
            save_voice_samples(session_dir, sysa, segments, speakers)
        except Exception:
            log.exception("could not save voice samples")
    (session_dir / "speakers.json").write_text(json.dumps(speakers))

    turns = merge_turns(segments)
    for t in turns:
        t.pop("words", None)
    (session_dir / "transcript.json").write_text(json.dumps(turns, indent=2, ensure_ascii=False))
    out = write_note(cfg, meta, turns, speakers, llm, dropped)

    if cfg["keep_audio"]:
        n = max(len(mic), len(sysa))
        stereo = np.zeros((n, 2), np.int16)
        stereo[: len(mic), 0] = (mic * 32767).astype(np.int16)
        stereo[: len(sysa), 1] = (sysa * 32767).astype(np.int16)
        with wave.open(str(out.with_suffix(".wav")), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(TARGET_SR)
            w.writeframes(stereo.tobytes())
    for p in ("mic.pcm", "system.pcm"):
        (session_dir / p).unlink(missing_ok=True)

    meta.update(status="done", notes=str(out), work_filtered=dropped is not None)
    meta_path.write_text(json.dumps(meta, indent=2))
    log.info("wrote %s", out)
    notify(cfg, f"Meeting notes ready: {out.name}" +
           (f". Run {plat.CTL} jira to review tickets." if cfg["jira"]["base_url"] else ""))


def write_note(cfg, meta, turns, speakers, llm=None, dropped=None):
    """Summarize the turns and write the meeting's Markdown note; returns its path."""
    started = dt.datetime.fromisoformat(meta["started_at"])
    duration = meta.get("duration_s", 0)

    def tag(t, fmt):
        return fmt.format(t["flag"]) if t.get("flag") else ""

    transcript = "\n".join(f"[{ts(t['start'])}] {t['speaker']}: {t['text']}{tag(t, ' ({})')}"
                           for t in turns)

    if transcript.strip():
        notes = summarize(cfg, transcript, llm or LLM(cfg))
    else:
        what = "work discussion" if dropped else "speech"
        notes = f"## Summary\n- No {what} was detected in this recording.\n\n## Action Items\n- None identified."

    end = started + dt.timedelta(seconds=duration)
    title = meta["title"]
    md = [f"# {title}", "",
          f"**Date:** {started:%A %d %B %Y, %H:%M}–{end:%H:%M} ({round(duration / 60)} min)  ",
          f"**Source:** Microsoft Teams · recorded, transcribed and summarized on this {plat.DEVICE}  "]
    if speakers:
        md.append("**Participants:** " + ", ".join(
            [cfg["my_name"]] + [n if s["known"] else f"{n} (unrecognized)"
                                for n, s in speakers.items()]) + "  ")
    if dropped:
        md.append(f"**Filtered:** {dropped} non-work line(s) left out  ")
    md += ["", notes.strip(), ""]
    md += voices_section(speakers)
    md += ["---", "", "## Transcript", ""]
    md += [f"**[{ts(t['start'])}] {t['speaker']}:** {t['text']}{tag(t, ' _({})_')}  "
           for t in turns] or ["_(empty)_"]

    cfg["output_dir"].mkdir(parents=True, exist_ok=True)
    out = cfg["output_dir"] / f"{started:%Y-%m-%d %H%M} - {safe_filename(title)}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# Jira: the local model drafts tickets from a meeting, you approve each one
# --------------------------------------------------------------------------

JIRA_BEGIN, JIRA_END = "<!-- jira -->", "<!-- /jira -->"
KEYCHAIN_SERVICE = "MeetingScribe Jira"
ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9_]{1,9}-\d+\b")
SEARCH_FIELDS = ["summary", "status", "issuetype", "project", "updated", "description", "assignee"]
# Set by code or by Jira itself, never by the model.
FIELDS_NOT_FROM_MODEL = {"project", "issuetype", "reporter", "assignee", "attachment", "issuelinks",
                         "parent", "timetracking", "worklog", "watches", "sprint"}

JIRA_EXTRACT_INSTRUCTIONS = """Below are the notes and transcript of a work meeting held on {date}. "{me}" is the person these notes belong to.

Calendar for resolving due dates. "Today" is the meeting day; "by Thursday" is the first Thursday after the meeting day:
{calendar}

List the pieces of work from this meeting that belong in a Jira ticket tracker: action items, bugs or incidents reported, follow-ups, requests, and decisions that need work. Leave out work that was finished during the meeting, pure information, and trivial chores such as "send the invite" or "share my screen".

Answer with a JSON array only, no prose. One object per piece of work:
{{"title": "short imperative title, under 90 characters",
  "details": "2 to 5 sentences: what needs doing, why, and anything specific that was said (systems, tables, environments, numbers, errors)",
  "owner": "who will do it, exactly as labelled in the transcript, or empty if nobody",
  "due": "YYYY-MM-DD from the calendar if a due date was stated or clearly implied, else empty",
  "kind": "Task", "Bug" or "Story",
  "priority": "High", "Medium" or "Low"; High only when the meeting called it urgent or blocking,
  "keywords": ["3 to 6 distinctive search terms: system, table, job, environment or feature names, and any ticket keys mentioned; no generic words such as update, issue or fix"],
  "evidence": "the transcript line showing this work was agreed, quoted exactly"}}
If nothing is ticket-worthy, answer [].

NOTES:
{notes}

TRANSCRIPT:
{transcript}"""

# Labelling each ticket is easier for a 14B model than picking one action; code applies the rules.
JIRA_DECIDE_INSTRUCTIONS = """A meeting produced this piece of work:

{candidate}

A search across every Jira space found these existing tickets, most relevant first:

{tickets}

Label every ticket above:
- "same": the ticket is about this exact deliverable: the same task on the same system, table, job or feature. Example: "repoint the reporting jobs to Delta Lake" and "Nightly reporting jobs fail after migration" are the same.
- "related": same project, system or migration, but a different deliverable. Example: "validate store_sales row counts" is only related to "reporting jobs fail after migration", and a firewall request is only related to a data migration task.
- "unrelated": anything else.

Also answer:
- "recurring": true if the meeting says this problem came back or happened again, else false.
- "worth_a_ticket": false only if the work is trivial or too vague to act on.

Answer with one JSON object only:
{{"labels": {{"KEY-1": "same" or "related" or "unrelated", ...}}, "recurring": true or false, "worth_a_ticket": true or false, "reason": "one sentence"}}"""


def decide(labels, found, recurring, worth):
    """(action, key, related) from the model's labels. A Done ticket is never commented on:
    if the problem is back, a new ticket links to it; if not, the work is already covered."""
    same = [k for k in found if labels.get(k) == "same"]
    related = [k for k in found if labels.get(k) == "related"]
    open_same = [k for k in same if not is_done(found[k])]
    if not worth:
        return "skip", "", related
    if open_same:
        return "comment", open_same[0], related
    if same and not recurring:
        return "skip", same[0], related
    return "create", "", same + related


JIRA_FILL_INSTRUCTIONS = """Write a Jira {issue_type} in space {project} for this piece of work from the meeting "{title}" on {date}:

{candidate}

Fields, as [field id] name (type). Where allowed values are listed, use exactly one of them as spelled (a list of them for array fields), or leave the field out if none fits:
{fields}

Rules:
- "summary": imperative and specific, under 100 characters.
- "description": Jira wiki markup with these parts: "h3. Context" (why, from the meeting), "h3. What needs doing" (bullets starting with *), "h3. Acceptance criteria" (bullets), then a last line "_Raised in the meeting \\"{title}\\" on {date}._". Use only facts from the material; never invent numbers, names or dates.
- "duedate": YYYY-MM-DD, only if the work has a due date.
- "labels": at most 2 short topic labels naming systems or areas (e.g. "delta-lake"), never status words such as urgent.
- Leave out any field the material gives you nothing for.

Answer with one JSON object only, keyed by the field ids in brackets."""


class JiraError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def keychain(action, account, secret=None):
    """macOS login Keychain via the Security framework (keyring), so a secret is never put on a
    command line where other processes could read it. "get" returns the secret or None."""
    import keyring
    import keyring.errors
    if action == "get":
        return keyring.get_password(KEYCHAIN_SERVICE, account)
    if action == "set":
        keyring.set_password(KEYCHAIN_SERVICE, account, secret)
        return None
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, account)
    except keyring.errors.PasswordDeleteError:
        pass
    return None


def require_https(url):
    """Tokens and cookies only travel over HTTPS. Plain http is allowed for this computer alone (tests)."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    sys.exit(f"refusing Jira URL {url!r}: it must start with https:// so your token or cookie is encrypted")


AUTH_CHOICES = {
    "1": ("bearer", "Personal access token (Jira Server / Data Center)"),
    "2": ("basic", "API token + email (Jira Cloud)"),
    "3": ("cookie", "Browser session cookie (works on SSO-only sites)"),
}
COOKIE_HELP = """  To copy your session cookie: open Jira in your browser while signed in, open the developer
  tools (Network tab), reload, click any request to your Jira site, and copy the whole value of
  the "Cookie" request header. It expires when your Jira session does; you'll be asked again then."""


def jira_credentials(j, ask=False):
    """(auth, secret, email, source). Uses the system keyring, otherwise asks (input hidden)."""
    base = j["base_url"].rstrip("/")
    auth = j["auth"].lower()
    if not auth and not ask:
        auth = next((a for a, _ in AUTH_CHOICES.values() if keychain("get", f"{base}#{a}")), "")
    if not auth:
        print("How do you want to sign in to Jira?")
        for n, (_, label) in AUTH_CHOICES.items():
            print(f"  {n}) {label}")
        auth = AUTH_CHOICES.get(input("Choose 1, 2 or 3: ").strip(), AUTH_CHOICES["3"])[0]
    email = j["email"]
    if auth == "basic" and not email:
        email = keychain("get", f"{base}#email") or input("Jira account email: ").strip()
    if not ask and (secret := keychain("get", f"{base}#{auth}")):
        return auth, secret, email, "keychain"
    if auth == "cookie":
        print(COOKIE_HELP)
    what = "session cookie" if auth == "cookie" else "access token"
    secret = getpass.getpass(f"Paste your Jira {what} (input is hidden): ").strip()
    if not secret:
        sys.exit("no credentials given")
    return auth, secret, email, "prompt"


class JiraClient:
    def __init__(self, j, auth, secret, email=""):
        require_https(j["base_url"])
        self.base = j["base_url"].rstrip("/")
        self.cloud = j["deployment"].lower() == "cloud"
        self.timeout = j["timeout_seconds"]
        self.headers = {"Accept": "application/json", "User-Agent": "MeetingScribe/1.0"}
        if auth == "cookie":
            self.headers["Cookie"] = secret
        elif auth == "basic":
            self.headers["Authorization"] = "Basic " + base64.b64encode(f"{email}:{secret}".encode()).decode()
        else:
            self.headers["Authorization"] = "Bearer " + secret
        if not j["verify_ssl"]:
            print("WARNING: verify_ssl = false; Jira's certificate is NOT checked, so your token or cookie "
                  "could be intercepted. Use ca_bundle instead if your network inspects HTTPS.")
            self.ctx = ssl._create_unverified_context()
        else:
            self.ctx = ssl.create_default_context(cafile=str(Path(j["ca_bundle"]).expanduser())
                                                  if j["ca_bundle"] else None)
            if not j["ssl_strict"]:  # corporate TLS-inspection CAs often lack keyUsage
                self.ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

    def request(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers, data = dict(self.headers), None
        if body is not None:
            data = json.dumps(body).encode()
            # Jira's XSRF check rejects cookie-authenticated writes without this header.
            headers.update({"Content-Type": "application/json", "X-Atlassian-Token": "no-check"})
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw, ctype = r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            hint = {401: " (credentials rejected or expired)", 403: " (no permission)",
                    404: " (not found: wrong base_url or deployment?)"}.get(e.code, "")
            raise JiraError(f"HTTP {e.code}{hint} for {method} {path}: {detail}", e.code) from None
        except urllib.error.URLError as e:
            raise JiraError(f"cannot reach {self.base}: {e.reason}") from None
        if not raw:
            return {}
        if "json" not in ctype:  # an SSO login page: the cookie or token wasn't accepted
            raise JiraError(f"{path} returned {ctype or 'non-JSON'}; most likely a login page", 401)
        return json.loads(raw)

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def post(self, path, body):
        return self.request("POST", path, body=body)

    def myself(self):
        return self.get("/rest/api/2/myself")

    def projects(self):
        return self.get("/rest/api/2/project")

    def issue(self, key):
        return self.get(f"/rest/api/2/issue/{key}", {"fields": ",".join(SEARCH_FIELDS)})

    def search(self, jql, limit):
        issues, token = [], None
        while len(issues) < limit:
            params = {"jql": jql, "fields": ",".join(SEARCH_FIELDS), "maxResults": min(100, limit - len(issues))}
            if self.cloud:  # Cloud's enhanced search pages with a token
                if token:
                    params["nextPageToken"] = token
                page = self.get("/rest/api/2/search/jql", params)
                token = page.get("nextPageToken")
                done = page.get("isLast", True) or not token
            else:
                params["startAt"] = len(issues)
                page = self.get("/rest/api/2/search", params)
                done = len(issues) + len(page.get("issues", [])) >= page.get("total", 0)
            issues += page.get("issues", [])
            if done or not page.get("issues"):
                break
        return issues

    def _paged(self, path):
        out = []
        while True:
            page = self.get(path, {"startAt": len(out), "maxResults": 100})
            items = page.get("issueTypes") or page.get("fields") or page.get("values") or []
            out += items
            if not items or len(out) >= page.get("total", len(out)):
                return out

    def issue_types(self, project):
        return [t for t in self._paged(f"/rest/api/2/issue/createmeta/{project}/issuetypes")
                if not t.get("subtask")]

    def create_fields(self, project, type_id):
        return {f.get("fieldId") or f.get("key"): f
                for f in self._paged(f"/rest/api/2/issue/createmeta/{project}/issuetypes/{type_id}")}

    def find_user(self, project, name):
        """Field value for an assignable user whose display name matches, or None."""
        key = "query" if self.cloud else "username"
        users = self.get("/rest/api/2/user/assignable/search", {"project": project, key: name})
        exact = [u for u in users if u.get("displayName", "").lower() == name.lower()]
        users = exact or (users if len(users) == 1 else [])
        if not users:
            return None
        u = users[0]
        return {"accountId": u["accountId"]} if "accountId" in u else {"name": u["name"]}

    def create(self, fields):
        return self.post("/rest/api/2/issue", {"fields": fields})

    def comment(self, key, body):
        return self.post(f"/rest/api/2/issue/{key}/comment", {"body": body})

    def link(self, key, other):
        return self.post("/rest/api/2/issueLink", {"type": {"name": "Relates"},
                                                   "inwardIssue": {"key": key},
                                                   "outwardIssue": {"key": other}})

    def url(self, key):
        return f"{self.base}/browse/{key}"


def parse_llm_json(text, want):
    """The first JSON value of type `want` in a model answer (code fences and prose tolerated)."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    s = m.group(1) if m else text
    i = s.find("[" if want is list else "{")
    if i < 0:
        raise ValueError("no JSON in answer")
    value, _ = json.JSONDecoder().raw_decode(s[i:])
    if not isinstance(value, want):
        raise ValueError(f"expected {want.__name__}")
    return value


def llm_json(llm, prompt, want, max_tokens=1500):
    for attempt in range(2):
        answer = llm.chat(prompt if not attempt else
                          prompt + "\n\nAnswer with valid JSON only, nothing else.", max_tokens)
        try:
            return parse_llm_json(answer, want)
        except ValueError as e:
            log.warning("model answer was not usable JSON (%s): %.200s", e, answer)
    raise ValueError("the model did not return usable JSON")


def calendar_lines(day):
    return "\n".join(f"{d:%A %Y-%m-%d}" + (" (meeting day)" if not i else "")
                     for i, d in enumerate(day + dt.timedelta(days=n) for n in range(15)))


def is_done(issue):
    return ((issue["fields"].get("status") or {}).get("statusCategory") or {}).get("key") == "done"


def describe_issue(issue):
    f = issue["fields"]
    done = is_done(issue)
    desc = re.sub(r"\s+", " ", f.get("description") or "")[:300]
    who = (f.get("assignee") or {}).get("displayName", "unassigned")
    return (f"{issue['key']} [{(f.get('issuetype') or {}).get('name', '?')} · "
            f"{(f.get('status') or {}).get('name', '?')}{' (Done)' if done else ''} · {who} · "
            f"updated {(f.get('updated') or '')[:10]}] {f.get('summary', '')}"
            + (f" — {desc}" if desc else ""))


def describe_candidate(c):
    return "\n".join(f"{k}: {c[k]}" for k in ("title", "details", "owner", "due", "kind", "priority", "evidence")
                     if c.get(k))


def search_terms(c):
    terms = []
    for t in c.get("keywords", []) + ISSUE_KEY.findall(c.get("title", "") + " " + c.get("details", "")):
        t = re.sub(r'[+\-&|!(){}\[\]^~*?\\:"/]', " ", str(t))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) >= 3 and t.lower() not in (x.lower() for x in terms):
            terms.append(t)
    return terms[:6]


def related_tickets(client, cand, j):
    """The existing tickets, in any space, most likely to be about this work."""
    found = {}
    for key in set(ISSUE_KEY.findall(" ".join(str(cand.get(k, "")) for k in
                                              ("title", "details", "evidence", "keywords")))):
        try:
            found[key] = client.issue(key)
        except JiraError:
            pass
    terms = search_terms(cand)
    if terms:
        clauses = " OR ".join(f'text ~ "\\"{t}\\""' if " " in t else f'text ~ "{t}"' for t in terms)
        jql = f"({clauses}) AND updated >= -{j['search_days']}d ORDER BY updated DESC"
        try:
            for issue in client.search(jql, 50):
                found.setdefault(issue["key"], issue)
        except JiraError as e:
            log.warning("related-ticket search failed: %s", e)

    def score(issue):
        f = issue["fields"]
        hay = f"{f.get('summary', '')} {f.get('description') or ''}".lower()
        hits = sum(t.lower() in hay for t in terms) + 3 * sum(t.lower() in f.get("summary", "").lower() for t in terms)
        return hits + 5 * difflib.SequenceMatcher(None, cand["title"].lower(), f.get("summary", "").lower()).ratio()

    ranked = sorted(found.values(), key=score, reverse=True)
    return ranked[: j["search_limit"]]


def field_prompt_lines(fields):
    lines = []
    for fid, f in fields.items():
        schema = f.get("schema", {})
        kind = schema.get("type", "string") + (f" of {schema['items']}" if schema.get("items") else "")
        line = f"- [{fid}] {f.get('name', fid)} ({kind}){' REQUIRED' if f.get('required') else ''}"
        allowed = [a.get("name") or a.get("value") for a in f.get("allowedValues", [])]
        if allowed:
            line += "; allowed: " + ", ".join(str(a) for a in allowed[:40] if a)
        lines.append(line)
    return "\n".join(lines)


def fields_for_model(fields):
    """The fields worth asking the model about: the usual ones plus anything required."""
    usual = {"summary", "description", "priority", "labels", "duedate", "components"}
    return {fid: f for fid, f in fields.items()
            if fid not in FIELDS_NOT_FROM_MODEL and (fid in usual or f.get("required"))
            and f.get("schema", {}).get("type") != "user"}


def to_api(value, f):
    """A human-readable field value -> the JSON Jira expects. Raises ValueError if it can't."""
    schema, allowed = f.get("schema", {}), f.get("allowedValues", [])
    many = schema.get("type") == "array"
    values = value if isinstance(value, list) else (
        [v.strip() for v in str(value).split(",") if v.strip()] if many else [value])
    out = []
    for v in values:
        if allowed:
            hit = next((a for a in allowed if str(v).strip().lower() in
                        (str(a.get("name", "")).lower(), str(a.get("value", "")).lower())), None)
            if not hit:
                raise ValueError(f"{v!r} is not one of: " + ", ".join(
                    str(a.get("name") or a.get("value")) for a in allowed[:15]))
            out.append({"id": hit["id"]})
        elif schema.get("type") == "date" or schema.get("items") == "date":
            dt.date.fromisoformat(str(v))
            out.append(str(v))
        elif schema.get("type") == "number":
            out.append(float(v))
        elif schema.get("items") == "string" and schema.get("system") == "labels":
            out.append(re.sub(r"\s+", "-", str(v).strip()))
        elif schema.get("type") in ("string", None) or schema.get("items") == "string":
            out.append(str(v))
        else:
            raise ValueError(f"MeetingScribe can't fill a {schema.get('type')} field")
    return out if many else (out[0] if out else None)


def shown(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


class TicketDraft:
    def __init__(self, client, project, cand, meeting, llm, types, me):
        self.client, self.project, self.cand, self.meeting, self.llm = client, project, cand, meeting, llm
        self.types, self.me = types, me
        self.related, self.assignee = [], ("", None)
        names = {t["name"].lower(): t for t in types}
        self.type = names.get(cand.get("kind", "").lower()) or names.get("task") or types[0]
        self.fill()

    def fill(self):
        self.fields = self.client.create_fields(self.project, self.type["id"])
        ask = fields_for_model(self.fields)
        prompt = JIRA_FILL_INSTRUCTIONS.format(
            issue_type=self.type["name"], project=self.project, title=self.meeting["title"],
            date=self.meeting["date"], candidate=describe_candidate(self.cand), fields=field_prompt_lines(ask))
        got = llm_json(self.llm, prompt, dict, 1500)
        self.values = {fid: v for fid, v in got.items() if fid in ask and v not in ("", [], None)}
        self.values.setdefault("summary", self.cand["title"])
        if "labels" in self.fields:
            labels = self.values.get("labels") or []
            labels = labels if isinstance(labels, list) else [labels]
            self.values["labels"] = list(dict.fromkeys(labels + list(self.meeting["labels"])))
        if self.cand.get("due") and "duedate" in self.fields:
            self.values.setdefault("duedate", self.cand["due"])
        self.set_assignee(self.cand.get("owner", ""))

    def set_assignee(self, who):
        who = (who or "").strip()
        if not who or re.fullmatch(r"(Speaker \d+|Unassigned|Others)", who, re.I):
            self.assignee = ("", None)
        elif who.lower() in (self.meeting["me"].lower(), "me"):
            self.assignee = (self.me["displayName"], {"accountId": self.me["accountId"]}
                             if "accountId" in self.me else {"name": self.me["name"]})
        else:
            try:
                self.assignee = (who, self.client.find_user(self.project, who))
            except JiraError:
                self.assignee = (who, None)

    def problems(self):
        out = {}
        for fid, v in self.values.items():
            try:
                to_api(v, self.fields[fid])
            except (ValueError, KeyError) as e:
                out[fid] = str(e)
        for fid, f in self.fields.items():
            if (f.get("required") and not f.get("hasDefaultValue") and fid not in self.values
                    and fid not in FIELDS_NOT_FROM_MODEL):
                out[fid] = "required, please set it"
        return out

    def payload(self):
        fields = {"project": {"key": self.project}, "issuetype": {"id": self.type["id"]}}
        fields.update({fid: to_api(v, self.fields[fid]) for fid, v in self.values.items()})
        if self.assignee[1]:
            fields["assignee"] = self.assignee[1]
        return fields

    def show(self):
        bad = self.problems()
        order = ["summary", "priority", "duedate", "labels", "components"]
        fids = [f for f in order if f in self.values] + [
            f for f in self.values if f not in order and f != "description"] + [
            f for f in bad if f not in self.values]
        print(f"  {'Space':<11} {self.project}")
        print(f"  {'Type':<11} {self.type['name']}")
        for fid in fids:
            name = self.fields.get(fid, {}).get("name", fid)
            val = shown(self.values[fid]) if fid in self.values else "—"
            print(f"  {name[:11]:<11} {val}" + (f"   ⚠ {bad[fid]}" if fid in bad else ""))
        who, value = self.assignee
        print(f"  {'Assignee':<11} " + (f"{who}" if value else
                                        f"unassigned{f' ({who} not found in Jira)' if who else ''}"))
        if self.related:
            print(f"  {'Links to':<11} " + ", ".join(self.related))
        print("  Description")
        for line in str(self.values.get("description", "")).splitlines():
            print(textwrap.fill(line, 100, initial_indent="    ", subsequent_indent="      ") if line else "")

    def edit(self):
        names = {f.get("name", fid).lower(): fid for fid, f in self.fields.items()}
        print("  Fields you can change: type, assignee, " + ", ".join(
            self.fields[f].get("name", f) for f in self.fields if f not in FIELDS_NOT_FROM_MODEL))
        which = input("  Field to change: ").strip().lower()
        if which == "type":
            print("  Types in " + self.project + ": " + ", ".join(t["name"] for t in self.types))
            t = next((t for t in self.types if t["name"].lower() == input("  New type: ").strip().lower()), None)
            if t:
                keep = {k: self.values[k] for k in ("summary", "description") if k in self.values}
                self.type = t
                print("  Re-drafting for the new type…")
                self.fill()
                self.values.update(keep)
            return
        if which == "assignee":
            self.set_assignee(input("  Assignee (Jira display name, 'me' or empty): "))
            return
        fid = names.get(which) or (which if which in self.fields else None)
        if not fid or fid in FIELDS_NOT_FROM_MODEL:
            print("  (no such field)")
            return
        if fid == "description":
            self.values[fid] = edit_in_editor(str(self.values.get(fid, "")))
            return
        if self.fields[fid].get("allowedValues"):
            print("  Allowed: " + ", ".join(str(a.get("name") or a.get("value"))
                                           for a in self.fields[fid]["allowedValues"][:40]))
        new = input(f"  New {self.fields[fid].get('name', fid)} (empty = remove): ").strip()
        if new:
            self.values[fid] = new
        else:
            self.values.pop(fid, None)


def edit_in_editor(text):
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(text)
    try:
        subprocess.run([*os.environ.get("EDITOR", "nano").split(), f.name])
        return Path(f.name).read_text().strip()
    finally:
        os.unlink(f.name)


def meeting_comment(cand, meeting):
    lines = [f"*Update from the meeting \"{meeting['title']}\" on {meeting['date']}*", "", cand["details"]]
    if cand.get("owner") or cand.get("due"):
        lines += ["", " · ".join(x for x in (cand.get("owner") and f"Owner: {cand['owner']}",
                                             cand.get("due") and f"Due: {cand['due']}") if x)]
    if cand.get("evidence"):
        lines += ["", "{quote}" + cand["evidence"] + "{quote}"]
    lines += ["", f"_Posted by MeetingScribe after review by {meeting['me']}._"]
    return "\n".join(lines)


def ask(prompt, choices):
    while True:
        a = input(prompt).strip().lower()[:1]
        if a in choices:
            return a
        print("  Please answer " + " / ".join(choices))


def jira_note_section(record, client):
    if not record["items"]:
        return []
    md = [JIRA_BEGIN, "## Jira", ""]
    for it in record["items"]:
        verb = {"created": "Created", "commented": "Commented on"}[it["action"]]
        md.append(f"- {verb} [{it['key']}]({client.url(it['key'])}): {it['title']}")
    return md + [JIRA_END, ""]


def update_note_jira(note, record, client):
    if not note.exists():
        return
    text = note.read_text(encoding="utf-8")
    fresh = "\n".join(jira_note_section(record, client))
    section = re.compile(rf"{re.escape(JIRA_BEGIN)}.*?{re.escape(JIRA_END)}\n\n?", re.S)
    if section.search(text):
        text = section.sub(lambda _: fresh + "\n", text)
    elif "\n---\n\n## Transcript" in text:
        text = text.replace("\n---\n\n## Transcript", "\n" + fresh + "\n---\n\n## Transcript", 1)
    else:
        text += "\n" + fresh
    note.write_text(text, encoding="utf-8")


def connect_jira(j):
    """Sign in, re-asking when a stored token or cookie has expired. Returns (client, myself)."""
    if not j["base_url"]:
        j["base_url"] = input("Jira site URL (e.g. https://yourcompany.atlassian.net): ").strip()
        if not j["base_url"]:
            sys.exit("set [jira].base_url in config.toml")
        j["deployment"] = "cloud" if ".atlassian.net" in j["base_url"] else "server"
    require_https(j["base_url"])
    base, retry = j["base_url"].rstrip("/"), False
    while True:
        auth, secret, email, source = jira_credentials(j, ask=retry)
        client = JiraClient(j, auth, secret, email)
        try:
            me = client.myself()
        except JiraError as e:
            if e.status != 401 or retry and source == "prompt" and not ask_retry():
                sys.exit(f"Jira sign-in failed: {e}")
            if source == "keychain":
                keychain("delete", f"{base}#{auth}")
            print(f"Jira didn't accept the stored {'cookie' if auth == 'cookie' else 'token'} "
                  "(expired?). Please paste a fresh one.")
            retry = True
            continue
        print(f"Signed in to {base} as {me.get('displayName')}.")
        if source == "prompt":
            keychain("set", f"{base}#{auth}", secret)
            if email:
                keychain("set", f"{base}#email", email)
            print(f"Saved in your {plat.SECRET_STORE} (\"{KEYCHAIN_SERVICE}\"). "
                  f"Remove it any time with: {plat.CTL} jira --forget")
        return client, me


def ask_retry():
    return ask("Try another token or cookie? [y/n] ", "yn") == "y"


def choose_project(client, suggested):
    try:
        projects = sorted(client.projects(), key=lambda p: p["key"])
    except JiraError:
        projects = []
    keys = {p["key"] for p in projects}
    if projects and len(projects) <= 60:
        print("\nJira spaces you can see:")
        for i in range(0, len(projects), 3):
            print("  " + "".join(f"{p['key']:<10} {p['name'][:26]:<28}" for p in projects[i:i + 3]))
    while True:
        key = input(f"\nWhich space should new tickets go to?{f' [{suggested}]' if suggested else ''} "
                    ).strip().upper() or suggested
        if key and (not keys or key in keys):
            return key
        print(f"  {key or '(empty)'} is not a space you can see.")


def cmd_jira(cfg, args):
    """Turn a meeting's work items into Jira tickets: the local model drafts, you approve."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if not sys.stdin.isatty():
        sys.exit(f"`jira` asks you to approve every ticket, so run it in a terminal: {plat.CTL} jira")
    if not accept_terms(cfg):
        sys.exit("terms of use not accepted")
    j = cfg["jira"]
    if args.forget:
        base = j["base_url"].rstrip("/")
        for k in ("bearer", "basic", "cookie", "email"):
            keychain("delete", f"{base}#{k}")
        print(f"Removed every saved Jira secret for {base or '(no base_url)'} from the {plat.SECRET_STORE}.")
        return
    d = find_session(cfg, args.session)
    meta = json.loads((d / "session.json").read_text())
    note = Path(meta.get("notes") or "")
    started = dt.datetime.fromisoformat(meta["started_at"])
    meeting = {"title": meta["title"], "date": f"{started:%A %Y-%m-%d}", "me": cfg["my_name"],
               "labels": j["labels"]}
    rec_path = d / "jira.json"
    record = json.loads(rec_path.read_text()) if rec_path.exists() else {"items": []}
    print(f"Meeting: {meta['title']} ({started:%a %d %b %Y %H:%M})")

    client, me = connect_jira(j)
    state_path = cfg["work_dir"] / "jira_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    project = (args.project or "").upper() or choose_project(
        client, record.get("project") or state.get("last_project") or j["project"])
    state["last_project"] = project
    state_path.write_text(json.dumps(state))
    types = client.issue_types(project)
    if not types:
        sys.exit(f"you can't create tickets in {project}")

    print("\nLoading the local model…")
    llm = LLM(cfg)
    if record.get("candidates") and not args.refresh:
        cands = record["candidates"]
    else:
        print("Reading the meeting for work that belongs in Jira…")
        turns = json.loads((d / "transcript.json").read_text()) if (d / "transcript.json").exists() else []
        transcript = "\n".join(f"[{ts(t['start'])}] {t['speaker']}: {t['text']}" for t in turns)
        if len(transcript) > cfg["llm_chunk_chars"]:
            transcript = transcript[: cfg["llm_chunk_chars"]] + "\n(transcript cut here; the notes cover the rest)"
        notes = note.read_text(encoding="utf-8").split("\n---\n")[0] if note.exists() else ""
        cands = [c for c in llm_json(llm, JIRA_EXTRACT_INSTRUCTIONS.format(
            date=meeting["date"], calendar=calendar_lines(started.date()), me=cfg["my_name"],
            notes=notes, transcript=transcript), list, 2500)
                 if isinstance(c, dict) and c.get("title")]
        for c in cands:
            c["keywords"] = [str(k) for k in c.get("keywords") or []]
            # The model tends to quote the label too: "Me: I'll do it" -> I'll do it
            c["evidence"] = re.sub(r'^["“]?(?:[\w .]+?:\s+)?(.*?)["”]?$', r"\1", str(c.get("evidence", "")).strip())
            try:
                dt.date.fromisoformat(c.get("due") or "")
            except ValueError:
                c["due"] = ""
        record["candidates"] = cands
        rec_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    if not cands:
        print("Nothing in this meeting looks like it needs a Jira ticket.")
        return
    print(f"Found {len(cands)} possible ticket(s). Searching every Jira space for existing ones…")

    handled = {it["title"] for it in record["items"]}
    for n, cand in enumerate(cands, 1):
        print("\n" + "─" * 100)
        print(f"{n}/{len(cands)}  {cand['title']}")
        if cand["title"] in handled and not args.refresh:
            done = next(it for it in record["items"] if it["title"] == cand["title"])
            print(f"  Already {done['action']} as {done['key']}; skipping. (--refresh to review again)")
            continue
        if cand.get("evidence"):
            print(f"  From the meeting: “{cand['evidence']}”")
        related = related_tickets(client, cand, j)
        found = {i["key"]: i for i in related}
        if related:
            got = llm_json(llm, JIRA_DECIDE_INSTRUCTIONS.format(
                candidate=describe_candidate(cand),
                tickets="\n".join(describe_issue(i) for i in related)), dict, 400)
            labels = {k: str(v).lower() for k, v in (got.get("labels") or {}).items()}
            action, key, links = decide(labels, found, bool(got.get("recurring")),
                                        got.get("worth_a_ticket", True) is not False)
            reason = got.get("reason", "")
            if action == "create" and any(labels.get(k) == "same" for k in found):
                reason = "the matching ticket is Done but the meeting says the problem is back. " + reason
        else:
            action, key, links, labels = "create", "", [], {}
            reason = "No existing ticket in any space matches this work."
        print(f"  Searched all spaces: {len(related)} candidate ticket(s)" +
              "".join(f"\n    · {labels.get(i['key'], '?'):<9} {describe_issue(i)[:130]}" for i in related[:5]))
        print(f"  Model suggests: {action.upper()}{f' ({key})' if key else ''} — {reason}")

        if action == "skip":
            why = f"{key} already covers it" if key else "it looks too small for a ticket"
            if ask(f"  Skip it, since {why}? [y]es skip / [n]o, draft a ticket anyway ", "yn") == "y":
                continue
            action = "create"
        if action == "comment":
            body = meeting_comment(cand, meeting)
            print(f"\n  Comment for {key} ({found[key]['fields'].get('summary', '')}):")
            print(textwrap.indent(body, "    | "))
            a = ask(f"  Add this comment to {key}? [y]es / [n]o / [t]icket: create a new one instead ", "ynt")
            if a == "n":
                continue
            if a == "y":
                if args.dry_run:
                    print(f"  (dry run) would POST comment to {key}")
                    continue
                try:
                    client.comment(key, body)
                except JiraError as e:
                    print(f"  ✗ {e}")
                    continue
                record["items"].append({"title": cand["title"], "action": "commented", "key": key,
                                        "at": dt.datetime.now().isoformat(timespec="seconds")})
                print(f"  ✓ Commented on {key}: {client.url(key)}")
                rec_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
                continue
            action = "create"

        print("  Drafting the ticket…")
        try:
            draft = TicketDraft(client, project, cand, meeting, llm, types, me)
        except (JiraError, ValueError) as e:
            print(f"  ✗ could not draft it: {e}")
            continue
        draft.related = links[:3]
        while True:
            print()
            draft.show()
            a = ask(f"\n  Create this in {project}? [y]es / [n]o / [e]dit a field ", "yne")
            if a == "e":
                draft.edit()
                continue
            if a == "y" and draft.problems():
                print("  Fix the fields marked ⚠ first ([e]dit).")
                continue
            break
        if a == "n":
            continue
        if args.dry_run:
            print("  (dry run) would POST /rest/api/2/issue:\n" +
                  textwrap.indent(json.dumps({"fields": draft.payload()}, indent=2, ensure_ascii=False), "    "))
            continue
        try:
            new = client.create(draft.payload())["key"]
        except JiraError as e:
            print(f"  ✗ Jira refused it: {e}")
            continue
        for other in draft.related:
            try:
                client.link(new, other)
            except JiraError as e:
                print(f"  (could not link {other}: {e})")
        record["items"].append({"title": cand["title"], "action": "created", "key": new,
                                "at": dt.datetime.now().isoformat(timespec="seconds")})
        record["project"] = project
        rec_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        print(f"  ✓ Created {new}: {client.url(new)}")

    if not args.dry_run:
        update_note_jira(note, record, client)
    made = record["items"]
    print("\n" + "─" * 100)
    print(f"Done. {len(made)} Jira update(s) recorded for this meeting" +
          (f"; listed in {note.name}" if made and note.exists() and not args.dry_run else "") + ".")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_run(cfg, args):
    setup_logging(cfg, "daemon")
    plat.background_process()
    if not terms_accepted(cfg):
        log.error("terms of use not accepted; run %s terms (or %s) first", plat.CTL, plat.INSTALL)
        notify(cfg, f"MeetingScribe is not recording: run {plat.CTL} terms to accept the terms of use.")
        time.sleep(3600)  # launchd would otherwise restart us every few seconds
        sys.exit(0)
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)
    lock = open(cfg["work_dir"] / "daemon.lock", "w")
    if not plat.single_instance(lock):
        log.error("another MeetingScribe daemon is already running")
        sys.exit(0)
    Daemon(cfg).run()


def cmd_process(cfg, args):
    setup_logging(cfg, "process")
    plat.background_process()
    plat.lower_priority()
    d = Path(args.session).expanduser().resolve()
    try:
        process_session(cfg, d)
    except Exception:
        log.exception("processing failed for %s", d)
        notify(cfg, f"Meeting processing failed; see {cfg['log_dir'] / 'process.log'}")
        sys.exit(1)


def cmd_status(cfg, args):
    for line in plat.permission_report():
        print(line)
    probe = plat.AudioProbe()
    for p in probe.teams_processes(cfg):
        print(f"  {p['name']:45} pid={p['pid']:<6} mic={p['input']} speaker={p['output']}")
    mic, out = probe.teams_state(cfg)
    print("Teams mic in use:", mic, "| Teams playing audio:", out)
    print("Teams windows:")
    for owner, title in plat.teams_windows():
        print(f"  [{owner}] {title}")
    print("Meeting window detected:", meeting_window_title(cfg))


def cmd_test_capture(cfg, args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    plat.background_process()
    d = cfg["work_dir"] / "test-capture"
    if d.exists():
        for p in d.iterdir():
            p.unlink()
    rec = Recording(cfg, d, "test")
    rec.start()
    print(f"Recording {args.seconds}s; play some audio in Teams and say something…")
    end = time.time() + args.seconds
    while time.time() < end:
        plat.run_loop(0.5)
    rec.stop()
    for w, name in ((rec.mic_w, "mic (Me)"), (rec.sys_w, "system (Others)")):
        print(f"{name:16} {w.samples / TARGET_SR:6.1f}s  peak={w.peak:.4f}"
              + ("   <-- silent!" if w.peak < 1e-4 else ""))
    print("Raw audio in", d)


def find_session(cfg, ref):
    sessions = sorted(p.parent for p in (cfg["work_dir"] / "sessions").glob("*/session.json")
                      if json.loads(p.read_text()).get("status") == "done")
    if not sessions:
        sys.exit("no processed meetings yet")
    if ref in (None, "latest"):
        return sessions[-1]
    hits = [s for s in sessions if ref in s.name]
    if not hits:
        sys.exit(f"no processed meeting matching {ref!r}; have: {[s.name for s in sessions]}")
    return hits[-1]


def cmd_name_speaker(cfg, args):
    """Rename a voice in one meeting's notes and store its voiceprint under that name."""
    d = find_session(cfg, args.session)
    sp_path = d / "speakers.json"
    speakers = json.loads(sp_path.read_text()) if sp_path.exists() else {}
    if args.label not in speakers:
        sys.exit(f"{args.label!r} is not a voice in {d.name}; voices: {list(speakers)}")
    lib = VoiceLibrary(cfg)
    lib.add(args.name, speakers[args.label]["centroid"])
    lib.save()

    entry = speakers.pop(args.label)
    entry.update(known=True)
    if entry.get("sample"):  # the clip only existed to help you recognize the voice
        (d / "voices" / entry.pop("sample")).unlink(missing_ok=True)
    if args.name in speakers:  # two clusters were really the same person
        entry["seconds"] += speakers[args.name]["seconds"]
    speakers[args.name] = entry
    sp_path.write_text(json.dumps(speakers))

    pat = re.compile(rf"\b{re.escape(args.label)}\b(?!\d)")
    tr = d / "transcript.json"
    if tr.exists():
        turns = json.loads(tr.read_text())
        for t in turns:
            if t["speaker"] == args.label:
                t["speaker"] = args.name
        tr.write_text(json.dumps(turns, indent=2, ensure_ascii=False))
    note = Path(json.loads((d / "session.json").read_text())["notes"])
    if note.exists():
        text = pat.sub(args.name, note.read_text(encoding="utf-8"))
        text = text.replace(f"{args.name} (unrecognized)", args.name)
        section = re.compile(rf"{re.escape(VOICES_BEGIN)}.*?{re.escape(VOICES_END)}\n\n?", re.S)
        fresh = "\n".join(voices_section(speakers))
        text = section.sub(lambda _: fresh + "\n" if fresh else "", text)
        note.write_text(text, encoding="utf-8")
    print(f"'{args.label}' is now '{args.name}' in {note.name}; voiceprint saved, so "
          f"{args.name} will be recognized automatically in future meetings.")


def cmd_listen(cfg, args):
    """Play the clip of each unrecognized voice (or one of them) and show what it said."""
    d = find_session(cfg, args.session)
    sp_path = d / "speakers.json"
    speakers = json.loads(sp_path.read_text()) if sp_path.exists() else {}
    todo = [n for n, s in speakers.items() if not s["known"] and s.get("sample")]
    if args.label:
        todo = [n for n in todo if n == args.label]
    if not todo:
        sys.exit(f"no unrecognized voices to play in {d.name}" +
                 (f" named {args.label!r}" if args.label else ""))
    turns = json.loads((d / "transcript.json").read_text())
    print(f"Meeting {d.name}")
    for n in todo:
        lines = [t for t in turns if t["speaker"] == n]
        print(f"\n▶ {n}: {speakers[n]['seconds']} s of speech, {len(lines)} lines")
        for t in lines[:3]:
            text = t["text"] if len(t["text"]) <= 110 else t["text"][:107] + "…"
            print(f"   [{ts(t['start'])}] {text}")
        try:
            plat.play_audio(d / "voices" / speakers[n]["sample"])
        except KeyboardInterrupt:
            print("   (skipped)")
        print(f'   name it:  {plat.CTL} name "{n}" "Full Name"')


def cmd_voices(cfg, args):
    lib = VoiceLibrary(cfg)
    if args.forget:
        if lib.voices.pop(args.forget, None) is None:
            sys.exit(f"no voiceprint named {args.forget!r}")
        lib.save()
        print("forgot", args.forget)
        return
    if not lib.voices:
        print(f"No voiceprints yet. Name a voice with: {plat.CTL} name \"Speaker 1\" \"Full Name\"")
    for name, v in sorted(lib.voices.items()):
        print(f"  {name:30} learned from {v['n']} meeting(s)")


def cmd_refilter(cfg, args):
    """Apply the work filter to meetings processed before it existed: drops their
    non-work lines from transcript.json and rewrites the note and its summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    todo = []
    for p in sorted((cfg["work_dir"] / "sessions").glob("*/session.json")):
        meta = json.loads(p.read_text())
        if (meta.get("status") == "done" and (args.session or "") in p.parent.name
                and (args.force or not meta.get("work_filtered"))
                and (p.parent / "transcript.json").exists()):
            todo.append((p.parent, meta))
    if not todo:
        sys.exit("no processed meetings left to filter" + (f" matching {args.session!r}" if args.session else ""))
    flt = LLM(cfg, cfg["work_filter_model"])
    llm = flt if filter_is_summary_model(cfg) else LLM(cfg)
    for d, meta in todo:
        turns = json.loads((d / "transcript.json").read_text())
        sp_path = d / "speakers.json"
        speakers = json.loads(sp_path.read_text()) if sp_path.exists() else {}
        turns, dropped = work_only(cfg, flt, turns) if turns else ([], 0)
        for name, info in speakers.items():  # the "They said" quote must be a kept line
            mine = [t for t in turns if t["speaker"] == name]
            if mine:
                text = mine[0]["text"] if len(mine[0]["text"]) <= 90 else mine[0]["text"][:87].rstrip() + "…"
                info["first"] = [mine[0]["start"], text.replace("|", "/")]
            else:
                info.pop("first", None)
        # A note renamed by hand keeps its name: its title becomes the meeting's title.
        prefix = f"{dt.datetime.fromisoformat(meta['started_at']):%Y-%m-%d %H%M} - "
        old = Path(meta.get("notes") or cfg["output_dir"] / "missing")
        if not old.exists():
            found = sorted(cfg["output_dir"].glob(prefix + "*.md"))
            if len(found) == 1:
                old = found[0]
                meta["title"] = old.stem[len(prefix):]
        (d / "transcript.json").write_text(json.dumps(turns, indent=2, ensure_ascii=False))
        sp_path.write_text(json.dumps(speakers))
        out = write_note(cfg, meta, turns, speakers, llm, dropped)
        if old.exists() and old != out:
            old.unlink()
        meta.update(notes=str(out), work_filtered=True)
        (d / "session.json").write_text(json.dumps(meta, indent=2))
        print(f"{d.name}: dropped {dropped} non-work line(s) -> {out.name}")


def cmd_terms(cfg, args):
    sys.exit(0 if accept_terms(cfg) else "terms of use not accepted")


def cmd_download(cfg, args):
    from huggingface_hub import snapshot_download
    print("Downloading", cfg["whisper_model"])
    snapshot_download(cfg["whisper_model"])
    print("Downloading", cfg["voice_model"])
    snapshot_download(cfg["voice_model"])
    if cfg["llm_backend"] == "llamacpp":
        print("Downloading", cfg["gguf_model"])
        gguf_path(cfg, download=True)
    elif cfg["llm_backend"] == "mlx":
        print("Downloading", cfg["mlx_llm_model"])
        snapshot_download(cfg["mlx_llm_model"])
        if cfg["work_only"] and not filter_is_summary_model(cfg):
            print("Downloading", cfg["work_filter_model"])
            snapshot_download(cfg["work_filter_model"])
    else:
        print("Ollama backend: run `ollama pull", cfg["ollama_model"] + "`")
    print("Done. All later runs are offline (HF_HUB_OFFLINE=1).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    p = sub.add_parser("process")
    p.add_argument("session")
    p.set_defaults(fn=cmd_process)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("test-capture")
    p.add_argument("seconds", type=int, nargs="?", default=15)
    p.set_defaults(fn=cmd_test_capture)
    sub.add_parser("download-models").set_defaults(fn=cmd_download)
    p = sub.add_parser("name-speaker", help='e.g. name-speaker "Speaker 2" "Priya Shah"')
    p.add_argument("label")
    p.add_argument("name")
    p.add_argument("--session", default="latest", help="session id (or part of it); default latest")
    p.set_defaults(fn=cmd_name_speaker)
    p = sub.add_parser("listen", help="play clips of unrecognized voices in a meeting")
    p.add_argument("label", nargs="?")
    p.add_argument("--session", default="latest")
    p.set_defaults(fn=cmd_listen)
    p = sub.add_parser("voices", help="list stored voiceprints")
    p.add_argument("--forget", metavar="NAME")
    p.set_defaults(fn=cmd_voices)
    p = sub.add_parser("refilter", help="drop non-work lines from already processed meetings")
    p.add_argument("--session", help="session id (or part of it); default every unfiltered meeting")
    p.add_argument("--force", action="store_true", help="also re-filter meetings already filtered")
    p.set_defaults(fn=cmd_refilter)
    p = sub.add_parser("jira", help="draft Jira tickets from a meeting; you approve each one")
    p.add_argument("--session", default="latest", help="session id (or part of it); default latest")
    p.add_argument("--project", help="Jira space (project) key; asked if not given")
    p.add_argument("--refresh", action="store_true", help="re-read the meeting and review every item again")
    p.add_argument("--dry-run", action="store_true", help="search and draft, but write nothing to Jira")
    p.add_argument("--forget", action="store_true", help=f"delete the saved Jira token / cookie from the {plat.SECRET_STORE}")
    p.set_defaults(fn=cmd_jira)
    sub.add_parser("terms", help="show and accept the terms of use").set_defaults(fn=cmd_terms)
    args = ap.parse_args()
    if sys.stderr is None:  # pythonw.exe (Windows background task) has no console streams
        sys.stdout = sys.stderr = open(os.devnull, "w")
    try:
        args.fn(load_config(), args)
    except NotImplementedError as e:  # a platform piece that is not built yet
        sys.exit(str(e))


if __name__ == "__main__":
    main()
