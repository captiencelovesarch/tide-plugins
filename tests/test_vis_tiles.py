"""more visualizer tiles: the loudness river, spectrogram, art halo and
beat tapper, moved here from tide's tests/test_visualizer_deck.py with the
tiles (tide 3.0). The plugin runs under a real host so the visualizer's
slots can show its tiles; tiles built on their own read their knobs from
a plain dict.

Run:  PYTHONPATH=~/tide/src python -m pytest tests/test_vis_tiles.py
"""
import importlib
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from tests._support import PLUGINS_DIR, FakePlayer, FakeQueue
from tide import settings as settings_module
from tide.beat_map import Beat, BeatMap, Hit
from tide.plugin_host import PluginHost
from tide.ui.ambient import BeatClock
from tide.ui.vis_deck import MapView, VisEngine
from tide.ui.vis_play import LAYOUTS, MissingTile
from tide.ui.visualizer import VisualizerView

_pkg = types.ModuleType("tide_plugins_test_vis_tiles")
_pkg.__path__ = [str(PLUGINS_DIR / "vis-tiles")]
sys.modules["tide_plugins_test_vis_tiles"] = _pkg
tiles_mod = importlib.import_module("tide_plugins_test_vis_tiles.tiles")
HaloTile, RiverTile = tiles_mod.HaloTile, tiles_mod.RiverTile
SpectrogramTile, TapperTile = tiles_mod.SpectrogramTile, tiles_mod.TapperTile


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication(sys.argv[:1])


_HOST = []


@pytest.fixture(autouse=True)
def plugin_on():
    """the plugin running, so the registry has its tiles."""
    tmp = Path(tempfile.mkdtemp(prefix="tide-vis-tiles-"))
    h = PluginHost(None, FakePlayer(), FakeQueue(), roots=[(PLUGINS_DIR, "store")],
                   state_file=tmp / "p.toml", settings_dir=tmp / "s", stamp=tmp / "stamp",
                   data_dir=tmp / "d")
    assert h.set_enabled("vis-tiles", True), h.record("vis-tiles").status
    _HOST.append(h)
    yield h
    h.shutdown()
    _HOST.remove(h)


def plugin_rec():
    return _HOST[-1].record("vis-tiles")


def render(widget, w=900, h=560):
    widget.resize(w, h)
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#101010"))
    widget.render(img, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return img


PERIOD = 0.5


def song(duration=40.0, complete=True, confidence=0.9, hits=()):
    beats = tuple(Beat(i * PERIOD, 1.0 if i % 4 == 0 else 0.6, i % 4, PERIOD, True)
                  for i in range(int(duration / PERIOD)))
    swell = tuple(min(1.0, i / 80.0) for i in range(int(duration * 4) + 2))
    return BeatMap(duration, complete, 120.0, confidence, beats, tuple(hits), swell)


def render(widget, w=900, h=560):
    widget.resize(w, h)
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor("#101010"))
    widget.render(img, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return img


def _loud_song(duration=40.0):
    base = song(duration)
    n = int(duration * 16) + 1
    loud = tuple(min(1.0, 0.2 + 0.7 * (i / n)) for i in range(n))     # a long build
    return BeatMap(base.duration, base.complete, base.tempo, base.confidence,
                   base.beats, base.hits, base.swell, base.swell_rate, loud, 16.0)


class _Player:
    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class S(QObject):
            state_changed = Signal(object)
            position_changed = Signal(float)
            duration_changed = Signal(float)
        self.sig = S()
        self.state_changed = self.sig.state_changed
        self.position_changed = self.sig.position_changed
        self.duration_changed = self.sig.duration_changed
        self.state = PlayState.PLAYING
        self.seeks = []

    def seek(self, seconds):
        self.seeks.append(seconds)


class _Ambient:
    def __init__(self, clock=None):
        self.clock = clock
        self.watching = []

    def set_watching(self, name, on):
        self.watching.append((name, on))

    def beat_clock(self):
        return self.clock


def _settings(**kw):
    s = settings_module.Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _settings(**kw):
    s = settings_module.Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _view(layout="quad", tiles=("spectrum", "deck", "lyrics", "halo"), clock=None, **kw):
    s = _settings(visualizer_layout=layout, visualizer_tile_1=tiles[0],
                  visualizer_tile_2=tiles[1], visualizer_tile_3=tiles[2],
                  visualizer_tile_4=tiles[3], **kw)
    amb = _Ambient(clock)
    v = VisualizerView()
    v.attach(settings=lambda: s, ambient=lambda: amb)
    v.resize(1000, 640)
    return v, s, amb


def _feed(e, playing=True):
    e.set_now_playing(SimpleNamespace(title="like him", artists="tyler, the creator"))
    e.set_duration(40.0)
    e.set_playing(playing)
    e.update_bands(np.linspace(0.9, 0.1, 32))
    e.update_waveform(np.sin(np.linspace(0, 30, 1024)).astype(np.float32))
    e.update_pulse(0.9)


def _engine(clock=None, **kw):
    e = VisEngine()
    e.apply_settings(_settings(**kw))
    e.set_clock_provider(lambda: clock[0] if isinstance(clock, list) else clock)
    return e


TILE_SETS = (("deck", "lyrics", "river", "spectrogram"),
             ("tapper", "halo", "spectrum", "deck"))


@pytest.mark.parametrize("kinds", TILE_SETS)
@pytest.mark.parametrize("look", ("blocks", "glow"))
def test_every_tile_paints_in_both_looks(kinds, look, monkeypatch):
    monkeypatch.setattr(settings_module, "save_fields", lambda *a, **k: None)
    clock = [BeatClock(song(hits=(Hit(4.25, 0.9),)), 3.9, 1.0)]
    v, s, amb = _view(tiles=kinds, clock=None, visualizer_look=look)
    amb.clock = clock[0]
    v.show()
    e = v.engine
    _feed(e)
    assert [t.kind for t in v.tiles()] == list(kinds)
    for flash in ("off", "soft", "hard"):
        s.visualizer_flash = flash
        v.apply_settings(s)
        for k in range(6):
            amb.clock = BeatClock(clock[0].beat_map, 3.9 + k * 0.05, 1.0)
            e.tick()
        render(v)
    assert e.look == look
    v.teardown()


def test_tiles_paint_without_a_map_with_a_partial_map_and_tiny():
    v, s, amb = _view(tiles=("spectrum", "river", "spectrogram", "halo"))
    v.show()
    e = v.engine
    _feed(e)
    e.tick()
    render(v)
    amb.clock = BeatClock(song(duration=12.0, complete=False), 10.0, 1.0)
    e.tick()
    render(v)
    render(v, 200, 160)
    v.teardown()


def test_layouts_keep_a_tile_whose_kind_stays():
    v, s, _amb = _view(layout="stack", tiles=("spectrum", "tapper", "halo", "river"))
    assert [t.kind for t in v.tiles()] == ["spectrum", "tapper"]
    tapper = v.tiles()[1]
    s.visualizer_layout = "quad"
    v.apply_settings(s)
    tiles = v.tiles()
    assert [t.kind for t in tiles] == ["spectrum", "tapper", "halo", "river"]
    assert tiles[1] is tapper                   # the streak survives
    assert [t.caption_here for t in tiles] == [True, False, False, False]
    s.visualizer_layout = "single"
    v.apply_settings(s)
    assert len(v.tiles()) == 1 and v.tiles()[0].solo
    for name, slots in LAYOUTS.items():
        s.visualizer_layout = name
        v.apply_settings(s)
        assert len(v.tiles()) == len(slots)
    v.teardown()


def test_the_tiles_go_with_the_plugin_and_come_back():
    v, s, _amb = _view(tiles=("river", "halo", "spectrogram", "tapper"))
    assert not any(isinstance(t, MissingTile) for t in v.tiles())
    _HOST[-1].set_enabled("vis-tiles", False)
    assert all(isinstance(t, MissingTile) for t in v.tiles())
    assert s.visualizer_tile_1 == "river"          # the pick stays
    _HOST[-1].set_enabled("vis-tiles", True)
    assert [t.kind for t in v.tiles()] == ["river", "halo", "spectrogram", "tapper"]
    assert not any(isinstance(t, MissingTile) for t in v.tiles())
    v.teardown()


def test_river_draws_the_song_around_the_playhead():
    e = _engine()
    knobs = {"river_view": "near"}
    e.mv = MapView(_loud_song())
    e.position = 20.0
    e.playing = True
    river = RiverTile(e)
    river.plugin_settings = knobs
    lo, hi, head = river._window(e.mv)
    assert (lo, hi, head) == pytest.approx((20.0 - 16.0 * 0.3, 20.0 + 16.0 * 0.7, 0.3))
    knobs["river_view"] = "whole"
    e.mv = MapView(_loud_song())
    e.position = 20.0
    e.duration = 40.0
    lo, hi, head = river._window(e.mv)
    assert (lo, hi, head) == pytest.approx((0.0, 40.0, 0.5))


def test_spectrogram_records_history_and_marks_the_beats():
    e = _engine(visualizer_look="glow")
    e.playing = True
    e.spec.level[:] = np.linspace(1.0, 0.0, e.spec.n)        # loud bass
    spec = SpectrogramTile(e)
    spec.plugin_settings = {"spectrogram_history": 6, "spectrogram_contrast": "normal"}
    spec.resize(400, 300)
    spec._ensure()
    assert spec._hist.shape == (6 * 30, SpectrogramTile.ROWS)
    e.kicks_now = [1.0]
    spec.on_frame(0.0, 0.5)                       # half a second: 15 columns
    written = np.nonzero(spec._hist.max(axis=1))[0]
    assert len(written) == 15
    assert spec._hist[written[0], 0] > spec._hist[written[0], -1]   # bass row first
    assert np.count_nonzero(spec._beats) == 1
    e.playing = False
    head = spec._head
    spec.on_frame(0.0, 1.0)
    assert spec._head == head                     # paused: time stands still
    lut = spec._ramp()
    assert lut.shape == (256, 4) and lut[0, 3] == 0 and lut[-1, 3] == 255


def test_halo_paints_the_cover():
    from PySide6.QtGui import QColor as C
    art = QImage(64, 64, QImage.Format_RGB32)
    art.fill(C("#d03a2a"))
    e = _engine(visualizer_look="glow")
    e.set_now_playing(SimpleNamespace(title="t", artists="a", thumbnail="https://x/cover.jpg"))
    e._art_asked = True                           # no network in tests
    e.set_art("https://x/cover.jpg", art)
    halo = HaloTile(e)
    halo.plugin_settings = {"halo_spokes": 96, "halo_punch": "gentle"}
    halo.resize(400, 300)
    halo.show()
    render(halo)
    assert halo._cover is not None and halo._cover.width() > 10
    halo.hide()


def _tapper(**knobs):
    e = _engine()
    e.mv = MapView(song())
    e.playing = True
    e.speed = 1.0
    t = TapperTile(e)
    t.plugin_settings = {"tapper_target": "beats", "tapper_window": "normal", "tapper_offset": 0,
                         **knobs}
    t.resize(500, 300)
    return e, t


def test_tapper_grades_taps_against_the_beat_map():
    e, t = _tapper()
    e.position = 2.0
    assert t.tap() == "perfect"
    e.position = 2.56                            # 60 ms late
    assert t.tap() == "good"
    e.position = 3.2
    assert t.tap() == "miss"                     # 200 ms off either beat
    assert (t.streak, t.best, t.taps) == (0, 2, 3)
    e.position = 3.5
    t.tap()
    e.position = 3.51
    assert t.tap() == "miss"                     # the same beat twice


def test_tapper_offset_and_targets_follow_the_knobs():
    e, t = _tapper(tapper_offset=60)
    e.position = 2.06                            # 60 ms late, compensated
    assert t.tap() == "perfect"
    e2, t2 = _tapper(tapper_target="downbeats")
    e2.position = 2.5                            # a beat, not a downbeat
    assert t2.tap() == "miss"
    e2.position = 4.0
    assert t2.tap() == "perfect"


def test_tapper_counts_beats_you_let_pass_while_playing_along():
    e, t = _tapper()
    e.position = 2.0
    t.tap()
    e.position = 2.0
    t.on_frame(time.monotonic(), 0.016)
    e.position = 3.3
    t.on_frame(time.monotonic(), 0.016)
    assert t.streak == 0
    assert list(t.history)[-2:] == ["miss", "miss"]     # 2.5 and 3.0


def test_tapper_waits_without_a_map():
    e = _engine()
    e.playing = True
    t = TapperTile(e)
    t.plugin_settings = {"tapper_target": "beats", "tapper_window": "normal", "tapper_offset": 0}
    assert t.tap() is None and t.last[0] == "waiting"


def test_menus_build_and_write_the_settings(monkeypatch):
    saved = []
    monkeypatch.setattr(settings_module, "save_fields",
                        lambda s, *names: saved.extend(names))
    v, s, _amb = _view()
    menu = v._build_style_menu()
    tile_menu = v._build_tile_menu(3)
    from PySide6.QtGui import QAction
    actions = {a.text(): a for a in tile_menu.findChildren(QAction)}
    actions["spectrogram"].trigger()
    assert s.visualizer_tile_4 == "spectrogram" and "visualizer_tile_4" in saved
    assert v.tiles()[3].kind == "spectrogram"
    knob_menu = v._build_tile_menu(3)
    knob_actions = {a.text(): a for a in knob_menu.findChildren(QAction)}
    assert "24 seconds" in knob_actions           # the spectrogram's history knob
    knob_actions["24 seconds"].trigger()
    assert plugin_rec().settings["spectrogram_history"] == 24   # the plugin's own setting
    menu.deleteLater()
    tile_menu.deleteLater()
    knob_menu.deleteLater()
    v.teardown()


def test_the_tappers_menu_resets_its_score(monkeypatch):
    monkeypatch.setattr(settings_module, "save_fields", lambda *a, **k: None)
    v, s, _amb = _view(tiles=("spectrum", "deck", "lyrics", "tapper"))
    from PySide6.QtGui import QAction
    menu = v._build_tile_menu(3)
    actions = {a.text(): a for a in menu.findChildren(QAction)}
    tapper = v.tiles()[3]
    tapper.streak = 7
    actions["reset score"].trigger()
    assert tapper.streak == 0
    menu.deleteLater()
    v.teardown()

