# PyInstaller recipe for the desktop app, on both systems: pyinstaller packaging/MeetingScribe.spec
# A folder build rather than one file: nothing unpacks to a temp folder at each start, which is
# slower and is what security software objects to.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = (ROOT / "packaging" / "VERSION").read_text().strip()
MAC = sys.platform == "darwin"

datas = [(str(ROOT / "config.toml"), "."), (str(ROOT / "ui"), "ui")]
binaries, hiddenimports = [], ["screen_names", *collect_submodules("keyring.backends")]

packages = ["onnxruntime", "webview", "huggingface_hub"]
packages += ["mlx", "mlx_whisper", "mlx_lm", "transformers", "tokenizers"] if MAC else [
    "faster_whisper", "ctranslate2", "llama_cpp", "av", "comtypes", "pythonnet", "clr_loader", "pystray"]
for package in packages:
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["platforms.macos", "Quartz", "Vision", "AppKit", "Foundation", "CoreMedia", "ScreenCaptureKit"] if MAC else [
    "platforms.windows", "PIL.Image", "PIL.ImageDraw"]

a = Analysis([str(ROOT / "meetingscribe_app.py")], pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=["torch", "torchaudio", "scipy", "speechbrain", "tkinter", "matplotlib", "pytest"])
pyz = PYZ(a.pure)
icon = ROOT / "packaging" / ("MeetingScribe.icns" if MAC else "MeetingScribe.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MeetingScribe", console=False, icon=str(icon),
          target_arch="arm64" if MAC else None)
folder = COLLECT(exe, a.binaries, a.datas, name="MeetingScribe")
if MAC:
    use = "MeetingScribe records {} of your meetings and transcribes them on this Mac."
    app = BUNDLE(folder, name="MeetingScribe.app", icon=str(icon), bundle_identifier="local.meetingscribe",
                 version=VERSION, info_plist={
                     "CFBundleShortVersionString": VERSION, "LSMinimumSystemVersion": "14.4",
                     "NSMicrophoneUsageDescription": use.format("your side"),
                     "NSAudioCaptureUsageDescription": use.format("the other side"),
                     "NSAppleEventsUsageDescription": "MeetingScribe shows a notification when it starts and stops recording.",
                     "NSHighResolutionCapable": True})
