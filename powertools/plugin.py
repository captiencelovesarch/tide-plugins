"""tide powertools: the theme editor, the glyph editor and the keymap
editor, as buttons in tide's settings dialog. they were in tide's core
until 3.0. what they edit stays in core (your themes, glyph swaps and
keymap keep working with this plugin gone); this is only the editing.

first-party and shipped in step with tide, so the editors use tide's own
widgets and settings directly, not just tide.plugin_api."""
from __future__ import annotations


class Powertools:
    def start(self, ctx) -> None:
        ui = ctx.ui
        ui.add_settings_button(
            "power tools", "theme editor", self._theme,
            tooltip="edit the active theme's colors, typography and radius live; "
                    "save the result as your own theme")
        ui.add_settings_button(
            "power tools", "glyph editor", self._glyphs,
            tooltip="swap any transport glyph (▶ ▮▮ ♥ …) for 1-3 characters of "
                    "your own, per personality")
        ui.add_settings_button("shortcuts", "keymap editor", self._keymap)
        ctx.set_status("the editors are in settings: appearance and shortcuts")

    def _theme(self, dialog) -> None:
        from .theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(parent=dialog)
        saved = getattr(dialog, "_on_theme_saved", None)
        if callable(saved):
            dlg.theme_saved.connect(saved)
        dlg.exec()
        dlg.deleteLater()

    def _glyphs(self, dialog) -> None:
        # parented to the settings dialog, so its refresh walk reaches the
        # window behind it
        from .glyph_editor import open_glyph_editor
        open_glyph_editor(dialog._settings, dialog)

    def _keymap(self, dialog) -> None:
        from .keymap_editor import open_keymap_editor
        open_keymap_editor(dialog, dialog._settings)

    def stop(self) -> None:
        pass    # ctx takes the buttons away
