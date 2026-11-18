"""The light show: a thread of its own, 60 frames a second, so a slow
device or a dropped wifi packet never holds up tide.

Each frame takes the highest pulse since the last one (like tide's
backdrop), shapes it with tide.plugin_api's flash cap and envelope, and
paints every device: the cover's two colours as a slow-moving wash laid
out by where each led sits (layout.py), at the beat's brightness. A kick
lands in the middle of each device and rolls outward: every led plays
the beat back a little later and a little softer the further out it
is. A device that fails is retried every few seconds; the others carry
on.
"""
from __future__ import annotations

import colorsys
import math
import threading
import time
from collections import deque
from typing import Callable

from tide.plugin_api import Envelope, FlashLimiter, STRENGTH

from . import layout

FPS = 60
RETRY_S = 5.0
DRIFT_S = 24.0             # one trip of the gradient along the leds
REST = 0.18                # brightness while paused, with "glow dimly"
# a kick takes this long to roll from the middle of a device to its far
# edge, and has this much less left when it gets there. the beat's own
# release is 0.09 to 0.26 s, so the middle is already falling while the
# edge rises: it reads as a ring going out, not a blur.
TRAVEL_S = 0.28
FADE = 0.35
_HISTORY = int(TRAVEL_S * FPS) + 2


def brightness(mode: str, pulse: float) -> float:
    """how bright the lights are for a pulse, before strength. a room
    needs more range than a glow at the edge of your view."""
    pulse = max(0.0, min(1.0, pulse))
    if mode == "breathing":
        return 0.35 + 0.65 * pulse
    return 0.12 + 0.88 * pulse


def led_color(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """a cover colour made into one an rgb led can show. leds can't do
    pastel: a soft colour is mostly white light and comes out washed out
    (2026-10-21: igor's pink came out a weird salmon). so the hue stays and
    the colour goes to full strength; a pastel pink leans toward magenta,
    which is how leds make pink."""
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    if s < 0.08:
        return (1.0, 1.0, 1.0)            # a grey cover: white light
    deg = h * 360.0
    if s < 0.55 and v > 0.6 and (deg >= 330.0 or deg < 12.0):
        # pastel pink / rose: full strength at its own hue reads as red
        deg = 328.0 if deg >= 330.0 or deg < 12.0 else deg
    return colorsys.hsv_to_rgb((deg / 360.0) % 1.0, max(s, 0.9), 1.0)


def _mix_hue(a: tuple, b: tuple, t: float) -> tuple[float, float, float]:
    """between two colours the short way round the colour wheel, at full
    strength: red to blue passes through purple, not through grey."""
    ha, sa, va = colorsys.rgb_to_hsv(*a)
    hb, sb, vb = colorsys.rgb_to_hsv(*b)
    if sa < 0.08 or sb < 0.08:          # white has no hue to travel along
        return tuple(x + (y - x) * t for x, y in zip(a, b))
    d = ((hb - ha + 0.5) % 1.0) - 0.5
    return colorsys.hsv_to_rgb((ha + d * t) % 1.0, sa + (sb - sa) * t, va + (vb - va) * t)


def gradient(n: int, colors: tuple, level: float, phase: float) -> list[tuple[int, int, int]]:
    """``n`` leds from colour a to b and back, shifted by ``phase`` (0..1),
    at ``level`` (0..1)."""
    places = [(i / max(1, n), 0.0) for i in range(n)]
    return paint(places, colors, lambda _reach: level, level, phase)


def paint(places: list, colors: tuple, level_at: Callable[[float], float],
          base: float, phase: float) -> list[tuple[int, int, int]]:
    """one device's frame. ``places``: (along, reach) per led (layout.py).
    the wash goes from colour a to b and back along ``along``, shifted by
    ``phase``. ``level_at(reach)`` is the beat as far out as ``reach``,
    and what it lifts above ``base`` (the resting glow) fades with reach."""
    a, b = colors
    out = []
    for along, reach in places:
        level = level_at(reach)
        if level > base:
            level = base + (level - base) * (1.0 - FADE * reach)
        level = max(0.0, min(1.0, level))
        x = (along + phase) % 1.0
        t = 0.5 - 0.5 * math.cos(2 * math.pi * x)
        r, g, bl = _mix_hue(a, b, t)
        out.append((int(255 * level * r), int(255 * level * g), int(255 * level * bl)))
    return out


class Ripple:
    """the last TRAVEL_S of the beat, one level a frame, so a led
    ``reach`` out hears it ``reach * TRAVEL_S`` late. a led at the middle
    gets exactly what the old show sent every led, flash cap and all, and
    one further out gets the same, later: never more flashes."""

    def __init__(self) -> None:
        self._levels: deque[float] = deque(maxlen=_HISTORY)

    def push(self, level: float) -> None:
        self._levels.append(float(level))

    def at(self, reach: float) -> float:
        if not self._levels:
            return 0.0
        back = min(len(self._levels) - 1, round(reach * TRAVEL_S * FPS))
        return self._levels[-1 - back]


def _places(out, k: int, n: int) -> list:
    """device ``k``'s led places, or a plain strip if it has none (yet)."""
    places = getattr(out, "places", None) or []
    if k < len(places) and len(places[k]) == n:
        return places[k]
    return layout.strip(n)


class Show(threading.Thread):
    def __init__(self, outputs: list, on_status: Callable[[str], None]) -> None:
        super().__init__(name="tide-lights", daemon=True)
        self.outputs = outputs
        self.on_status = on_status
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.pulse = 0.0
        self.pulse_peak = 0.0
        self.playing = False
        self.colors: tuple = ((1.0, 0.83, 0.0), (1.0, 0.45, 0.2))
        self.mode = "kick"
        self.strength = "normal"
        self.paused = "dim"
        self.spread = "ripple"
        self.sweep_until = 0.0
        self._live: dict[int, bool] = {}
        self._retry_at: dict[int, float] = {}
        self._connecting: set[int] = set()
        self._said = ""

    # ---- from the gui thread ----

    def set_pulse(self, level: float) -> None:
        with self._lock:
            self.pulse = float(level)
            self.pulse_peak = max(self.pulse_peak, self.pulse)

    def configure(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def sweep(self, seconds: float = 1.6) -> None:
        """the test button: a fast run through the colours."""
        with self._lock:
            self.sweep_until = time.monotonic() + seconds

    def stop(self) -> None:
        self._stop.set()

    # ---- the thread ----

    def run(self) -> None:
        flash, env, ripple = FlashLimiter(), Envelope(), Ripple()
        frame = 1.0 / FPS
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                with self._lock:
                    beat = max(self.pulse, self.pulse_peak)
                    self.pulse_peak = 0.0
                    playing, colors = self.playing, self.colors
                    mode, strength, paused = self.mode, self.strength, self.paused
                    spread = self.spread
                    sweep = now < self.sweep_until
                gain = STRENGTH.get(strength, 0.7)
                if sweep:
                    level = base = 1.0
                    phase = (now * 2.0) % 1.0
                elif playing:
                    target = brightness(mode, beat)
                    level = env.update(now, flash.update(now, target)) * gain
                    base = brightness(mode, 0.0) * gain
                    phase = (now / DRIFT_S) % 1.0
                else:
                    flash.update(now, 0.0)
                    env.update(now, 0.0)
                    level = base = REST * gain if paused == "dim" else 0.0
                    phase = (now / DRIFT_S) % 1.0
                ripple.push(level)
                if spread == "together" or sweep:
                    def level_at(_reach: float, level=level) -> float:
                        return level
                else:
                    level_at = ripple.at
                self._paint(now, colors, level_at, base, phase)
                next_t += frame
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_t = time.monotonic()
        finally:
            for i, out in enumerate(self.outputs):
                if self._live.get(i):
                    try:
                        out.close()
                    except OSError:
                        pass

    def _connect(self, i: int, out) -> None:
        """off the show's thread: a strip that doesn't answer takes two
        seconds to time out, and the other lights shouldn't stall for it."""
        try:
            out.connect()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._connecting.discard(i)
                self._retry_at[i] = time.monotonic() + RETRY_S
            self._report(f"{out.kind} {out.host}: {exc}"[:140])
            return
        with self._lock:
            self._connecting.discard(i)
            stopped = self._stop.is_set()
            if not stopped:
                self._live[i] = True
        if stopped:
            # the show ended while this one was connecting: let it go
            try:
                out.close()
            except OSError:
                pass
            return
        self._report_live()

    def _paint(self, now: float, colors: tuple, level_at: Callable[[float], float],
               base: float, phase: float) -> None:
        for i, out in enumerate(self.outputs):
            with self._lock:
                live = self._live.get(i, False)
                start = (not live and i not in self._connecting
                         and now >= self._retry_at.get(i, 0.0) and not self._stop.is_set())
                if start:
                    self._connecting.add(i)
            if start:
                threading.Thread(target=self._connect, args=(i, out), daemon=True,
                                 name=f"tide-lights-{out.kind}").start()
            if not live:
                continue
            try:
                maintain = getattr(out, "maintain", None)
                if maintain is not None:
                    maintain()
                out.send([paint(_places(out, k, n), colors, level_at, base, phase)
                          for k, n in enumerate(out.leds)])
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._live[i] = False
                    self._retry_at[i] = now + RETRY_S
                try:
                    out.close()
                except OSError:
                    pass
                self._report(f"lost {out.name}: {exc}"[:110])
                self._report_live()

    def _report_live(self) -> None:
        with self._lock:
            names = [o.name for i, o in enumerate(self.outputs) if self._live.get(i)]
        self._report("lighting " + ", ".join(names) if names else "no lights connected")

    def _report(self, text: str) -> None:
        with self._lock:
            if text == self._said or self._stop.is_set():
                # stopped: the plugin's status line may be gone already (a
                # slow device answering after tide let go)
                return
            self._said = text
        try:
            self.on_status(text)
        except RuntimeError:
            pass
