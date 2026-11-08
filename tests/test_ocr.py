"""This system's real OCR reads a name tile. Runs on Windows and in the macOS CI job.

    python -m pytest tests
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402
import screen_names as sn  # noqa: E402


def tile_picture():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1200, 700), (32, 33, 36))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    draw.rectangle((40, 40, 580, 640), fill=(60, 64, 67), outline=(138, 180, 248), width=6)
    draw.rectangle((620, 40, 1160, 640), fill=(60, 64, 67))
    draw.text((60, 590), "Priya Shah", fill="white", font=font)
    draw.text((640, 590), "Tom Berg", fill="white", font=font)
    return np.asarray(img)


def test_real_ocr_finds_the_person_talking():
    pytest.importorskip("PIL")
    img = tile_picture()
    out = sn.read_snapshot(img, ms.plat.read_text(img), "Google Meet")
    assert out["active"] == "Priya Shah"
    assert out["roster"] == ["Priya Shah", "Tom Berg"]
