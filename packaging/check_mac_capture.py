#!/usr/bin/env python3
"""On a real Mac: both ways of recording the other side of a call start without a mistake in our
code. A test machine may refuse the permission, or wait for someone to answer its prompt, which
is allowed once our code has reached macOS; a missing name is not.
    python packaging/check_mac_capture.py
"""

import ctypes
import linecache
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
from platforms import macos  # noqa: E402

OUR_MISTAKES = (NameError, AttributeError, ImportError, TypeError, ctypes.ArgumentError)
REFUSED = (PermissionError, RuntimeError, TimeoutError, OSError)  # what the system may answer


def waiting_on_macos(worker):
    """The line a stuck start is on, if that line is a call into Core Audio itself."""
    frame = sys._current_frames().get(worker.ident)
    while frame is not None and not frame.f_code.co_filename.endswith("macos.py"):
        frame = frame.f_back  # the innermost frame of ours, below any library frames
    if frame is None:
        return None
    line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
    return line if "self.ca." in line else f"not in a Core Audio call: macos.py line {frame.f_lineno}: {line}"


def try_start(cls, mode, folder):
    writer = ms.TrackWriter(folder / f"{cls.__name__}-{mode}.pcm", time.monotonic())
    capture, box = cls(writer, ms.APPS_BY_ID["teams"], mode), {}

    def start():
        try:
            capture.start()
            box["result"] = "started"
        except BaseException as e:
            box["error"] = e

    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    worker.join(60)
    if worker.is_alive():
        where = waiting_on_macos(worker)
        if where and where.startswith("not in"):
            return f"{cls.__name__} ({mode}) is stuck, {where}"
        if not where:
            return f"{cls.__name__} ({mode}) did not finish starting within 60 s, and no frame of ours was found"
        print(f"{cls.__name__} ({mode}): waiting on macOS, probably for the permission prompt, at: {where}", flush=True)
        return None
    try:
        capture.stop()
    except Exception as e:
        print(f"  stop after a failed start: {type(e).__name__}: {e}")
    e = box.get("error")
    if isinstance(e, OUR_MISTAKES) or (e is not None and not isinstance(e, REFUSED)):
        return f"{cls.__name__} ({mode}): {type(e).__name__}: {e}"
    print(f"{cls.__name__} ({mode}): {box.get('result') or f'refused by the system ({type(e).__name__}: {e})'}", flush=True)
    return None


def main():
    with tempfile.TemporaryDirectory() as t:
        failed = [f for cls in (macos.ProcessTapCapture, macos.SystemAudioCapture) for mode in ("all", "app")
                  if (f := try_start(cls, mode, Path(t)))]
    if failed:
        sys.exit("Recording the other side of a call failed:\n- " + "\n- ".join(failed))
    print("both ways of recording the other side start")


if __name__ == "__main__":
    main()
