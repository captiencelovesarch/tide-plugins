"""more visualizer tiles: the loudness river, the spectrogram, the art
halo and the beat tapper, as tiles any visualizer slot can show. they
were in tide's core until 3.0; tiles.py is that code."""
from __future__ import annotations

import weakref

from .tiles import HaloTile, RiverTile, SpectrogramTile, TapperTile

TILES = (
    ("river", "loudness river", RiverTile, ("river_view",)),
    ("spectrogram", "spectrogram", SpectrogramTile, ("spectrogram_history", "spectrogram_contrast")),
    ("halo", "art halo", HaloTile, ("halo_spokes", "halo_punch")),
    ("tapper", "beat tapper", TapperTile, ("tapper_target", "tapper_window", "tapper_offset")),
)


class VisTiles:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self._tiles: weakref.WeakSet = weakref.WeakSet()
        for kind, label, cls, knobs in TILES:
            ctx.ui.add_visualizer_tile(kind, label, self._factory(cls), knobs=knobs)
        # a knob changed on the store page: the tiles redraw with it
        ctx.settings_changed.connect(self._on_settings)
        ctx.set_status("four more tiles in each visualizer slot's menu")

    def _factory(self, cls):
        def make(engine):
            tile = cls(engine)
            self._tiles.add(tile)
            return tile
        return make

    def _on_settings(self, _keys) -> None:
        for tile in list(self._tiles):
            try:
                tile.settings_changed()
                tile.engine.wake()
            except RuntimeError:
                pass        # a tile qt already deleted

    def stop(self) -> None:
        pass    # ctx takes the tiles away; their slots keep the pick
