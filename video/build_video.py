#!/usr/bin/env python3
"""
Builds MeetingScribe_Demo.mp4 entirely on this Mac.

  1. Narration: every line in script.json is spoken by Kokoro-82M, an open neural voice
     running offline (models/), and timed. `"engine": "say"` falls back to macOS `say`.
  2. timeline.js: scene and caption timings derived from the narration, read by motion.html.
  3. Frames: an off-screen WebKit view (Safari's engine, via pyobjc) renders motion.html frame
     by frame (renderAt(t)), so every frame is exact regardless of machine speed. Nothing is
     fetched from the network: the page is self-contained.
  4. Encoding: the ffmpeg binary bundled with imageio-ffmpeg muxes frames and narration.

  video/.venv/bin/python video/build_video.py                 # full build
  video/.venv/bin/python video/build_video.py --stills 5,40   # PNG previews at those seconds
"""
import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).resolve().parent
SR = 48000


class Voice:
    """Kokoro-82M (Apache 2.0), an open neural voice running offline via ONNX; or macOS `say`."""

    def __init__(self, script):
        self.script = script
        if script["engine"] == "kokoro":
            from kokoro_onnx import Kokoro
            self.kokoro = Kokoro(str(HERE / "models" / "kokoro-v1.0.onnx"), str(HERE / "models" / "voices-v1.0.bin"))

    def speak(self, text, out):
        """Writes a 48 kHz mono WAV of `text` to `out`; returns (pcm bytes, seconds)."""
        raw = out.with_suffix(".raw.wav")
        if self.script["engine"] == "kokoro":
            import numpy as np
            samples, sr = self.kokoro.create(text, voice=self.script["voice"], speed=self.script.get("speed", 1.0), lang="en-us")
            samples = samples / max(1e-6, float(np.abs(samples).max())) * 0.89  # same level on every line
            with wave.open(str(raw), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes((samples * 32767).astype(np.int16).tobytes())
        else:
            raw = raw.with_suffix(".aiff")
            subprocess.run(["/usr/bin/say", "-v", self.script["voice"], "-r", str(self.script["rate"]), "-o", str(raw), text], check=True)
        subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", f"LEI16@{SR}", "-c", "1", str(raw), str(out)], check=True)
        raw.unlink()
        with wave.open(str(out)) as w:
            return w.readframes(w.getnframes()), w.getnframes() / SR


def narrate(script, work):
    voice = Voice(script)
    scenes, clips, t = [], [], 0.0
    for sc in script["scenes"]:
        lines, cur = [], script["lead_in"]
        for i, text in enumerate(sc["lines"]):
            pcm, dur = voice.speak(text, work / f"{sc['id']}_{i}.wav")
            lines.append({"t": round(cur, 3), "dur": round(dur, 3), "text": text})
            clips.append((t + cur, pcm))
            cur += dur + script["gap"]
        dur = max(sc["min"], cur - script["gap"] + script["tail"])
        scenes.append({"id": sc["id"], "start": round(t, 3), "dur": round(dur, 3), "lines": lines})
        t += dur
        print(f"  {sc['id']:<10} {dur:5.1f}s")
    return scenes, clips, t


def write_audio(clips, total, path):
    buf = bytearray(int(total * SR) * 2)
    for start, pcm in clips:
        i = int(start * SR) * 2
        buf[i:i + len(pcm)] = pcm[: len(buf) - i]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(buf))


class Renderer:
    """An off-screen WKWebView (Apple's WebKit, the engine behind Safari), driven frame by frame."""

    def __init__(self, width, height):
        import AppKit
        import Quartz  # noqa: F401  (bridges CGImage for the snapshot)
        import WebKit
        self.AppKit, self.WebKit, self.width = AppKit, WebKit, width
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(2)  # no Dock icon
        rect = AppKit.NSMakeRect(0, 0, width, height)
        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        cfg.preferences().setValue_forKey_(True, "allowFileAccessFromFileURLs")
        self.view = WebKit.WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        self.win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        self.win.setContentView_(self.view)
        self.win.setFrameOrigin_((-4 * width, -4 * height))  # off every screen
        self.win.orderBack_(None)
        self.scale = self.win.backingScaleFactor() or 1.0

    def wait(self, box, timeout=30):
        end = time.time() + timeout
        loop, NSDate = self.AppKit.NSRunLoop.currentRunLoop(), self.AppKit.NSDate
        while "r" not in box:
            if time.time() > end:
                raise RuntimeError("WebKit did not answer")
            loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.002))
        return box["r"]

    def eval(self, js):
        box = {}
        self.view.evaluateJavaScript_completionHandler_(js, lambda r, e: box.update(r=(r, e)))
        result, error = self.wait(box)
        if error:
            raise RuntimeError(f"{js}: {error}")
        return result

    def open(self, path, query):
        self.path, self.query = path, query
        url = self.AppKit.NSURL.URLWithString_(path.as_uri() + query)
        self.view.loadFileURL_allowingReadAccessToURL_(url, self.AppKit.NSURL.fileURLWithPath_(str(path.parent)))
        for _ in range(400):
            box = {}
            self.view.evaluateJavaScript_completionHandler_(
                "document.readyState + ':' + typeof window.renderAt", lambda r, e: box.update(r=(r, e)))
            if self.wait(box)[0] == "complete:function":
                return
            time.sleep(0.05)
        raise RuntimeError("motion.html did not load")

    def shot(self, t, fmt="jpeg"):
        import objc
        # Each frame in its own autorelease pool, or snapshots pile up until WebKit gives up.
        with objc.autorelease_pool():
            for attempt in range(4):
                try:
                    return self._shot(t, fmt)
                except RuntimeError as e:
                    if attempt == 3:
                        raise
                    print(f"  (frame at {t:.2f}s failed: {str(e)[-80:]}; reloading the page)", flush=True)
                    self.open(self.path, self.query)

    def _shot(self, t, fmt):
        result = self.eval(f"(() => {{ try {{ renderAt({t}); return 'ok'; }} catch (e) {{ return String(e); }} }})()")
        if result != "ok":
            raise RuntimeError(f"renderAt({t}) threw: {result}")
        conf = self.WebKit.WKSnapshotConfiguration.alloc().init()
        conf.setAfterScreenUpdates_(True)
        conf.setSnapshotWidth_(self.width / self.scale)  # points; pixels = points x backing scale
        box = {}
        self.view.takeSnapshotWithConfiguration_completionHandler_(conf, lambda img, e: box.update(r=(img, e)))
        img, error = self.wait(box)
        if error or img is None:
            raise RuntimeError(f"snapshot failed: {error}")
        rep = self.AppKit.NSBitmapImageRep.alloc().initWithCGImage_(img.CGImageForProposedRect_context_hints_(None, None, None)[0])
        kind = self.AppKit.NSBitmapImageFileTypeJPEG if fmt == "jpeg" else self.AppKit.NSBitmapImageFileTypePNG
        props = {self.AppKit.NSImageCompressionFactor: 0.93} if fmt == "jpeg" else {}
        return bytes(rep.representationUsingType_properties_(kind, props))

    def close(self):
        self.win.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stills", help="comma-separated seconds; writes PNGs to video/stills instead of the MP4")
    ap.add_argument("--keep-narration", action="store_true", help="reuse timeline.js and narration.wav")
    ap.add_argument("--out", default=str(HERE.parent / "MeetingScribe_Demo.mp4"))
    args = ap.parse_args()
    script = json.loads((HERE / "script.json").read_text())
    W, H, fps = script["width"], script["height"], script["fps"]
    audio = HERE / "narration.wav"

    if not (args.keep_narration and audio.exists() and (HERE / "timeline.js").exists()):
        print("Narration:")
        with tempfile.TemporaryDirectory() as tmp:
            scenes, clips, total = narrate(script, Path(tmp))
        write_audio(clips, total, audio)
        (HERE / "timeline.js").write_text(
            "window.TIMELINE = " + json.dumps({"scenes": scenes, "total": round(total, 3)}, indent=1) + ";\n")
        print(f"  total      {total:5.1f}s")
    total = json.loads((HERE / "timeline.js").read_text().split("=", 1)[1].rstrip().rstrip(";"))["total"]

    chrome = Renderer(W, H)
    try:
        chrome.open(HERE / "motion.html", "?capture")
        if args.stills:
            out = HERE / "stills"
            out.mkdir(exist_ok=True)
            for s in args.stills.split(","):
                (out / f"t{float(s):06.1f}.png").write_bytes(chrome.shot(float(s), "png"))
            print("stills in", out)
            return
        n = int(total * fps)
        ff = subprocess.Popen([
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
            "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
            "-i", str(audio), "-vf", f"scale={W}:{H}:flags=lanczos:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", args.out],
            stdin=subprocess.PIPE)
        t0 = time.time()
        for i in range(n):
            ff.stdin.write(chrome.shot(i / fps))
            if i % (fps * 10) == 0:
                el = time.time() - t0
                print(f"  frame {i}/{n}  ({el:.0f}s elapsed, ~{el / max(i, 1) * (n - i):.0f}s left)", flush=True)
        ff.stdin.close()
        if ff.wait():
            sys.exit("ffmpeg failed")
        print("wrote", args.out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
