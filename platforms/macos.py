"""macOS side of MeetingScribe: Core Audio call detection, Teams audio capture
(process tap or ScreenCaptureKit), window titles, notifications and the run loop.

Every module in this package exposes the same names; meeting_scribe.py only
talks to the one platforms.load() picks.
"""

import ctypes
import fcntl
import json
import logging
import os
import struct
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger("meetingscribe")
TARGET_SR = 16000  # same as meeting_scribe.TARGET_SR

# How messages name things on this system.
CTL, INSTALL, DEVICE, SECRET_STORE = "./ctl.sh", "./install.sh", "Mac", "macOS Keychain"

DIRS = {
    "work_dir": "~/Library/Application Support/MeetingScribe",
    "log_dir": "~/Library/Logs/MeetingScribe",
}

MODEL_DEFAULTS = {
    "asr_backend": "mlx",
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "llm_backend": "mlx",
}


def notify(text):
    script = f'display notification {json.dumps(text)} with title "MeetingScribe"'
    subprocess.Popen(["/usr/bin/osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def background_process():
    """Homebrew's Python runs as Python.app, which macOS gives a Dock icon as soon as
    it touches window/audio services. Declare ourselves a background-only process."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(2)  # NSApplicationActivationPolicyProhibited
    except Exception as e:
        log.warning("could not hide Dock icon: %s", e)


def lower_priority():
    os.nice(10)


def single_instance(lock_file):
    """Holds an exclusive lock on the open file; False if another process has it."""
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def run_loop(seconds):
    """Waits while letting Core Audio / ScreenCaptureKit callbacks run."""
    from Foundation import NSDate, NSRunLoop
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def play_audio(path):
    subprocess.run(["/usr/bin/afplay", str(path)])


def request_permissions(cfg):
    import Quartz
    if not Quartz.CGPreflightScreenCaptureAccess():
        log.info("Screen Recording not granted (optional): notes will be titled 'Teams meeting' "
                 "instead of the meeting's window title")
        asked = cfg["work_dir"] / ".asked_screen_recording"
        if not asked.exists():  # prompt once, not on every restart
            asked.touch()
            Quartz.CGRequestScreenCaptureAccess()


def permission_report():
    import Quartz
    return [f"Screen Recording permission (this process): {Quartz.CGPreflightScreenCaptureAccess()}"]


# --------------------------------------------------------------------------
# Meeting detection: Core Audio process objects (macOS 14.4+) via ctypes
# --------------------------------------------------------------------------

def _fourcc(s):
    return struct.unpack(">I", s.encode())[0]


class _PropAddr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


class CoreAudioProbe:
    """Reports which processes are using audio input/output, without opening any
    device ourselves. Our own mic capture never confuses it, because we only look
    at processes whose bundle ID belongs to Teams."""

    SYSTEM_OBJECT = 1
    GLOBAL = _fourcc("glob")
    PROCESS_LIST = _fourcc("prs#")
    BUNDLE_ID = _fourcc("pbid")
    PID = _fourcc("ppid")
    RUNNING_INPUT = _fourcc("piri")
    RUNNING_OUTPUT = _fourcc("piro")
    DEFAULT_INPUT = _fourcc("dIn ")

    def __init__(self):
        self.ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        u32p = ctypes.POINTER(ctypes.c_uint32)
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32, ctypes.c_void_p, u32p]
        self.ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_PropAddr), ctypes.c_uint32, ctypes.c_void_p,
            u32p, ctypes.c_void_p]
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                               ctypes.c_uint32]
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _u32(self, obj, sel):
        v, size = ctypes.c_uint32(0), ctypes.c_uint32(4)
        addr = _PropAddr(sel, self.GLOBAL, 0)
        st = self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                                ctypes.byref(size), ctypes.byref(v))
        return v.value if st == 0 else None

    def _bundle_id(self, obj):
        ref, size = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        addr = _PropAddr(self.BUNDLE_ID, self.GLOBAL, 0)
        if self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                              ctypes.byref(size), ctypes.byref(ref)) or not ref.value:
            return ""
        buf = ctypes.create_string_buffer(512)
        self.cf.CFStringGetCString(ref, buf, 512, 0x08000100)  # kCFStringEncodingUTF8
        self.cf.CFRelease(ref)
        return buf.value.decode("utf-8", "replace")

    def processes(self):
        addr = _PropAddr(self.PROCESS_LIST, self.GLOBAL, 0)
        size = ctypes.c_uint32(0)
        if self.ca.AudioObjectGetPropertyDataSize(self.SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                                  ctypes.byref(size)):
            return []
        ids = (ctypes.c_uint32 * (size.value // 4))()
        if self.ca.AudioObjectGetPropertyData(self.SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                              ctypes.byref(size), ids):
            return []
        return [{"id": o, "bundle": self._bundle_id(o), "pid": self._u32(o, self.PID),
                 "input": bool(self._u32(o, self.RUNNING_INPUT)),
                 "output": bool(self._u32(o, self.RUNNING_OUTPUT))} for o in ids]

    def app_state(self, prefix):
        procs = [p for p in self.processes() if p["bundle"].startswith(prefix)]
        return any(p["input"] for p in procs), any(p["output"] for p in procs)

    def default_input_device(self):
        return self._u32(self.SYSTEM_OBJECT, self.DEFAULT_INPUT)

    def teams_state(self, cfg):
        """(Teams holds the mic, Teams plays audio)."""
        return self.app_state(cfg["teams_bundle_prefix"])

    def teams_processes(self, cfg):
        return [{**p, "name": p["bundle"]} for p in self.processes()
                if p["bundle"].startswith(cfg["teams_bundle_prefix"])]


def teams_windows():
    """(owner, title) for every titled Teams window. Titles need Screen Recording."""
    import Quartz
    opts = Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        owner = str(w.get(Quartz.kCGWindowOwnerName, "") or "")
        title = str(w.get(Quartz.kCGWindowName, "") or "").strip()
        if ("Microsoft Teams" in owner or owner == "MSTeams") and title \
                and w.get(Quartz.kCGWindowLayer, 0) == 0:
            out.append((owner, title))
    return out


# --------------------------------------------------------------------------
# Teams audio capture
# --------------------------------------------------------------------------

def _wait_for(fn, timeout=10):
    """Run an async ScreenCaptureKit call and block until its completion handler fires."""
    done, box = threading.Event(), {}

    def handler(*args):
        box["args"] = args
        done.set()

    fn(handler)
    if not done.wait(timeout):
        raise TimeoutError("ScreenCaptureKit call timed out")
    return box["args"]


def _sck_classes():
    """Built lazily so pyobjc/ScreenCaptureKit are only loaded by commands that need them."""
    global _AudioOutput, _StreamDelegate
    if "_AudioOutput" in globals():
        return _AudioOutput, _StreamDelegate
    import objc
    import CoreMedia as CM
    import ScreenCaptureKit as SCK
    from Foundation import NSObject

    class _AudioOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
            if kind != SCK.SCStreamOutputTypeAudio or not CM.CMSampleBufferIsValid(sbuf):
                return
            try:
                frames = CM.CMSampleBufferGetNumSamples(sbuf)
                block = CM.CMSampleBufferGetDataBuffer(sbuf)
                if not frames or block is None:
                    return
                n = CM.CMBlockBufferGetDataLength(block)
                st, raw = CM.CMBlockBufferCopyDataBytes(block, 0, n, None)
                if st != 0:
                    return
                x = np.frombuffer(bytes(raw), dtype=np.float32)
                ch = max(1, len(x) // frames)
                if ch > 1:  # ScreenCaptureKit delivers non-interleaved (planar) float32
                    x = x[: ch * frames].reshape(ch, frames).mean(axis=0)
                dur = CM.CMTimeGetSeconds(CM.CMSampleBufferGetDuration(sbuf))
                sr = int(round(frames / dur)) if dur and dur > 0 else self.sr
                self.writer.write(x, sr, time.monotonic())
            except Exception as e:
                log.warning("system audio buffer dropped: %s", e)

    class _StreamDelegate(NSObject, protocols=[objc.protocolNamed("SCStreamDelegate")]):
        def stream_didStopWithError_(self, stream, error):
            log.warning("ScreenCaptureKit stream stopped: %s", error)
            self.owner.stopped = True

    return _AudioOutput, _StreamDelegate


class SystemAudioCapture:
    """Teams' audio via ScreenCaptureKit. Audio only; video is configured as a
    2x2 px, 1 fps stream that we never consume."""

    def __init__(self, writer, mode, prefix):
        self.writer, self.mode, self.prefix = writer, mode, prefix
        self.stream = None
        self.stopped = False

    def start(self):
        import CoreMedia as CM
        import ScreenCaptureKit as SCK
        AudioOutput, StreamDelegate = _sck_classes()

        content, err = _wait_for(lambda h: SCK.SCShareableContent
            .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(False, False, h))
        if err is not None or content is None:
            raise PermissionError(f"Screen Recording permission missing or denied ({err})")
        display = content.displays()[0]
        apps = [a for a in content.applications()
                if (a.bundleIdentifier() or "").startswith(self.prefix)]
        if self.mode == "teams" and apps:
            filt = SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                display, apps, [])
            log.info("system audio: capturing %d Teams processes: %s", len(apps),
                     sorted({a.bundleIdentifier() for a in apps}))
        else:
            if self.mode == "teams":
                log.warning("no Teams apps visible to ScreenCaptureKit; capturing all system audio")
            filt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
            log.info("system audio: capturing all system audio")

        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setCapturesAudio_(True)
        cfg.setExcludesCurrentProcessAudio_(True)
        cfg.setSampleRate_(TARGET_SR)
        cfg.setChannelCount_(1)
        cfg.setWidth_(2)
        cfg.setHeight_(2)
        cfg.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))

        self.output = AudioOutput.alloc().init()
        self.output.writer, self.output.sr = self.writer, TARGET_SR
        self.delegate = StreamDelegate.alloc().init()
        self.delegate.owner = self
        try:
            import libdispatch
            self.queue = libdispatch.dispatch_queue_create(b"local.meetingscribe.audio", None)
        except ImportError:
            self.queue = None
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, cfg, self.delegate)
        ok, err = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self.output, SCK.SCStreamOutputTypeAudio, self.queue, None)
        if not ok:
            raise RuntimeError(f"addStreamOutput failed: {err}")
        (err,) = _wait_for(lambda h: self.stream.startCaptureWithCompletionHandler_(h))
        if err is not None:
            raise RuntimeError(f"startCapture failed: {err}")
        self.stopped = False

    def stop(self):
        if self.stream is not None:
            try:
                _wait_for(lambda h: self.stream.stopCaptureWithCompletionHandler_(h), 5)
            except Exception as e:
                log.warning("system audio stop: %s", e)
            self.stream = None


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32), ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class _AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32), ("mBuffers", _AudioBuffer * 8)]


class _ASBD(ctypes.Structure):
    _fields_ = [("mSampleRate", ctypes.c_double), ("mFormatID", ctypes.c_uint32),
                ("mFormatFlags", ctypes.c_uint32), ("mBytesPerPacket", ctypes.c_uint32),
                ("mFramesPerPacket", ctypes.c_uint32), ("mBytesPerFrame", ctypes.c_uint32),
                ("mChannelsPerFrame", ctypes.c_uint32), ("mBitsPerChannel", ctypes.c_uint32),
                ("mReserved", ctypes.c_uint32)]


_IOPROC = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.POINTER(_AudioBufferList), ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p)


class ProcessTapCapture:
    """Teams' audio via a Core Audio process tap (macOS 14.4+). It is audio-only, so
    macOS shows no screen-capture indicator; it needs the "System Audio Recording
    Only" permission. A private aggregate device wraps the tap, and an IOProc
    hands each buffer to a writer thread so the real-time audio thread never waits."""

    TAP_FORMAT = _fourcc("tfmt")

    def __init__(self, writer, mode, prefix):
        self.writer, self.mode, self.prefix = writer, mode, prefix
        self.stopped = False
        self.tap_id = self.agg_id = None
        self.proc_id = ctypes.c_void_p()
        self.running = False
        self.probe = CoreAudioProbe()
        ca = self.ca = self.probe.ca
        u32p = ctypes.POINTER(ctypes.c_uint32)
        ca.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p, u32p]
        ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
        ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, u32p]
        ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
        ca.AudioDeviceCreateIOProcID.argtypes = [ctypes.c_uint32, _IOPROC, ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_void_p)]
        ca.AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        ca.AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        ca.AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

    def start(self):
        import objc
        from Foundation import NSBundle, NSNumber
        NSBundle.bundleWithPath_("/System/Library/Frameworks/CoreAudio.framework").load()
        CATapDescription = objc.lookUpClass("CATapDescription")

        procs = [p for p in self.probe.processes() if p["bundle"].startswith(self.prefix)]
        if self.mode == "teams" and procs:
            ids = [NSNumber.numberWithUnsignedInt_(p["id"]) for p in procs]
            desc = CATapDescription.alloc().initMonoMixdownOfProcesses_(ids)
            log.info("system audio: tapping %d Teams processes: %s", len(procs),
                     sorted({p["bundle"] for p in procs}))
        else:
            if self.mode == "teams":
                log.warning("no Teams audio processes found; tapping all system audio")
            desc = CATapDescription.alloc().initMonoGlobalTapButExcludeProcesses_([])
            log.info("system audio: tapping all system audio")
        desc.setPrivate_(True)
        desc.setMuteBehavior_(0)  # CATapUnmuted: you still hear the meeting normally
        desc.setName_("MeetingScribe")
        self.desc = desc

        tap = ctypes.c_uint32(0)
        st = self.ca.AudioHardwareCreateProcessTap(objc.pyobjc_id(desc), ctypes.byref(tap))
        if st:
            raise RuntimeError(f"AudioHardwareCreateProcessTap failed ({st})")
        self.tap_id = tap.value

        fmt, size = _ASBD(), ctypes.c_uint32(ctypes.sizeof(_ASBD))
        addr = _PropAddr(self.TAP_FORMAT, CoreAudioProbe.GLOBAL, 0)
        self.ca.AudioObjectGetPropertyData(self.tap_id, ctypes.byref(addr), 0, None,
                                           ctypes.byref(size), ctypes.byref(fmt))
        self.sr = int(fmt.mSampleRate) or 48000
        self.channels = fmt.mChannelsPerFrame or 1
        self.interleaved = not (fmt.mFormatFlags & (1 << 5))  # kAudioFormatFlagIsNonInterleaved

        tap_uuid = desc.UUID().UUIDString()
        self.agg_dict = {"name": "MeetingScribe Tap", "uid": f"local.meetingscribe.{tap_uuid}",
                         "private": True, "stacked": False, "tapautostart": True,
                         "taps": [{"uid": tap_uuid, "drift": True}]}
        from Foundation import NSDictionary
        self.agg_ns = NSDictionary.dictionaryWithDictionary_(self.agg_dict)
        agg = ctypes.c_uint32(0)
        st = self.ca.AudioHardwareCreateAggregateDevice(objc.pyobjc_id(self.agg_ns), ctypes.byref(agg))
        if st:
            self.stop()
            raise RuntimeError(f"AudioHardwareCreateAggregateDevice failed ({st})")
        self.agg_id = agg.value

        import collections
        self.pending = collections.deque()
        self.wake = threading.Event()

        def ioproc(dev, now, inp, in_time, out, out_time, client):
            try:
                abl = inp.contents
                chunks = [ctypes.string_at(abl.mBuffers[i].mData, abl.mBuffers[i].mDataByteSize)
                          for i in range(min(abl.mNumberBuffers, 8)) if abl.mBuffers[i].mData]
                self.pending.append((chunks, time.monotonic()))
                self.wake.set()
            except Exception:
                pass
            return 0

        self.ioproc = _IOPROC(ioproc)  # keep a reference: ctypes callbacks must outlive the device
        st = self.ca.AudioDeviceCreateIOProcID(self.agg_id, self.ioproc, None, ctypes.byref(self.proc_id))
        if st:
            self.stop()
            raise RuntimeError(f"AudioDeviceCreateIOProcID failed ({st})")
        self.running = True
        self.worker = threading.Thread(target=self._drain, daemon=True)
        self.worker.start()
        st = self.ca.AudioDeviceStart(self.agg_id, self.proc_id)
        if st:
            self.stop()
            raise RuntimeError(f"AudioDeviceStart failed ({st})")
        self.stopped = False
        log.info("process tap running @ %d Hz, %d ch", self.sr, self.channels)

    def _drain(self):
        while self.running or self.pending:
            self.wake.wait(0.5)
            self.wake.clear()
            while self.pending:
                chunks, arrived = self.pending.popleft()
                bufs = [np.frombuffer(c, dtype=np.float32) for c in chunks]
                if not bufs:
                    continue
                if len(bufs) > 1:  # one buffer per channel
                    n = min(len(b) for b in bufs)
                    x = np.mean([b[:n] for b in bufs], axis=0)
                elif self.channels > 1 and self.interleaved:
                    x = bufs[0][: len(bufs[0]) // self.channels * self.channels] \
                        .reshape(-1, self.channels).mean(axis=1)
                else:
                    x = bufs[0]
                self.writer.write(x, self.sr, arrived)

    def stop(self):
        if self.agg_id is not None and self.proc_id.value:
            self.ca.AudioDeviceStop(self.agg_id, self.proc_id)
            self.ca.AudioDeviceDestroyIOProcID(self.agg_id, self.proc_id)
            self.proc_id = ctypes.c_void_p()
        self.running = False
        if getattr(self, "worker", None):
            self.wake.set()
            self.worker.join(3)
        if self.agg_id is not None:
            self.ca.AudioHardwareDestroyAggregateDevice(self.agg_id)
            self.agg_id = None
        if self.tap_id is not None:
            self.ca.AudioHardwareDestroyProcessTap(self.tap_id)
            self.tap_id = None


def system_audio_capture(cfg, writer):
    cls = (SystemAudioCapture if cfg["system_audio_backend"] == "screencapturekit"
           else ProcessTapCapture)
    return cls(writer, cfg["capture_mode"], cfg["teams_bundle_prefix"])


AudioProbe = CoreAudioProbe
