"""Where each led sits, so the show paints by place instead of by number.

OpenRGB numbers a keyboard's leds in key-code order (a, b, c...), so a
gradient by led number put the colours on the keys in alphabet order:
a speckle, never a wash (2026-11-09). A keyboard says where every key
is (its matrix map), ram is a row of sticks, a header is a strip.

Each led gets two numbers from 0 to 1:

    along   where it sits in the colour wash: left to right on a
            keyboard, along a strip, stick by stick on ram
    reach   how far it is from where a kick lands (the middle of the
            device, or of each stick and strip). the beat gets there
            later and a little softer the further out it is.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SINGLE, LINEAR, MATRIX = 0, 1, 2


@dataclass(frozen=True)
class Zone:
    leds: int
    kind: int = LINEAR
    # (height, width, cells): each cell is the zone's own led number, or
    # NO_LED. row by row from the top left, as OpenRGB sends it
    matrix: tuple | None = None


NO_LED = 0xFFFFFFFF


def strip(n: int) -> list[tuple[float, float]]:
    """``n`` leds in a line: the wash runs along it, a kick lands in the
    middle and runs out to both ends (which end is "first" doesn't
    matter, and on a fan ring the two ends meet)."""
    out = []
    for i in range(n):
        t = (i + 0.5) / n
        out.append((t, abs(t - 0.5) * 2.0))
    return out


def _matrix(zone: Zone) -> list[tuple[float, float]]:
    h, w, cells = zone.matrix
    at: dict[int, tuple[float, float]] = {}
    for k, led in enumerate(cells):
        if led != NO_LED and 0 <= led < zone.leds and led not in at:
            row, col = divmod(k, w)
            at[led] = ((col + 0.5) / w, (row + 0.5) / h)
    if not at:
        return strip(zone.leds)
    # distance from the middle, in key units (a 22 x 6 keyboard is wide,
    # so a ring stays round instead of stretching to the board's shape)
    raw = {led: math.hypot((x - 0.5) * w, (y - 0.5) * h) for led, (x, y) in at.items()}
    far = max(raw.values()) or 1.0
    out = []
    for i in range(zone.leds):
        if i in at:
            out.append((at[i][0], raw[i] / far))
        else:
            out.append(((i + 0.5) / zone.leds, 0.5))   # a led the map leaves out
    return out


def device(zones: list[Zone], count: int) -> list[tuple[float, float]]:
    """(along, reach) for each of a device's ``count`` leds, in its own
    led order. Each zone with leds gets an equal share of the wash, so
    four ram sticks show four steps of it; a kick lands in the middle of
    every zone at once."""
    live = [z for z in zones if z.leds > 0]
    out: list[tuple[float, float]] = []
    for zi, z in enumerate(live):
        if z.matrix:            # openrgb only maps matrix zones; trust a map anyway
            spots = _matrix(z)
        elif z.kind == SINGLE or z.leds == 1:
            spots = [(0.5, 0.0)] * z.leds
        else:
            spots = strip(z.leds)
        out += [((zi + a) / len(live), r) for a, r in spots]
    if len(out) != count:
        # the zones don't add up to the device (an old server, a device
        # that only lists its leds): a plain strip is never wrong
        return strip(count)
    return out
