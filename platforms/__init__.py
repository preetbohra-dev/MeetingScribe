"""Operating-system layer. macos.py and windows.py expose the same names:

  CTL, INSTALL, DEVICE, SECRET_STORE  how messages name the control script, installer,
                            computer and secret store
  DIRS                      default work_dir and log_dir
  MODEL_DEFAULTS            transcription / LLM backends and models for this machine
  notify(text)              desktop notification
  background_process()      keep the daemon out of the Dock / taskbar
  lower_priority()          let meeting processing yield to the foreground
  single_instance(f)        exclusive lock on an open file; False if already held
  run_loop(seconds)         wait while native audio callbacks run
  play_audio(path)          play a WAV and block until it ends
  request_permissions(cfg)  ask for optional permissions once, at daemon start
  permission_report()       lines for `status`
  AudioProbe()              .teams_state(cfg) -> (mic, out), .teams_processes(cfg),
                            .default_input_device()
  teams_windows()           [(owner, title)] for Teams' top-level windows
  system_audio_capture(cfg, writer)  Teams' audio into a TrackWriter
"""

import sys


def load():
    if sys.platform == "darwin":
        from . import macos
        return macos
    if sys.platform == "win32":
        from . import windows
        return windows
    sys.exit(f"MeetingScribe runs on macOS and Windows, not {sys.platform}.")
