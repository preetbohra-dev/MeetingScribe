"""Windows side of MeetingScribe (Windows 10 build 20348+ / Windows 11). Meeting apps
(Teams, Zoom, browsers) are described by MEETING_APPS in meeting_scribe.py.

  Call detection   The privacy consent store in the registry records when each app
                   opens the microphone; LastUsedTimeStop is 0 while the app holds it.
                   Whether the app is playing call audio comes from the WASAPI audio
                   sessions of its process tree.
  App audio        WASAPI process loopback on the app's process tree: audio only, from
                   that app only, no permission prompt. "all" mode uses the same
                   loopback on every process except this one.
  Window titles    EnumWindows on the app's processes; no permission needed.

Only ctypes, winreg and comtypes (pure Python): Smart App Control, on by default on
new Windows 11 installs, blocks unsigned native DLLs such as scipy's and torch's.
"""

import sys

sys.coinit_flags = 0  # COINIT_MULTITHREADED, read by comtypes when it initialises COM

import base64  # noqa: E402
import ctypes  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import msvcrt  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import winreg  # noqa: E402
from ctypes import POINTER, Structure, byref, c_int, c_longlong, c_uint32, c_uint64, c_ushort, c_void_p  # noqa: E402
from ctypes.wintypes import BOOL, DWORD, HANDLE, HWND, LPARAM, LPCWSTR, LPWSTR  # noqa: E402

import numpy as np  # noqa: E402

log = logging.getLogger("meetingscribe")

# cuBLAS / cuDNN from the nvidia-* wheels the installer adds on NVIDIA PCs, for GPU Whisper.
_site_packages = os.path.dirname(os.path.dirname(os.path.abspath(np.__file__)))
for _dll_dir in glob.glob(os.path.join(_site_packages, "nvidia", "*", "bin")):
    os.add_dll_directory(_dll_dir)
    os.environ["PATH"] = _dll_dir + os.pathsep + os.environ["PATH"]

_LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
# How messages name things on this system.
CTL, INSTALL, DEVICE, SECRET_STORE = r".\ctl", r".\install", "PC", "Windows Credential Manager"

DIRS = {
    "work_dir": os.path.join(_LOCAL, "MeetingScribe"),
    "log_dir": os.path.join(_LOCAL, "MeetingScribe", "Logs"),
}


def _ram_gb():
    class MEMORYSTATUSEX(Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(byref(m))
    return m.ullTotalPhys / 2**30


# Qwen 2.5 14B (about 9 GB) needs room next to the meeting app and the browser; smaller PCs get 7B.
_QWEN = "bartowski/Qwen2.5-{0}-Instruct-GGUF/Qwen2.5-{0}-Instruct-Q4_K_M.gguf"
MODEL_DEFAULTS = {
    "asr_backend": "faster-whisper",
    "whisper_model": "dropbox-dash/faster-whisper-large-v3-turbo",
    "llm_backend": "llamacpp",
    "gguf_model": _QWEN.format("14B" if _ram_gb() >= 24 else "7B"),
    # Smart App Control blocks torch, so voices come from an ONNX model instead of SpeechBrain.
    "voice_model": "Wespeaker/wespeaker-voxceleb-resnet34-LM",
}

CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def notify(text):
    """A toast from PowerShell's built-in WinRT bridge; the text travels in the environment,
    so nothing in it is ever parsed as script."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
        "$t=$x.GetElementsByTagName('text');"
        "$t.Item(0).AppendChild($x.CreateTextNode('MeetingScribe'))>$null;"
        "$t.Item(1).AppendChild($x.CreateTextNode($env:MEETINGSCRIBE_TEXT))>$null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         env={**os.environ, "MEETINGSCRIBE_TEXT": text},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as e:
        log.warning("notification failed: %s", e)


def background_process():
    pass  # pythonw.exe from a scheduled task has no window to hide


def lower_priority():
    BELOW_NORMAL_PRIORITY_CLASS = 0x4000
    k32 = ctypes.windll.kernel32
    k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)


def single_instance(lock_file):
    """Holds an exclusive lock on the open file; False if another process has it."""
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def run_loop(seconds):
    time.sleep(seconds)


def play_audio(path):
    import winsound
    winsound.PlaySound(str(path), winsound.SND_FILENAME)


def _consent(path, hive=winreg.HKEY_CURRENT_USER):
    try:
        with winreg.OpenKey(hive, path) as k:
            return winreg.QueryValueEx(k, "Value")[0]
    except OSError:
        return None


def _mic_blocked():
    """What stops desktop apps from opening the mic, or None."""
    if _consent(CONSENT, winreg.HKEY_LOCAL_MACHINE) == "Deny":
        return "Microphone access is off for this device (Settings > Privacy & security > Microphone)"
    if _consent(CONSENT) == "Deny":
        return "Microphone access is off (Settings > Privacy & security > Microphone)"
    if _consent(CONSENT + r"\NonPackaged") == "Deny":
        return ("'Let desktop apps access your microphone' is off "
                "(Settings > Privacy & security > Microphone)")
    return None


def request_permissions(cfg):
    blocked = _mic_blocked()
    if blocked:
        log.error("%s: your side of meetings will be silent", blocked)


def permission_report():
    return [f"Microphone for desktop apps: {_mic_blocked() or 'allowed'}"]


# --------------------------------------------------------------------------
# Processes and windows
# --------------------------------------------------------------------------

class _PROCESSENTRY32W(Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", DWORD),
                ("cntThreads", DWORD), ("th32ParentProcessID", DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def _processes():
    """{pid: (exe name, parent pid)} from a Toolhelp snapshot."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    out = {}
    e = _PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    try:
        ok = k32.Process32FirstW(HANDLE(snap), byref(e))
        while ok:
            out[e.th32ProcessID] = (e.szExeFile, e.th32ParentProcessID)
            ok = k32.Process32NextW(HANDLE(snap), byref(e))
    finally:
        k32.CloseHandle(HANDLE(snap))
    return out


def _app_tree(app, procs=None):
    """(root pids, every pid in their trees, {pid: exe}). Roots are the app's processes
    whose parent is not the app; the trees include children that play the audio (Teams'
    WebView2, a browser's audio service)."""
    procs = procs or _processes()
    exes = app["win"]
    names = {pid: n for pid, (n, _) in procs.items()}
    roots = [pid for pid, (n, parent) in procs.items()
             if n.lower() in exes and names.get(parent, "").lower() not in exes]
    return sorted(roots, key=lambda p: exes.index(names[p].lower())), _descendants(roots, procs), names


def _descendants(roots, procs):
    """The pids in roots and every process below them."""
    children = {}
    for pid, (_, parent) in procs.items():
        children.setdefault(parent, []).append(pid)
    tree, todo = set(), list(roots)
    while todo:
        pid = todo.pop()
        if pid not in tree:
            tree.add(pid)
            todo += children.get(pid, [])
    return tree


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)


def app_windows(app):
    """(owner, title) for every visible, titled top-level window of the app's processes."""
    _, tree, names = _app_tree(app)
    out = []

    def visit(hwnd, _):
        pid = DWORD()
        _user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value in tree and _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip():
                    out.append((names.get(pid.value, app["name"]), buf.value.strip()))
        return True

    _user32.EnumWindows(_WNDENUMPROC(visit), 0)
    return out


# --------------------------------------------------------------------------
# Core Audio (WASAPI) interfaces, via comtypes
# --------------------------------------------------------------------------

import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
E_RENDER, E_CAPTURE, E_CONSOLE, DEVICE_STATE_ACTIVE = 0, 1, 0, 1


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "ctx"),
                  (["in"], c_void_p, "params"), (["out"], POINTER(POINTER(IUnknown)), "iface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "access"), (["out"], POINTER(c_void_p), "store")),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(LPWSTR), "id")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "state")),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint32), "count")),
        COMMETHOD([], HRESULT, "Item", (["in"], c_uint32, "i"), (["out"], POINTER(POINTER(IMMDevice)), "device")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "flow"), (["in"], DWORD, "mask"),
                  (["out"], POINTER(POINTER(IMMDeviceCollection)), "devices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "flow"), (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "device")),
    ]


class IAudioSessionControl2(IUnknown):
    _iid_ = GUID("{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}")
    _methods_ = [  # IAudioSessionControl first, then the 2 additions
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "state")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(LPWSTR), "name")),
        COMMETHOD([], HRESULT, "SetDisplayName", (["in"], LPCWSTR, "name"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(LPWSTR), "path")),
        COMMETHOD([], HRESULT, "SetIconPath", (["in"], LPCWSTR, "path"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "param")),
        COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], POINTER(GUID), "param"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(LPWSTR), "id")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(LPWSTR), "id")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(DWORD), "pid")),
    ]


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "count")),
        COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "i"), (["out"], POINTER(POINTER(IUnknown)), "session")),
    ]


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], POINTER(GUID), "guid"), (["in"], DWORD, "flags"),
                  (["out"], POINTER(c_void_p), "control")),
        COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], POINTER(GUID), "guid"), (["in"], DWORD, "flags"),
                  (["out"], POINTER(c_void_p), "volume")),
        COMMETHOD([], HRESULT, "GetSessionEnumerator",
                  (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "sessions")),
    ]


class WAVEFORMATEX(Structure):
    _fields_ = [("wFormatTag", c_ushort), ("nChannels", c_ushort), ("nSamplesPerSec", DWORD),
                ("nAvgBytesPerSec", DWORD), ("nBlockAlign", c_ushort), ("wBitsPerSample", c_ushort),
                ("cbSize", c_ushort)]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_int, "mode"), (["in"], DWORD, "flags"),
                  (["in"], c_longlong, "duration"), (["in"], c_longlong, "period"),
                  (["in"], POINTER(WAVEFORMATEX), "fmt"), (["in"], POINTER(GUID), "session")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "latency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "mode"), (["in"], POINTER(WAVEFORMATEX), "fmt"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "closest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "fmt")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(c_longlong), "default"),
                  (["out"], POINTER(c_longlong), "minimum")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], HANDLE, "event")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "iid"), (["out"], POINTER(POINTER(IUnknown)), "svc")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(c_void_p), "data"), (["out"], POINTER(c_uint32), "frames"),
                  (["out"], POINTER(DWORD), "flags"), (["out"], POINTER(c_uint64), "position"),
                  (["out"], POINTER(c_uint64), "qpc")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(ctypes.c_long), "hr"),
                  (["out"], POINTER(POINTER(IUnknown)), "iface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "operation")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


def _enumerator():
    return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)


def _subkeys(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            out = []
            for i in range(10_000):
                try:
                    out.append(winreg.EnumKey(key, i))
                except OSError:
                    return out
    except OSError:
        return []


def _mic_holders():
    """Lower-cased consent-store keys of the apps holding the microphone right now:
    packaged apps by package name ("msteams_8wekyb3d8bbwe"), desktop apps by exe path
    ("nonpackaged\\c:#program files#google#chrome#application#chrome.exe")."""
    names = _subkeys(CONSENT) + ["NonPackaged\\" + k for k in _subkeys(CONSENT + r"\NonPackaged")]
    out = set()
    for name in names:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT + "\\" + name) as k:
                start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
            if start and stop == 0:
                out.add(name.lower())
        except OSError:
            pass
    return out


def _holds_mic(app, holders):
    return any(h.startswith(app["win_packaged"]) if app["win_packaged"] and "\\" not in h
               else h.startswith("nonpackaged\\") and h.rsplit("#", 1)[-1] in app["win"]
               for h in holders)


class AudioProbe:
    """Whether a meeting app holds the mic and is playing call audio: an active WASAPI
    capture / playback session in its process tree. For the mic, the privacy consent
    store counts too (Teams). Edge records mic use there only sometimes, so the sessions
    are needed. Our own capture runs as python.exe, which is no meeting app."""

    def __init__(self):
        self.devices = _enumerator()

    def active_output_pids(self):
        return self.active_pids(E_RENDER)

    def active_pids(self, flow):
        """Processes with a running (not just open) session on any device of that flow:
        E_RENDER for playback, E_CAPTURE for microphones."""
        pids = set()
        coll = self.devices.EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
        for i in range(coll.GetCount()):
            try:
                mgr = coll.Item(i).Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None) \
                    .QueryInterface(IAudioSessionManager2)
                sessions = mgr.GetSessionEnumerator()
                for j in range(sessions.GetCount()):
                    s = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                    if s.GetState() == 1:  # AudioSessionStateActive
                        pids.add(s.GetProcessId())
            except comtypes.COMError as e:
                log.debug("audio sessions on device %d: %s", i, e)
        return pids

    def app_states(self, apps):
        """{app id: (holds the mic, plays audio)}, from one look at the registry, the
        process list and the audio sessions."""
        holders, procs = _mic_holders(), _processes()
        recording, playing = self.active_pids(E_CAPTURE), self.active_pids(E_RENDER)
        out = {}
        for a in apps:
            tree = _app_tree(a, procs)[1]
            out[a["id"]] = (_holds_mic(a, holders) or bool(tree & recording), bool(tree & playing))
        return out

    def app_processes(self, app):
        roots, tree, names = _app_tree(app)
        held = _holds_mic(app, _mic_holders())
        recording, playing = self.active_pids(E_CAPTURE), self.active_pids(E_RENDER)
        return [{"name": names[p], "pid": p, "input": p in recording or (held and p in roots),
                 "output": p in playing}
                for p in sorted(tree) if p in roots or p in playing or p in recording]

    def default_input_device(self):
        try:
            return self.devices.GetDefaultAudioEndpoint(E_CAPTURE, E_CONSOLE).GetId()
        except comtypes.COMError:
            return None


# --------------------------------------------------------------------------
# The meeting app's audio: WASAPI process loopback
# --------------------------------------------------------------------------

class _ActivationParams(Structure):
    _fields_ = [("ActivationType", c_int), ("TargetProcessId", DWORD), ("ProcessLoopbackMode", c_int)]


class _Blob(Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", c_void_p)]


class _PropVariant(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", _Blob)]


class _ActivationDone(comtypes.COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()
        self.result = None

    def ActivateCompleted(self, operation):
        try:
            hr, iface = operation.GetActivateResult()
            self.result = (hr, iface)
        except Exception as e:
            self.result = (e, None)
        finally:
            self.event.set()
        return 0


def _activate_loopback(pid, include_tree):
    """An IAudioClient that records what the process tree of pid plays (include_tree) or
    everything except it."""
    mmdev = ctypes.WinDLL("Mmdevapi.dll")
    fn = mmdev.ActivateAudioInterfaceAsync
    fn.argtypes = [LPCWSTR, POINTER(GUID), POINTER(_PropVariant),
                   POINTER(IActivateAudioInterfaceCompletionHandler),
                   POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    fn.restype = HRESULT
    params = _ActivationParams(1, pid, 0 if include_tree else 1)  # PROCESS_LOOPBACK; INCLUDE/EXCLUDE tree
    pv = _PropVariant(vt=65)  # VT_BLOB
    pv.blob = _Blob(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p))
    done = _ActivationDone()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    iid = IAudioClient._iid_
    fn("VAD\\Process_Loopback", byref(iid), byref(pv),
       done.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(op))
    if not done.event.wait(10):
        raise TimeoutError("process loopback activation timed out")
    hr, iface = done.result
    if isinstance(hr, Exception):
        raise hr
    if hr < 0:
        raise OSError(f"process loopback activation failed (0x{hr & 0xFFFFFFFF:08X}); "
                      "needs Windows 10 build 20348 or Windows 11")
    return iface.QueryInterface(IAudioClient)


class ProcessLoopbackCapture:
    """The meeting app's audio via WASAPI process loopback: 48 kHz 16-bit stereo, mixed to
    mono and handed to the writer from a polling thread. In "all" mode it records every
    process except this one."""

    SR = 48000

    def __init__(self, writer, app, mode):
        self.writer, self.app, self.mode = writer, app, mode
        self.stopped = False
        self.running = False
        self.thread = None

    def start(self):
        procs = _processes()
        roots, _, names = _app_tree(self.app, procs)
        if self.mode == "app" and len(roots) > 1:
            # Several top-level copies (e.g. a browser started twice with different profile
            # folders): follow the one playing audio. Loopback takes a single process tree.
            playing = AudioProbe().active_output_pids()
            roots.sort(key=lambda r: not (_descendants([r], procs) & playing))
        if self.mode == "app" and roots:
            pid, include = roots[0], True
            log.info("system audio: process loopback on %s (pid %d) and its children", names[pid], pid)
        else:
            if self.mode == "app":
                log.warning("no %s process found; recording all system audio", self.app["name"])
            pid, include = os.getpid(), False
            log.info("system audio: process loopback on everything except MeetingScribe")
        client = _activate_loopback(pid, include)
        fmt = WAVEFORMATEX(1, 2, self.SR, self.SR * 4, 4, 16, 0)  # WAVE_FORMAT_PCM
        flags = 0x00020000 | 0x80000000 | 0x08000000  # LOOPBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
        client.Initialize(0, flags, 2_000_000, 0, byref(fmt), None)  # shared mode, 200 ms buffer
        self.client = client
        self.capture = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
        client.Start()
        self.running, self.stopped = True, False
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            while self.running:
                time.sleep(0.01)
                while self.running and self.capture.GetNextPacketSize():
                    data, frames, flags, _, _ = self.capture.GetBuffer()
                    arrived = time.monotonic()
                    if flags & 0x2 or not data:  # AUDCLNT_BUFFERFLAGS_SILENT
                        x = np.zeros(frames, np.float32)
                    else:
                        pcm = np.frombuffer(ctypes.string_at(data, frames * 4), np.int16)
                        x = pcm.reshape(-1, 2).mean(axis=1, dtype=np.float32) / 32768
                    self.capture.ReleaseBuffer(frames)
                    self.writer.write(x, self.SR, arrived)
        except Exception as e:  # device lost, app quit: the recording's watchdog restarts us
            log.warning("system audio capture stopped: %s", e)
            self.stopped = True
        finally:
            comtypes.CoUninitialize()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(3)
            self.thread = None
        if getattr(self, "client", None) is not None:
            try:
                self.client.Stop()
            except comtypes.COMError as e:
                log.warning("system audio stop: %s", e)
            self.client = self.capture = None


def system_audio_capture(cfg, writer, app, mode):
    return ProcessLoopbackCapture(writer, app, mode)


# --------------------------------------------------------------------------
# Reading name tiles: the window's picture, and the OCR built into Windows
# --------------------------------------------------------------------------

_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
for _f, _args, _res in (("GetDC", [HWND], c_void_p), ("ReleaseDC", [HWND, c_void_p], c_int),
                        ("GetWindowRect", [HWND, c_void_p], BOOL), ("IsIconic", [HWND], BOOL),
                        ("PrintWindow", [HWND, c_void_p, c_uint32], BOOL)):
    getattr(_user32, _f).argtypes, getattr(_user32, _f).restype = _args, _res
for _f, _args, _res in (("CreateCompatibleDC", [c_void_p], c_void_p), ("DeleteDC", [c_void_p], BOOL),
                        ("CreateCompatibleBitmap", [c_void_p, c_int, c_int], c_void_p),
                        ("SelectObject", [c_void_p, c_void_p], c_void_p), ("DeleteObject", [c_void_p], BOOL),
                        ("GetDIBits", [c_void_p, c_void_p, c_uint32, c_uint32, c_void_p, c_void_p, c_uint32], c_int)):
    getattr(_gdi32, _f).argtypes, getattr(_gdi32, _f).restype = _args, _res


def window_image(app, is_meeting=None):
    """The app's largest visible window as H x W x 3 RGB, preferring one that shows the
    meeting. PrintWindow draws it even when other windows cover it; a minimized one has
    nothing to draw, so that is None."""
    _user32.SetThreadDpiAwarenessContext(c_void_p(-4))  # real pixels: PER_MONITOR_AWARE_V2
    _, tree, _ = _app_tree(app)
    found = []

    def visit(hwnd, _):
        pid = DWORD()
        _user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value in tree and _user32.IsWindowVisible(hwnd) and not _user32.IsIconic(hwnd):
            r = (c_int * 4)()
            _user32.GetWindowRect(hwnd, r)
            n = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if n and r[2] - r[0] >= 240 and r[3] - r[1] >= 160:
                found.append((bool(is_meeting and is_meeting(buf.value)), (r[2] - r[0]) * (r[3] - r[1]),
                              hwnd, r[2] - r[0], r[3] - r[1]))
        return True

    _user32.EnumWindows(_WNDENUMPROC(visit), 0)
    if not found:
        return None
    _, _, hwnd, w, h = max(found, key=lambda f: f[:2])
    screen = _user32.GetDC(hwnd)
    mem = _gdi32.CreateCompatibleDC(screen)
    bmp = _gdi32.CreateCompatibleBitmap(screen, w, h)
    old = _gdi32.SelectObject(mem, bmp)
    try:
        if not _user32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT: GPU-drawn windows too
            return None
        header = (c_int * 10)(40, w, -h, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)  # BITMAPINFOHEADER, top-down
        data = np.zeros((h, w, 4), np.uint8)
        if not _gdi32.GetDIBits(mem, bmp, 0, h, data.ctypes.data_as(c_void_p), header, 0):
            return None
    finally:
        _gdi32.SelectObject(mem, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mem)
        _user32.ReleaseDC(hwnd, screen)
    rgb = data[:, :, 2::-1].copy()
    return rgb if rgb.max() > 0 else None


_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Wait-Op($op, [Type]$type) {
  $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
  $null = $task.Wait(-1)
  $task.Result
}
$bytes = [Convert]::FromBase64String([Console]::In.ReadToEnd())
$mem = New-Object IO.MemoryStream(, $bytes)
$stream = [IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($mem)
$decoder = Wait-Op ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Wait-Op ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = Wait-Op ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$out = foreach ($line in $result.Lines) {
  $r = $line.Words | ForEach-Object { $_.BoundingRect }
  $x = ($r | ForEach-Object { $_.X } | Measure-Object -Minimum).Minimum
  $y = ($r | ForEach-Object { $_.Y } | Measure-Object -Minimum).Minimum
  $x2 = ($r | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum
  $y2 = ($r | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum
  [pscustomobject]@{ text = $line.Text; x = $x; y = $y; w = $x2 - $x; h = $y2 - $y }
}
ConvertTo-Json -Compress -InputObject @($out)
"""


def read_text(img):
    """Text lines in an RGB picture, from Windows' own OCR through PowerShell, the same
    bridge as notify(), so no new native code has to get past Smart App Control. The picture
    goes over a pipe and is never written to disk. Small windows are doubled first, because
    tile names on them are tiny for OCR."""
    scale = 2 if max(img.shape[:2]) < 1600 else 1
    big = img.repeat(scale, axis=0).repeat(scale, axis=1)[::-1, :, ::-1]  # BMPs are bottom-up BGR
    h, w = big.shape[:2]
    row = (w * 3 + 3) & ~3
    pad = np.zeros((h, row - w * 3), np.uint8)
    body = np.concatenate([big.reshape(h, w * 3), pad], axis=1).tobytes()
    header = (c_int * 10)(40, w, h, 1 | (24 << 16), 0, len(body), 0, 0, 0, 0)
    bmp = (b"BM" + (54 + len(body)).to_bytes(4, "little") + bytes(4) + (54).to_bytes(4, "little")
           + bytes(header) + body)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _OCR_SCRIPT],
                       input=base64.b64encode(bmp), capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    lines = json.loads(r.stdout.decode("utf-8-sig") or "[]")
    return [{"text": ln["text"], "x": ln["x"] / scale, "y": ln["y"] / scale,
             "w": ln["w"] / scale, "h": ln["h"] / scale} for ln in lines]
