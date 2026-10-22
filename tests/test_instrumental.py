"""instrumental finder: the scoring on its own (the real instrumental
wins over a karaoke channel's cover and a different song), and the
plugin under a real host: the toggle swaps in the instrumental at the
same spot, swaps the vocal back, and lets go when you pick another song.
No network: search_all and the window are fakes.

Run:  PYTHONPATH=~/tide/src python -m pytest tests/test_instrumental.py
"""
import importlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from tests._support import PLUGINS_DIR, FakePlayer, FakeQueue
from tide.api import Track
from tide.plugin_api import track_info
from tide.plugin_host import PluginHost
from tide.ui import lyrics as lyrics_ui

_app = QApplication.instance() or QApplication([])

_pkg = types.ModuleType("tide_plugins_test_instrumental")
_pkg.__path__ = [str(PLUGINS_DIR / "instrumental")]
sys.modules["tide_plugins_test_instrumental"] = _pkg
finder = importlib.import_module("tide_plugins_test_instrumental.finder")


def _t(vid, title, artists, secs=200, source="ytmusic"):
    return track_info(Track(vid, title, artists, duration_seconds=secs, source=source))


VOCAL = _t("v1", "Let Down", "Radiohead", 299)
CANDIDATES = [
    ("youtube music", _t("k1", "Let Down (Karaoke Version)", "Sing King", 300)),
    ("youtube music", _t("x1", "Paranoid Android (Instrumental)", "Radiohead", 383)),
    ("soundcloud", _t("i1", "Let Down (Instrumental)", "Radiohead", 299, "soundcloud")),
]


def _search(q, limit):
    return list(CANDIDATES)


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FinderTests(unittest.TestCase):
    def test_the_real_instrumental_wins(self):
        match = finder.find_instrumental(VOCAL, _search)
        self.assertEqual((match.track.id, match.source_name), ("i1", "soundcloud"))

    def test_nothing_close_enough(self):
        self.assertIsNone(finder.find_instrumental(VOCAL, lambda q, n: CANDIDATES[:2]))


class FakeWindow:
    def __init__(self, queue):
        self.queue = queue
        self.played = []

    def _play_track_then_seek(self, track, seconds):
        self.played.append((track.video_id, seconds))
        self.queue.current = track
        self.queue.current_changed.emit(track)


class PluginTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="tide-inst-"))
        self.player, self.queue = FakePlayer(), FakeQueue()
        self.window = FakeWindow(self.queue)
        self.host = PluginHost(self.window, self.player, self.queue,
                               roots=[(PLUGINS_DIR, "store")], state_file=tmp / "p.toml",
                               settings_dir=tmp / "s", stamp=tmp / "stamp", data_dir=tmp / "d")
        self.addCleanup(self.host.shutdown)
        self.assertTrue(self.host.set_enabled("instrumental", True))
        self.ctx = self.host.record("instrumental").ctx
        self.ctx.music.search_all = _search            # no network
        self.vocal = Track("v1", "Let Down", "Radiohead", duration_seconds=299)
        self.queue.current = self.vocal
        self.queue.current_changed.emit(self.vocal)
        self.player.position_changed.emit(42.0)
        self.toggle = lyrics_ui.PLUGIN_TOGGLES[-1]

    def click(self, on):
        self.toggle.fn(on, track_info(self.queue.current))

    def test_swap_in_and_back(self):
        self.assertEqual(self.toggle.label, "mute lyrics")
        self.click(True)
        self.assertTrue(_wait(lambda: self.window.played))
        self.assertEqual(self.window.played, [("i1", 42.0)])
        self.assertEqual(self.toggle.status, "playing the instrumental, from soundcloud")
        self.player.position_changed.emit(80.0)
        self.click(False)
        self.assertEqual(self.window.played[-1], ("v1", 80.0))
        self.assertFalse(self.toggle.checked)

    def test_picking_another_song_lets_go(self):
        self.click(True)
        self.assertTrue(_wait(lambda: self.window.played))
        other = Track("o1", "Airbag", "Radiohead")
        self.queue.current_changed.emit(other)
        self.assertFalse(self.toggle.checked)
        self.assertEqual(self.toggle.status, "")

    def test_the_toggle_goes_with_the_plugin(self):
        n = len(lyrics_ui.PLUGIN_TOGGLES)
        self.host.set_enabled("instrumental", False)
        self.assertEqual(len(lyrics_ui.PLUGIN_TOGGLES), n - 1)


if __name__ == "__main__":
    unittest.main()
