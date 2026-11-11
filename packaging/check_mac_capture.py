#!/usr/bin/env python3
"""On a real Mac: both ways of recording the other side of a call start without a mistake in our
code. A test machine may refuse the permission, which is allowed; a missing name is not.
    python packaging/check_mac_capture.py
"""

import ctypes
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
    try:
        capture.stop()
    except Exception as e:
        print(f"  stop after a failed start: {type(e).__name__}: {e}")
    if worker.is_alive():
        return f"{cls.__name__} ({mode}) did not finish starting within 60 s"
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
