"""CI: the packaged Mac app turns a spoken two-track meeting into a note, with small models.
Run with any Python 3 on a Mac: python packaging/smoke_mac.py dist/MeetingScribe.app/Contents/MacOS/MeetingScribe"""
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

app = sys.argv[1]
base = Path(tempfile.mkdtemp())
config = base / "config.toml"
config.write_text(f'''work_dir = {json.dumps(str(base / "work"))}
output_dir = {json.dumps(str(base / "notes"))}
log_dir = {json.dumps(str(base / "logs"))}
notify = false
keep_audio = false
whisper_model = "mlx-community/whisper-tiny-mlx"
mlx_llm_model = "mlx-community/Llama-3.2-1B-Instruct-4bit"
''')
env = {**os.environ, "MEETINGSCRIBE_CONFIG": str(config)}


def speech(voice, text):
    """16 kHz mono 16-bit samples of macOS speaking text."""
    aiff, wav = base / "s.aiff", base / "s.wav"
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
    with wave.open(str(wav)) as w:
        return w.readframes(w.getnframes())


me = speech("Samantha", "Good morning everyone. Where are we on the database migration? Please send me the report by Friday.")
others = speech("Daniel", "Hi, this is Tom. The database migration finished last night and every table is validated. "
                          "I will send the report by Friday and raise the firewall change with the security team.")
silence = bytes(32000 * 2)
d = base / "work" / "sessions" / "2026-11-08_100000"
d.mkdir(parents=True)
(d / "mic.pcm").write_bytes(silence + me + silence + bytes(len(others)))
(d / "system.pcm").write_bytes(silence + bytes(len(me)) + silence + others)
(d / "session.json").write_text(json.dumps({"status": "recorded", "title": "Zoom meeting", "source": "Zoom", "app": "zoom",
                                            "started_at": "2026-11-08T10:00:00+00:00", "duration_s": 40}))
subprocess.run([app, "download-models"], env=env, check=True, timeout=1800)
run = subprocess.run([app, "process", str(d)], env=env, timeout=1800)
print((base / "logs" / "process.log").read_text(errors="replace")[-3000:])
assert run.returncode == 0, run.returncode
meta = json.loads((d / "session.json").read_text())
assert meta["status"] == "done", meta
note = Path(meta["notes"]).read_text()
print(note)
assert "could not be written" not in note, "the AI model did not run"
assert "migration" in note.lower() and "## Transcript" in note
assert Path(meta["notes"]).with_suffix(".json").exists()
print("the call was named:", meta["title"])
assert meta["title"] in Path(meta["notes"]).stem
print("smoke test passed")
