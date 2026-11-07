<p align="center"><img src="site/icon.svg" width="72" alt=""></p>

<h1 align="center">MeetingScribe</h1>

<p align="center">
Teams meeting notes that write themselves, on your Mac or Windows PC.<br>
<a href="https://meetingscribe.preetbohra.com"><b>Website</b></a> ·
<a href="https://meetingscribe.preetbohra.com/#film">3-minute demo</a> ·
<a href="#install">Install</a>
</p>

---

MeetingScribe runs in the background. When Microsoft Teams starts using your microphone it records the call, and a
few minutes after you hang up it saves a Markdown note to `Documents/Meeting Notes/` with:

- a **summary** and **action items** with owners,
- a **transcript** that says who said what, recognising people it has heard before,
- **only the work**: small talk and personal chat are left out.

You never press record. Recording, transcription and every model run on your computer, and nothing is uploaded.
If you use Jira, `ctl jira` can turn the meeting's action items into tickets, and you approve each one.

## Contents

- [How it works](#how-it-works)
- [Install](#install)
- [Everyday use](#everyday-use)
- [Speakers](#speakers)
- [Jira](#jira)
- [What a note looks like](#what-a-note-looks-like)
- [Settings](#settings)
- [Privacy and consent](#privacy-and-consent)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

## How it works

```
 During the call                          After you hang up
 ───────────────                          ─────────────────
 Teams takes the mic ─► start recording   1. cut long silences
   your mic       → mic.pcm   ("Me")      2. transcribe each track (Whisper large-v3-turbo)
   Teams' audio   → system.pcm (others)   3. fingerprint voices and match known people
 Teams lets go of the mic ─► stop         4. merge both tracks by time, tag likely echoes
                                          5. drop small talk (Qwen 2.5)
                                          6. write summary + action items, save the note
```

| | Mac | Windows |
|---|---|---|
| Detecting the call | Core Audio lists every process using audio (macOS 14.4+) | Windows' microphone privacy record says when Teams holds the mic |
| Capturing Teams | Core Audio *process tap* on the Teams processes | WASAPI *process loopback* on the Teams process tree |
| Transcription | mlx-whisper on the GPU | faster-whisper (NVIDIA GPU if there is one, else CPU) |
| Notes and small-talk filter | Qwen 2.5 14B on MLX | Qwen 2.5 GGUF on llama.cpp: 14B with 24 GB of RAM or more, else 7B |
| Voice fingerprints | ECAPA-TDNN (SpeechBrain) | WeSpeaker ResNet34 (ONNX Runtime) |
| Runs in the background via | a launch agent | a scheduled task at logon |

Both check once a second; when Teams holds the mic for `start_after_s`, recording starts, and `stop_after_s` after
it lets go, recording stops. Capture is audio-only and Teams-only, so there's no screen-recording indicator, and
you keep hearing the call as normal. If you're muted, an open meeting window keeps the recording going for up to
10 minutes. A crash or power cut keeps what was recorded and processes it on the next start; switching headsets
mid-call restarts mic capture; if the Teams-only capture stays silent, it falls back to all system audio.

## Install

One command on either system. It sets up Python and the packages, shows the terms (type `I agree`), downloads the
models once and starts the background recorder. No admin rights, no virtual audio drivers. Running it again is
safe.

**Mac** (Apple silicon, macOS 14.4 or later, ~10 GB free):

```bash
git clone https://github.com/preetbohra-dev/MeetingScribe.git
cd MeetingScribe
./install.sh
```

**Windows** (Windows 11, or Windows 10 build 20348+; ~6 GB free, ~11 GB with 24 GB of RAM):

```powershell
git clone https://github.com/preetbohra-dev/MeetingScribe.git
cd MeetingScribe
.\install
```

No Python? The installer gets one for you: Homebrew or [uv](https://docs.astral.sh/uv/) on the Mac, `winget` on
Windows. The model download is the only step that uses the network; after it every run is offline
(`HF_HUB_OFFLINE=1`). Windows doesn't need scipy or torch, whose DLLs Smart App Control would block.

<details>
<summary>What the installer creates</summary>

| | Mac | Windows |
|---|---|---|
| Launcher | `~/Applications/MeetingScribe.app`, so macOS permissions go to *MeetingScribe* rather than Terminal | — |
| Background start | Launch agent `~/Library/LaunchAgents/local.meetingscribe.plist` | Scheduled task *MeetingScribe* (at logon, rechecked every 5 minutes) |
| Logs | `~/Library/Logs/MeetingScribe/` | `%LOCALAPPDATA%\MeetingScribe\Logs\` |
| Recordings and voiceprints | `~/Library/Application Support/MeetingScribe/` | `%LOCALAPPDATA%\MeetingScribe\` |

`daemon.log` covers detecting and recording, `process.log` transcription and notes. `./uninstall.sh` or
`.\uninstall` removes the background part and keeps your notes.
</details>

### Permissions (one time)

**Mac**

| Permission | Needed | How |
|---|---|---|
| Microphone | Yes | Click **Allow** when asked on first start. Dismissed it? `tccutil reset Microphone local.meetingscribe`, then `./ctl.sh restart`. |
| System Audio Recording Only | Yes | Asked at the first call. Otherwise: **Privacy & Security → Screen & System Audio Recording → System Audio Recording Only**, turn on MeetingScribe. |
| Files and Folders → Desktop | Only if the folder is on your Desktop | Click **Allow** when asked. |
| Screen Recording | Optional | Only used to read the Teams window title for the note's name. Same pane, top section: **+**, <kbd>⌘⇧G</kbd>, `~/Applications/MeetingScribe.app`. |

Accessibility and Automation are not needed. Re-running `install.sh` re-signs the app, so macOS will ask again.

**Windows:** nothing to grant. Teams' audio and window titles need no permission. The microphone works as long as
**Settings → Privacy & security → Microphone → Let desktop apps access your microphone** is on (the default);
`.\ctl status` tells you if it isn't.

### Check it works

1. In Teams: **Settings → Devices → Make a test call**.
2. In a terminal: `./ctl.sh logs` (Mac) or `.\ctl logs` (Windows). You should see `meeting detected`, then
   `process tap running` (Mac) or `process loopback on ms-teams.exe` (Windows).
3. Hang up. After `meeting ended`, the note appears in `Documents/Meeting Notes/`.

The test bot plays your message back, so in a test call your words also appear on the remote side. That
doesn't happen in real meetings.

## Everyday use

There's nothing to do: join calls as usual. `ctl` is there when you want control. Examples below use the Mac's
`./ctl.sh`; on Windows type `.\ctl` instead, with the same arguments.

```bash
./ctl.sh status       # running? what does it see right now?
./ctl.sh logs         # live log
./ctl.sh restart      # after editing config.toml
./ctl.sh stop         # pause until ./ctl.sh start or next login
./ctl.sh start
```

If processing ever fails, the raw audio is kept so you can retry with `./ctl.sh process <session folder>`
(sessions are in the recordings folder listed under *What the installer creates*).

How long processing takes depends on the machine. An Apple silicon Mac needs a few minutes for an hour-long
meeting. A Windows laptop without an NVIDIA GPU is slower: on a Core i5-1135G7, transcription took about one and a
half times as long as the speech, so notes for a busy one-hour meeting take an hour or more. They still appear
on their own; the PC stays usable because processing runs at low priority.

## Speakers

Everyone on the remote side arrives in one audio stream, so MeetingScribe tells them apart by voice. Each
second of speech gets a voice fingerprint, similar fingerprints are grouped into one speaker, and each speaker is
compared with your voiceprint library. Known voices get their name; new ones become *Speaker 1*, *Speaker 2* and
so on, and the note lists them under **Unrecognized voices**.

```bash
./ctl.sh listen                               # play ~10 s of each unknown voice from the latest meeting
./ctl.sh name "Speaker 2" "Priya Shah"        # name them: rewrites the note and saves the voiceprint
./ctl.sh name "Speaker 1" "Tom Berg" 2026-10-28   # a specific meeting
./ctl.sh voices                               # list known voices
./ctl.sh voices --forget "Priya Shah"         # delete one
```

From the next meeting on, named people are labelled automatically, and each confident match refines their
voiceprint. Voiceprints stay on the computer that made them (the Mac and Windows models differ).

- **Wrong person?** Rename the label in that meeting, then `--forget` the wrong voiceprint if it's polluted.
- **One person split in two?** Give both labels the same name and they merge.
- **Two people merged into one?** Lower `speaker_cluster_threshold` (default 0.6).
- **Too many fragments?** Raise `speaker_min_seconds` (default 15) or set `max_speakers`.

In testing with synthetic voices, the same voice scored 0.83 to 0.90 against its voiceprint and different
voices never scored above 0.51. Real Teams audio is noisier, so expect somewhat lower scores.

## Jira

`./ctl.sh jira` turns the latest meeting's work into Jira tickets. It's the only part of MeetingScribe that uses
the network, it only runs when you start it, and it only talks to your own Jira site.

1. **Sign in** with a personal access token (Server / Data Center), an API token and email (Cloud), or a browser
   session cookie (SSO-only sites). The secret is kept in the macOS Keychain or Windows Credential Manager,
   never in a file.
2. **Pick the space** new tickets go to.
3. **Extract** the work from the note: title, details, owner, due date, priority.
4. **Search every space** you can see for tickets that already cover each item.
5. **Suggest** an action for each item:
   - same work, ticket open → **comment** on it with the meeting's update;
   - same work, ticket done, problem not back → **skip**;
   - nothing matches, or a done problem is back → **create** a ticket, linked to related ones.
6. **You decide.** Every draft is shown in full: **y** to push, **n** to skip, **e** to edit any field.

The note gets a `## Jira` section linking what was created, and re-running won't push anything twice.

```bash
./ctl.sh jira --session 2026-11-05    # a specific meeting
./ctl.sh jira --project EDA           # skip the space question
./ctl.sh jira --dry-run               # draft everything, write nothing
./ctl.sh jira --forget                # remove the saved sign-in
```

Always read the drafts: the model can still pick the wrong due date or guess a field.

## What a note looks like

```markdown
# Migration sync
**Date:** Wednesday 28 October 2026, 14:00–14:47 (47 min)
**Participants:** Me, Samantha Lee, Daniel Ortiz, Speaker 3 (unrecognized)
**Filtered:** 12 non-work line(s) left out

## Summary
- Database migration finished; tables validated.

## Action Items
- [ ] Me: Update the reporting jobs by Thursday
- [ ] Speaker 3: Raise the firewall change with the security team today

## Transcript
**[00:00:00] Me:** Good morning everyone, let's go through the migration status
**[00:00:05] Samantha Lee:** The database migration finished last night...
```

Apart from the small-talk filter, nothing is deleted from the transcript. Lines that might be an echo of the
remote side, or that Whisper wasn't sure about, are kept and tagged (`drop_suspect_lines = true` removes them).

**The small-talk filter** (`work_only = true`, the default) has Qwen 2.5 list lines that aren't about work,
then re-check each one with the lines around it. Meeting logistics and short replies to work points are kept;
when unsure, it keeps the line. Removed lines can't be recovered. `./ctl.sh refilter` applies it to older
meetings.

## Settings

Edit `config.toml`, then `./ctl.sh restart`. Every key is optional. The ones you're most likely to change:

| Key | What it does |
|---|---|
| `my_name` | The label for your own lines (default `Me`) |
| `language` | e.g. `"en"` to skip language detection |
| `mic_device` | Part of a mic's name, e.g. `"AirPods"` |
| `keep_audio` | Save a stereo WAV (left = you, right = others) next to each note |
| `work_only` | `false` keeps every line |
| `capture_mode` | `"all"` records all system audio if the remote side comes out silent |
| `whisper_model`, `mlx_llm_model` | Mac: smaller models for 8 GB Macs, e.g. `mlx-community/whisper-small-mlx` and `mlx-community/Qwen2.5-7B-Instruct-4bit` |
| `gguf_model` | Windows: `owner/repo/file.gguf` on Hugging Face, e.g. the 14B one on a 16 GB PC with time to spare |
| `llm_backend` | `"ollama"` to use a local Ollama server instead (loopback addresses only) |
| `speaker_*`, `max_speakers` | Speaker tuning, see [Speakers](#speakers) |

Model defaults depend on the system and are picked in `platforms/macos.py` and `platforms/windows.py`. Run
`./ctl.sh download-models` after changing a model.

## Privacy and consent

- Audio is written only to the recordings folder (see *What the installer creates*) and deleted once the note is
  saved.
- Whisper and the LLM run on your computer through MLX, faster-whisper and llama.cpp (or Ollama on 127.0.0.1).
- Offline flags are set before any ML library loads, and no saved Hugging Face token is ever sent. To check on a
  Mac, run `nettop -p $(pgrep -f "meeting_scribe.py process")` while a meeting is processed; on Windows, watch
  the python process in Resource Monitor's Network tab. There are no connections.
- Voiceprints are a few hundred numbers per person in `voices.json`. They can't be turned back into audio, but
  they are biometric data.

> **You are responsible for how you use MeetingScribe.** Many places and employers require everyone in a meeting
> to agree to being recorded, and voiceprints are covered by laws such as GDPR and Illinois' BIPA. Tell people,
> get consent where it's required, and follow your company's policy. MeetingScribe is provided as is, without
> warranty, is not an official tool of any company, and nothing here is legal advice. The installer shows these
> terms and won't install until you accept them (`./ctl.sh terms` shows them again).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `daemon: NOT running` | Mac: check `~/Library/Logs/MeetingScribe/stderr.log`. Windows: check the *MeetingScribe* task in Task Scheduler. Re-run the installer after moving the folder or recreating `.venv`. |
| Nothing logged after a reinstall (Mac) | A permission prompt (usually Desktop access) is waiting on screen. |
| `microphone delivers pure silence` | Mac: Microphone permission was denied. Windows: desktop apps' microphone access is off. |
| `system audio is pure silence` | Mac: System Audio Recording permission was denied. Windows: see if Teams' own speaker test is audible. |
| The remote side is silent | Set `capture_mode = "all"`. |
| Meeting not detected | Run `./ctl.sh status` during a call; `Teams mic in use` should be `True`. |
| Notes are called "Teams meeting" (Mac) | Grant the optional Screen Recording permission. |
| `DLL load failed ... Application Control policy` (Windows) | Smart App Control blocked a package; re-run `.\install`, which only installs ones it allows. |

## Repository layout

| Path | What it is |
|---|---|
| `meeting_scribe.py` | The program: background daemon, processing pipeline and CLI |
| `platforms/` | What differs per system: call detection, Teams audio capture, notifications, default models (`macos.py`, `windows.py`) |
| `config.toml` | Your settings |
| `install.sh`, `uninstall.sh`, `ctl.sh` | Mac: install, remove and control the background agent |
| `install.cmd`, `uninstall.cmd`, `ctl.cmd`, `windows/` | Windows: the same, as PowerShell scripts |
| `tests/` | Checks that the Windows stand-ins match the Mac's libraries (`python -m pytest tests`) |
| `site/` | The website at meetingscribe.preetbohra.com, with the demo video |
| `video/` | Source for the demo video: narration script, animation and build script |

<details>
<summary>Rebuilding the demo video</summary>

`video/build_video.py` speaks `video/script.json` with Kokoro-82M (an open, offline voice), renders
`video/motion.html` frame by frame and encodes `site/demo.mp4`. It runs on macOS (WebKit) or Windows (Chromium).

```bash
python3 -m venv video/.venv
video/.venv/bin/pip install imageio-ffmpeg kokoro-onnx pyobjc-framework-WebKit pyobjc-framework-Quartz  # macOS
video/.venv/bin/pip install imageio-ffmpeg kokoro-onnx playwright && video/.venv/bin/python -m playwright install chromium  # Windows
# once: kokoro-v1.0.onnx and voices-v1.0.bin from github.com/thewh1teagle/kokoro-onnx/releases into video/models/
video/.venv/bin/python video/build_video.py                   # about 10 minutes
video/.venv/bin/python video/build_video.py --stills 30,120   # a few PNG frames to check a change
```

People, meetings and ticket numbers in the video are made up.
</details>

## License

[GPL-3.0](LICENSE). Made by [Preet Bohra](https://preetbohra.com).
