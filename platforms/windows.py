"""Windows side of MeetingScribe.

Speakers, notes and Jira are shared with macOS; transcription and the LLM still
use mlx, which is Apple-only. Call detection and Teams audio capture are still
to come: the microphone consent store in the registry will tell when Teams
holds the mic, and WASAPI process loopback on ms-teams.exe will record its
audio. Until then run, status and test-capture stop with a clear message.
"""

import ctypes
import logging
import msvcrt
import os
import time

log = logging.getLogger("meetingscribe")

_LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
DIRS = {
    "work_dir": os.path.join(_LOCAL, "MeetingScribe"),
    "log_dir": os.path.join(_LOCAL, "MeetingScribe", "Logs"),
}

NOT_YET = "Recording Teams calls on Windows is not built yet."


def notify(text):
    log.info("notification: %s", text)


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


def request_permissions(cfg):
    pass


def permission_report():
    return []


class AudioProbe:
    def __init__(self):
        raise NotImplementedError(NOT_YET)


def teams_windows():
    raise NotImplementedError(NOT_YET)


def system_audio_capture(cfg, writer):
    raise NotImplementedError(NOT_YET)
