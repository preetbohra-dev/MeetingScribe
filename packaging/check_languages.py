#!/usr/bin/env python3
"""A meeting that changes language partway through, processed by the real speech model: what is
said in each language must reach the note, in its own script.
    python packaging/check_languages.py [--no-download] <program> [its arguments]
The note writer is left unavailable on purpose: the speech path and saving are what is checked here.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    args = sys.argv[1:]
    download = "--no-download" not in args
    program = [a for a in args if a != "--no-download"]
    with tempfile.TemporaryDirectory() as t:
        base = Path(t)
        config = base / "config.toml"
        config.write_text(f'notify = false\nkeep_audio = false\nspeaker_id = false\nllm_backend = "ollama"\nignore_memory_limit = true\n'
                          f'work_dir = {json.dumps(str(base / "w"))}\noutput_dir = {json.dumps(str(base / "n"))}\nlog_dir = {json.dumps(str(base / "l"))}\n', encoding="utf-8")
        env = {**os.environ, "MEETINGSCRIBE_CONFIG": str(config)}
        d = base / "w" / "sessions" / "2026-11-08_100000"
        d.mkdir(parents=True)
        with wave.open(str(HERE / "fixtures" / "english_hindi_english.wav")) as w:
            (d / "mic.pcm").write_bytes(w.readframes(w.getnframes()))
        (d / "session.json").write_text(json.dumps({"status": "recorded", "title": "Language check", "source": "Zoom", "app": "zoom", "started_at": "2026-11-08T10:00:00+00:00", "duration_s": 52}), encoding="utf-8")
        if download:
            subprocess.run([*program, "download-models"], env=env, check=True, timeout=2400)
        run = subprocess.run([*program, "process", str(d)], env=env, timeout=1800, capture_output=True)
        meta = json.loads((d / "session.json").read_text(encoding="utf-8"))
        if run.returncode or meta["status"] != "done":
            sys.exit(f"the meeting was lost: exit {run.returncode}, status {meta['status']}\n{run.stderr.decode('utf-8', 'replace')[-1500:]}")
        turns = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
        said = " ".join(t["text"] for t in turns)
        sys.stdout.reconfigure(encoding="utf-8")
        print(said)
        note = Path(meta["notes"]).read_text(encoding="utf-8")
        hindi = len(re.findall(r"[ऀ-ॿ]+", said))
        assert hindi >= 15, f"Hindi did not come out in Hindi script: {hindi} words"
        assert "ईमेल" in said or "इमेल" in said or "ग्राहक" in said, "the middle of the Hindi was lost"
        assert "migration" in said.lower(), "the English at the start was lost"
        assert "friday" in said.lower() or "runbook" in said.lower(), "the English after the Hindi was lost"
        assert re.search(r"[ऀ-ॿ]", note), "the Hindi did not reach the note"
    print("a meeting that changes language keeps every part of it")


if __name__ == "__main__":
    main()
