"""local files under a real host, on a temp music folder: the files are
indexed and searchable, a cover.jpg becomes every song's art, unchanged
files aren't read twice, and the source goes when the plugin stops.

Run:  PYTHONPATH=~/tide/src python -m pytest tests/test_local_files.py
"""
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from tests._support import PLUGINS_DIR, FakePlayer, FakeQueue
from tide.plugin_host import PluginHost
from tide.settings import Settings
from tide.sources import registry

_app = QApplication.instance() or QApplication([])


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000)


class LocalFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tide-local-"))
        self.music = self.tmp / "music" / "an album"
        self.music.mkdir(parents=True)
        for name in ("first song", "second song"):
            _wav(self.music / f"{name}.wav")
        img = QImage(8, 8, QImage.Format_RGB32)
        img.fill(QColor("#336699"))
        img.save(str(self.music / "cover.jpg"))
        self.settings_dir = self.tmp / "s"
        self.settings_dir.mkdir()
        (self.settings_dir / "local-files.toml").write_text(
            f'folder = "{self.tmp / "music"}"\nwatch = false\n')
        self.host = PluginHost(SimpleNamespace(_settings=Settings()), FakePlayer(), FakeQueue(),
                               roots=[(PLUGINS_DIR, "store")], state_file=self.tmp / "p.toml",
                               settings_dir=self.settings_dir, stamp=self.tmp / "stamp",
                               data_dir=self.tmp / "d")
        self.addCleanup(self.host.shutdown)
        patch = mock.patch("tide.settings.save_fields")
        patch.start()
        self.addCleanup(patch.stop)
        # the plugin's own cache folder (the index) stays in the temp tree
        cache = mock.patch("tide.config.PLUGIN_CACHE_DIR", self.tmp / "cache")
        cache.start()
        self.addCleanup(cache.stop)
        art = mock.patch("tide.config.ART_CACHE_DIR", self.tmp / "art")
        art.start()
        self.addCleanup(art.stop)

    def start(self):
        self.assertTrue(self.host.set_enabled("local-files", True),
                        self.host.record("local-files").status)
        rec = self.host.record("local-files")
        self.assertTrue(_wait(lambda: rec.detail.startswith("2 songs")), rec.detail)
        return rec

    def test_indexes_and_finds(self):
        self.start()
        src = registry().get("local")
        found = src.search_songs("second")
        self.assertEqual([t.title for t in found], ["second song"])
        self.assertTrue(found[0].thumbnail.startswith("tide-art:"), found[0].thumbnail)
        self.assertTrue(any((self.tmp / "art").iterdir()), "the cover is in the art cache")
        albums = [p.title for p in src.get_library_playlists()]
        self.assertIn("all tracks", albums)

    def test_unchanged_files_arent_read_again(self):
        self.start()
        src = registry().get("local")
        mod = __import__(type(src).__module__, fromlist=["_read_tags"])
        with mock.patch.object(mod, "_read_tags", side_effect=AssertionError("read again")):
            self.assertEqual(src.rescan(), 2)

    def test_goes_with_the_plugin(self):
        self.start()
        self.host.set_enabled("local-files", False)
        self.assertIsNone(registry().get("local"))


if __name__ == "__main__":
    unittest.main()
