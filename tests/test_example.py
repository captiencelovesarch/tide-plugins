"""the example plugin the README walks through starts, writes the song, and
follows its settings."""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from tests._support import PLUGINS_DIR, FakePlayer, FakeQueue

EXAMPLES = PLUGINS_DIR / "examples"


class NowPlayingFileTests(unittest.TestCase):
    def setUp(self):
        from tide.plugin_host import PluginHost
        tmp = Path(tempfile.mkdtemp(prefix="tide-example-"))
        self.player, self.queue = FakePlayer(), FakeQueue()
        self.queue.current = types.SimpleNamespace(
            video_id="abc", title="Noid", artists="Tyler, The Creator", album="CHROMAKOPIA",
            duration_seconds=285, thumbnail="", source="ytmusic")
        self.player.state = "playing"
        self.host = PluginHost(None, self.player, self.queue, roots=[(EXAMPLES, "yours")],
                               state_file=tmp / "p.toml", settings_dir=tmp / "s",
                               stamp=tmp / "stamp", data_dir=tmp / "d")
        rec = self.host.record("now-playing-file")
        self.assertIsNotNone(rec)
        # yours: off until switched on
        self.assertFalse(self.host.is_enabled("now-playing-file"))
        self.assertTrue(self.host.set_enabled("now-playing-file", True), rec.status)
        self.rec = rec
        self.path = rec.instance.path

    def tearDown(self):
        self.host.shutdown()

    def test_writes_the_song(self):
        self.assertEqual(self.path.read_text(), "Tyler, The Creator - Noid\n")
        self.assertIn("writing", self.rec.detail)

    def test_follows_the_format(self):
        self.rec.settings.set("format", "{title} ({album})")
        self.host.apply_settings("now-playing-file", {"format"})
        self.assertEqual(self.path.read_text(), "Noid (CHROMAKOPIA)\n")

    def test_paused_clears_when_asked(self):
        self.rec.settings.set("when_paused", "clear")
        self.player.state = "paused"
        self.player.state_changed.emit("paused")
        self.assertEqual(self.path.read_text(), "")
        self.assertEqual(self.rec.detail, "nothing playing")

    def test_switching_off_empties_the_file(self):
        self.host.set_enabled("now-playing-file", False)
        self.assertEqual(self.path.read_text(), "")
        self.assertIsNone(self.rec.instance)
