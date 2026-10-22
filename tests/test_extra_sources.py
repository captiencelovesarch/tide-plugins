"""extra sources under a real host: soundcloud, bandcamp and mixcloud
show up as sources (mixcloud starts off), an old switch is kept, and they
go when the plugin stops. No network: nothing is searched.

Run:  PYTHONPATH=~/tide/src python -m pytest tests/test_extra_sources.py
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PySide6.QtWidgets import QApplication

from tests._support import PLUGINS_DIR, FakePlayer, FakeQueue
from tide.plugin_host import PluginHost
from tide.settings import Settings
from tide.sources import registry

_app = QApplication.instance() or QApplication([])
SLUGS = ("soundcloud", "bandcamp", "mixcloud")


class ExtraSourcesTests(unittest.TestCase):
    def host(self, settings):
        tmp = Path(tempfile.mkdtemp(prefix="tide-extra-"))
        h = PluginHost(SimpleNamespace(_settings=settings), FakePlayer(), FakeQueue(),
                       roots=[(PLUGINS_DIR, "store")], state_file=tmp / "p.toml",
                       settings_dir=tmp / "s", stamp=tmp / "stamp", data_dir=tmp / "d")
        self.addCleanup(h.shutdown)
        patch = mock.patch("tide.settings.save_fields")
        patch.start()
        self.addCleanup(patch.stop)
        return h

    def test_the_three_sources(self):
        s = Settings()
        h = self.host(s)
        self.assertTrue(h.set_enabled("extra-sources", True), h.record("extra-sources").status)
        reg = registry()
        self.assertEqual([reg.get(x).name for x in SLUGS], ["soundcloud", "bandcamp", "mixcloud"])
        self.assertEqual([reg.is_enabled(x) for x in SLUGS], [True, True, False])
        self.assertEqual(s.sources_enabled["mixcloud"], False)
        h.set_enabled("extra-sources", False)
        self.assertEqual([reg.get(x) for x in SLUGS], [None, None, None])

    def test_switches_from_tide_2_are_kept(self):
        s = Settings()
        s.sources_enabled = {"ytmusic": True, "soundcloud": False, "bandcamp": False,
                             "mixcloud": False}
        h = self.host(s)
        h.set_enabled("extra-sources", True)
        self.assertFalse(any(registry().is_enabled(x) for x in SLUGS))
        self.assertEqual(h.record("extra-sources").detail,
                         "all three are switched off: turn them on in the source panel")


if __name__ == "__main__":
    unittest.main()
