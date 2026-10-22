"""tide powertools under a real host: its three buttons land in the
settings dialog's power tools and shortcuts sections, open their editor
a turn after the click, and go with the plugin.

Run:  PYTHONPATH=~/tide/src python -m pytest tests/test_powertools.py
"""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests._support import PLUGINS_DIR, FakePlayer, FakeQueue
from tide.plugin_host import PluginHost
from tide.settings import Settings

_app = QApplication.instance() or QApplication([])


class PowertoolsTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="tide-powertools-"))
        self.host = PluginHost(None, FakePlayer(), FakeQueue(), roots=[(PLUGINS_DIR, "store")],
                               state_file=tmp / "p.toml", settings_dir=tmp / "s",
                               stamp=tmp / "stamp", data_dir=tmp / "d")
        self.addCleanup(self.host.shutdown)
        self.assertTrue(self.host.set_enabled("powertools", True),
                        self.host.record("powertools").status)

    def dialog(self):
        from tide.ui.settings import SettingsDialog
        dlg = SettingsDialog(Settings())
        self.addCleanup(dlg.deleteLater)
        return dlg

    def editor(self, name):
        """the plugin's own copy of an editor module (what its buttons use)."""
        plugin = self.host.record("powertools").instance
        return importlib.import_module(type(plugin).__module__.rsplit(".", 1)[0] + "." + name)

    def test_the_buttons_and_what_they_open(self):
        dlg = self.dialog()
        self.assertEqual(set(dlg.plugin_buttons), {"theme editor", "glyph editor", "keymap editor"})
        keymap_editor = self.editor("keymap_editor")
        with mock.patch.object(keymap_editor, "open_keymap_editor") as opener:
            dlg.plugin_buttons["keymap editor"].click()
            self.assertEqual(opener.call_count, 0, "opened inside the click")
            QTest.qWait(30)
            opener.assert_called_once_with(dlg, dlg._settings)

    def test_they_go_with_the_plugin(self):
        self.host.set_enabled("powertools", False)
        self.assertEqual(self.dialog().plugin_buttons, {})

    def test_the_keymap_editor_takes_the_scale(self):
        from tide.ui import scale
        keymap_editor = self.editor("keymap_editor")
        scale.set_factor(scale.Scale.HUGE)
        self.addCleanup(scale.set_factor, scale.Scale.NORMAL)
        dlg = keymap_editor.KeymapEditor(Settings())
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.minimumWidth(), scale.px(560))
        self.assertEqual(dlg.width(), scale.px(600))


if __name__ == "__main__":
    unittest.main()
