"""Shared bits for the plugin tests. They run against a tide checkout:

    PYTHONPATH=~/tide/src python -m pytest tests

PLUGINS_DIR is this repo's root, which holds one folder per plugin, the
same shape as tide's store folder (<data>/plugins/installed/).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

PLUGINS_DIR = Path(__file__).resolve().parent.parent


class FakePlayer(QObject):
    state_changed = Signal(object)
    position_changed = Signal(float)
    duration_changed = Signal(float)
    speed_changed = Signal(float)
    ended = Signal()

    def __init__(self):
        super().__init__()
        self.state = None
        self.duration = 0.0
        self.seeks = []

    def seek(self, s):
        self.seeks.append(s)


class FakeQueue(QObject):
    current_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.current = None

    def tracks(self):
        return []

    def current_index(self):
        return -1
