"""Draws the app icon (site/icon.svg's shape) as MeetingScribe.ico and MeetingScribe.icns."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
SCALE = 16  # 64-unit icon, drawn large
img = Image.new("RGBA", (64 * SCALE, 64 * SCALE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.rounded_rectangle((0, 0, 64 * SCALE - 1, 64 * SCALE - 1), 15 * SCALE, fill=(229, 72, 58, 255))
for x, y1, y2 in ((16, 28, 36), (24, 22, 42), (32, 15, 49), (40, 24, 40), (48, 29, 35)):
    draw.rounded_rectangle(((x - 2.5) * SCALE, (y1 - 2.5) * SCALE, (x + 2.5) * SCALE, (y2 + 2.5) * SCALE),
                           2.5 * SCALE, fill="white")
img = img.resize((1024, 1024), Image.LANCZOS)
img.save(HERE / "MeetingScribe.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(HERE / "MeetingScribe.icns")
