"""The visualizer: tide's beat in the headset, one of four ways (the
"visualizer" setting). It replaced the ring round the whole lens edge
(2026-11-04: "not that good"); the user liked all four mockups, so all
four are choices.

* watch: the watch on your wrist glows with the beat while it's up. every
  other mode does this too.
* floor: on each kick a ring of light spreads across the floor round your
  feet and fades. it stays where it started: it's in the room, not on
  your face.
* corner: the cover, small and low in a corner of your view (the side
  away from the watch), with a glow round it that beats. the title shows
  for a few seconds when the song changes. hidden while the panel is open.
* bottom: the old ring's glow and rim along the bottom of your view only
  (ring.draw with part="bottom").

Textures are drawn per song off the gui thread, numpy like ring.py; the
chip's cover and title need the theme's fonts and are drawn in panel.py.
The vr thread only places, scales and fades. Every flash goes through the
same FlashLimiter / Kicks caps as the ring did: three full flashes a
second.
"""
from __future__ import annotations

import math

import numpy as np

from . import geometry as geo
from . import ring as ringmod

MODES = ("off", "watch", "floor", "corner", "bottom")
# the overlays each mode uses. the watch glow is in all of them
LAYERS = {
    "off": (),
    "watch": ("watch_glow",),
    "floor": ("watch_glow", "ripple"),
    "corner": ("watch_glow", "chip", "chip_ring", "chip_title"),
    "bottom": ("watch_glow", "glow", "rim"),
}
TEX_PX = 512

# floor ripples
RIPPLES = 5                # overlays in the pool: three kicks a second, each lives RIPPLE_S
RIPPLE_S = 1.6
RIPPLE_FROM_M = 0.5        # across, as it starts...
RIPPLE_TO_M = 3.4          # ...and as it fades out
RIPPLE_EDGE = 0.9          # where the bright edge sits in the texture (1.0: its side)
FLOOR_LIFT_M = 0.004       # off the floor, so a game's own floor doesn't flicker through

# the corner chip, a metre out
CHIP_PX = 256
CHIP_M = 0.085
CHIP_DISTANCE_M = 1.0
CHIP_SIDE_DEG = 21.0       # out from the middle of your view...
CHIP_DROP_DEG = 16.0       # ...and down
CHIP_RADIUS = 40           # modern's corners, in texture px; brutalist is square
CHIP_GLOW_MARGIN = 0.38    # glow room round the cover, as a share of its width
CHIP_GLOW_REACH = 0.14
CHIP_FADE_S = 0.3
TITLE_PX = (560, 132)
TITLE_M = 0.26
TITLE_GAP_M = 0.02
TITLE_HOLD_S = 4.0

# the watch glow: a ring of light just outside the watch's disc
WATCH_GLOW = 1.75          # its width over the watch's
WATCH_DISC = 0.92          # the watch face's disc, as a share of its own width (panel.watch_face)
WATCH_GLOW_M = 0.08        # a starting width; the vr thread sizes it to the watch each tick


def ripple_keys() -> list[str]:
    return [f"ripple{i}" for i in range(RIPPLES)]


def chip_side(watch_hand: str) -> str:
    """the chip goes on the side away from the watch."""
    return "left" if watch_hand == "right" else "right"


# ---- drawing (any thread: numpy and QImage only) ----

def _image(rgba: np.ndarray):
    from PySide6.QtGui import QImage
    h, w = rgba.shape[:2]
    data = np.ascontiguousarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8))
    return QImage(data.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


def _polar(px: int) -> tuple[np.ndarray, np.ndarray]:
    """each pixel's radius (0 in the middle, 1 at the square's side) and
    angle (0 at the top)."""
    c = (np.arange(px, dtype=np.float32) + 0.5) / px * 2.0 - 1.0
    x, y = np.meshgrid(c, c)
    return np.hypot(x, y), np.arctan2(x, -y)


def _rgba(a: np.ndarray, ang: np.ndarray, colors: tuple, hard: bool, seed: int) -> np.ndarray:
    """two colours round the circle (the first at the top, like the
    ring), a hair of noise so 8-bit gradients don't band."""
    c1 = np.array(colors[0], dtype=np.float32)
    c2 = np.array(colors[0 if hard else 1], dtype=np.float32)
    mix = (0.5 - 0.5 * np.cos(ang))[..., None].astype(np.float32)
    rgb = c1 * (1.0 - mix) + c2 * mix
    rng = np.random.default_rng(seed)
    a = a.astype(np.float32)
    a = np.where(a > 0, np.clip(a + rng.uniform(-1.5 / 255, 1.5 / 255, a.shape), 0, 1), 0.0)
    return np.dstack([rgb, a[..., None]])


def floor_ring(colors: tuple, style: str = "modern", px: int = TEX_PX):
    """one ripple: a bright edge with a soft trail behind it, toward the
    middle, like a wave front. clear in the middle and outside."""
    r, ang = _polar(px)
    d = r - RIPPLE_EDGE
    if style == "brutalist":
        a = np.where(np.abs(d) < 0.028, 1.0, np.where((d < 0) & (d > -0.12), 0.3, 0.0))
    else:
        edge = np.exp(-(d / 0.022) ** 2)
        trail = np.where(d < 0, 0.4 * np.exp(np.minimum(d, 0.0) / 0.15), 0.0)
        a = np.maximum(edge, trail)
    a = np.where(r < 1.0, a, 0.0)
    return _image(_rgba(a, ang, colors, style == "brutalist", 17))


def disc_glow(colors: tuple, style: str = "modern", px: int = TEX_PX // 2):
    """the watch's glow: light outside its disc, nothing over it."""
    r, ang = _polar(px)
    inner = WATCH_DISC / WATCH_GLOW
    d = (r - inner) / (1.0 - inner)              # 0 at the watch's edge, 1 at the square's
    if style == "brutalist":
        a = np.clip(1.0 - d / 0.45, 0.0, 1.0) ** 0.8
    else:
        a = np.exp(-(np.maximum(d, 0.0) / 0.32) ** 1.3)
    a = np.where(d > 0, a, 0.0) * np.clip((1.0 - r) / 0.06, 0.0, 1.0)
    return _image(_rgba(a, ang, colors, style == "brutalist", 23))


def textures(mode: str, eyes: dict | None, colors: tuple, style: str) -> dict:
    """{layer: (QImage, width in metres)} for ``mode``, everything but the
    chip's cover and title. the bottom glow needs the lens outlines
    (``eyes``); without them it waits."""
    names = LAYERS.get(mode, ())
    out: dict = {}
    if "watch_glow" in names:
        out["watch_glow"] = (disc_glow(colors, style), WATCH_GLOW_M)
    if "ripple" in names:
        out["ripple"] = (floor_ring(colors, style), RIPPLE_FROM_M)
    if "chip_ring" in names:
        radius = CHIP_RADIUS if style == "modern" else 0
        img, ratio = ringmod.panel_glow((CHIP_PX, CHIP_PX), radius, colors, style,
                                        margin=CHIP_GLOW_MARGIN, reach=CHIP_GLOW_REACH,
                                        under=0.03)
        out["chip_ring"] = (img, CHIP_M * ratio)
    if "glow" in names and eyes is not None:
        (glow, rim), width = ringmod.draw(eyes, colors, style, part="bottom")
        out["glow"] = (glow, width)
        out["rim"] = (rim, width)
    return out


# ---- placing and timing (the vr thread) ----

def floor_pose(head: np.ndarray) -> np.ndarray:
    """flat on the floor under ``head`` (standing space: the floor is
    y = 0), seen from above."""
    a = np.eye(4)
    # its +z, the side an overlay is seen from, points up
    a[:3, :3] = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))
    a[:3, 3] = (float(head[0]), FLOOR_LIFT_M, float(head[2]))
    return a


def ripple_look(age: float) -> tuple[float, float] | None:
    """(width in metres, alpha 0..1) of a ripple ``age`` seconds old, or
    None once it's gone. it spreads fast then slows, and fades as it goes."""
    t = age / RIPPLE_S
    if not 0.0 <= t < 1.0:
        return None
    width = RIPPLE_FROM_M + (RIPPLE_TO_M - RIPPLE_FROM_M) * (1.0 - (1.0 - t) ** 2)
    alpha = (1.0 - t) ** 1.6 * min(1.0, age / 0.06)
    return width, alpha


def chip_pose(side: str, toward_middle_m: float = 0.0) -> np.ndarray:
    """where the chip sits relative to your head, turned to face you.
    ``toward_middle_m`` moves it that far toward the middle of your view
    (the title, beside the cover)."""
    sx = -1.0 if side == "left" else 1.0
    d = CHIP_DISTANCE_M
    pos = np.array([sx * d * math.tan(math.radians(CHIP_SIDE_DEG)) - sx * toward_middle_m,
                    -d * math.tan(math.radians(CHIP_DROP_DEG)), -d])
    return geo.facing(pos, np.zeros(3))


def title_pose(side: str) -> np.ndarray:
    return chip_pose(side, CHIP_M / 2 + TITLE_GAP_M + TITLE_M / 2)


class Fade:
    """0..1 toward on or off, in ``seconds`` either way."""

    def __init__(self, seconds: float = CHIP_FADE_S) -> None:
        self.seconds = seconds
        self.level = 0.0
        self._last: float | None = None

    def update(self, now: float, on: bool) -> float:
        dt = 0.0 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now
        step = dt / self.seconds if self.seconds > 0 else 1.0
        self.level = min(1.0, self.level + step) if on else max(0.0, self.level - step)
        return self.level
