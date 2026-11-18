import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402


class Banner:
    """Stands in for the banner process, so that no test opens a window."""
    started = []

    def __init__(self):
        self.returncode = None
        Banner.started.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


@pytest.fixture(autouse=True)
def no_banner_window(monkeypatch):
    Banner.started = []
    monkeypatch.setattr(ms.Daemon, "spawn_banner", lambda self: Banner())
    return Banner
