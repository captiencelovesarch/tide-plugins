"""Build the store banners for the official plugins.

    python tools/banners.py                  # every plugin below, all cuts
    python tools/banners.py lights --no-motion

Each plugin gets three 1280×720 webp files in <slug>/store/:

- grid.webp: background, the plugin's mark, the capture in a frame. no text.
- full.webp: the same with "<name> × tide" on a darkened left side.
- motion.webp: the grid cut where only the capture frame moves. 4 to 8 s,
  periodic so the loop seam is a crossfade, and checked against the flash
  cap (under 3 a second).

Sources live in <slug>/store/src/ (real captures, the drawn backgrounds).
Needs pillow, rsvg-convert and the IBM Plex Mono font.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = Path(__file__).resolve().parent.parent
W, H = 1280, 720
FPS = 20
PAD = 60
FONT_BOLD = "/usr/share/fonts/TTF/IBMPlexMono-Bold.ttf"
FONT = "/usr/share/fonts/TTF/IBMPlexMono-Regular.ttf"
FULL_BOX = (540, 130, 680, 460)
GRID_BOX = (200, 92, 880, 536)
# a whole desktop is 16:9, so kde's frames are too
KDE_FULL_BOX = (548, 171, 672, 378)
KDE_GRID_BOX = (232, 110, 900, 506)


# ---------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------

def svg(text: str, w: int, h: int | None = None) -> Image.Image:
    png = subprocess.run(["rsvg-convert", "-w", str(w), "-h", str(h or w)],
                         input=text.encode(), capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def cover(img: Image.Image, w=W, h=H, anchor=(0.5, 0.5)) -> Image.Image:
    s = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    x = round((img.width - w) * anchor[0])
    y = round((img.height - h) * anchor[1])
    return img.crop((x, y, x + w, y + h))


def zoomed(img, w, h, s, anchor=(0.5, 0.5)):
    """``img`` at w×h, zoomed in by s (≥1) about ``anchor``."""
    return cover(cover(img, round(w * s), round(h * s)), w, h, anchor)


def rounded_mask(size, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), r, fill=255)
    return m


def mark(slug: str, size: int, tint) -> Image.Image:
    """the plugin's own mark: its outline icon, white, on a frosted tile."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, size - 1, size - 1), size // 4, fill=tint,
                                           outline=(255, 255, 255, 60), width=2)
    icon = (REPO / slug / "icon.svg").read_text().replace("currentColor", "#ffffff")
    ic = svg(icon, round(size * 0.62))
    tile.alpha_composite(ic, ((size - ic.width) // 2, (size - ic.height) // 2))
    return tile


def shade_left(canvas, strength=170, reach=0.62):
    """darken the left side so white type always reads."""
    g = Image.new("L", (W, 1))
    for i in range(W):
        g.putpixel((i, 0), round(strength * max(0.0, 1 - i / (W * reach)) ** 1.4))
    black = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    black.putalpha(g.resize((W, H)))
    canvas.alpha_composite(black)


class Stage:
    """everything that stays still around one capture frame."""

    def __init__(self, bg: Image.Image, box, r=18):
        self.box, self.r = box, r
        x, y, w, h = box
        c = bg.convert("RGBA")
        sh = Image.new("RGBA", (w + 2 * PAD, h + 2 * PAD), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((PAD, PAD + 10, PAD + w, PAD + h + 10), r, fill=(0, 0, 0, 170))
        c.alpha_composite(sh.filter(ImageFilter.GaussianBlur(24)), (x - PAD, y - PAD))
        self.base = c
        self.mask = rounded_mask((w, h), r)
        self.edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(self.edge).rounded_rectangle((0, 0, w - 1, h - 1), r,
                                                    outline=(255, 255, 255, 46), width=2)
        self.top: list[tuple[Image.Image, tuple[int, int]]] = []   # still things over the frame

    @property
    def size(self):
        return self.box[2], self.box[3]

    def glow(self, color):
        _, _, w, h = self.box
        g = Image.new("RGBA", (w + 2 * PAD, h + 2 * PAD), (0, 0, 0, 0))
        ImageDraw.Draw(g).rounded_rectangle((PAD, PAD, PAD + w, PAD + h), self.r, outline=color, width=10)
        return g.filter(ImageFilter.GaussianBlur(22))

    def frame(self, cap: Image.Image, glow=None) -> Image.Image:
        x, y, w, h = self.box
        c = self.base.copy()
        if glow is not None:
            c.alpha_composite(glow, (x - PAD, y - PAD))
        c.paste(cover(cap.convert("RGB"), w, h), (x, y), self.mask)
        c.alpha_composite(self.edge, (x, y))
        for img, at in self.top:
            c.alpha_composite(img, at)
        return c


def grid_stage(slug, bg, box=GRID_BOX):
    st = Stage(bg, box)
    st.top.append((mark(slug, 120, (10, 10, 20, 150)), (48, 48)))
    return st


def full_stage(slug, name, bg, box=FULL_BOX):
    bg = bg.convert("RGBA")
    shade_left(bg)
    st = Stage(bg, box)
    m = mark(slug, 104, (255, 255, 255, 30))
    text = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(text)
    x, y = 76, 196
    ty = y + m.height + 36
    d.text((x, ty), name, font=ImageFont.truetype(FONT_BOLD, 64), fill=(255, 255, 255, 255))
    d.text((x + 2, ty + 80), "× tide", font=ImageFont.truetype(FONT, 40), fill=(255, 255, 255, 215))
    st.top += [(m, (x, y)), (text, (0, 0))]
    return st


# ---------------------------------------------------------------------------
# motion
# ---------------------------------------------------------------------------

def smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def two_state(t, T, hold_a, fade):
    """0 holding A, 1 holding B, smooth fades between. the second fade ends
    at T, so the loop starts on A again: the seam is a crossfade."""
    if t < hold_a:
        return 0.0
    if t < hold_a + fade:
        return smooth((t - hold_a) / fade)
    if t < T - fade:
        return 1.0
    return 1.0 - smooth((t - (T - fade)) / fade)


def _lum(img) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    px = img.convert("RGB").resize((64, 36)).get_flattened_data()
    return sum(0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) for r, g, b in px) / len(px)


def flashes_per_second(frames, box) -> float:
    """the worst 1 s window's flash count in the moving area, seam included.
    a flash is a pair of opposing luminance swings of 10% or more."""
    x, y, w, h = box
    ls = [_lum(f.crop((x, y, x + w, y + h))) for f in frames]
    ls += ls[:FPS]
    swings, ext, way = [], ls[0], 0
    for i, v in enumerate(ls[1:], 1):
        if way >= 0 and v - ext <= -0.1:
            swings.append(i); ext, way = v, -1
        elif way <= 0 and v - ext >= 0.1:
            swings.append(i); ext, way = v, 1
        elif (way >= 0 and v > ext) or (way <= 0 and v < ext):
            ext = v
    worst = max((sum(1 for s in swings if i <= s < i + FPS) for i in range(len(ls))), default=0)
    return worst / 2


def save_still(img: Image.Image, path: Path):
    img.convert("RGB").save(path, "WEBP", quality=90, method=6)


def save_motion(frames, path: Path, box):
    seconds = len(frames) / FPS
    if not 4 <= seconds <= 8:
        raise SystemExit(f"{path}: {seconds:.1f} s, it should be 4 to 8")
    rate = flashes_per_second(frames, box)
    if rate >= 3:
        raise SystemExit(f"{path}: {rate} flashes a second, the cap is under 3")
    rgb = [f.convert("RGB") for f in frames]
    rgb[0].save(path, save_all=True, append_images=rgb[1:], duration=round(1000 / FPS),
                loop=0, quality=80, method=4)
    print(f"  motion: {seconds:.1f} s, {rate} flashes/s at worst, {path.stat().st_size / 1e6:.1f} MB")


# ---------------------------------------------------------------------------
# the plugins
# ---------------------------------------------------------------------------

def src(slug, name) -> Path:
    return REPO / slug / "store" / "src" / name


def steamvr(motion: bool):
    bg = cover(Image.open(src("steamvr", "aurora.png")).convert("RGB"), anchor=(0.5, 0.62))
    panel = Image.open(src("steamvr", "panel-playing.png")).convert("RGB")
    panel = panel.crop((0, 200, panel.width, panel.height))     # the sky above it is empty
    watch = Image.open(src("steamvr", "watch.png")).convert("RGB")
    purple = (150, 90, 255, 200)
    stills = {"full": full_stage("steamvr", "steamvr", bg), "grid": grid_stage("steamvr", bg)}
    out = {k: st.frame(panel, st.glow(purple)) for k, st in stills.items()}
    if motion:
        # the watch on your wrist, then the panel it opens, under a slow zoom
        st = stills["grid"]
        glow, (w, h), T = st.glow(purple), st.size, 7.0
        frames = []
        for i in range(round(T * FPS)):
            t = i / FPS
            s = 1.0 + 0.05 * (0.5 - 0.5 * math.cos(2 * math.pi * t / T))
            k = two_state(t, T, 1.8, 1.0)
            a = zoomed(watch, w, h, s, (0.4, 0.45))
            b = zoomed(panel, w, h, s, (0.6, 0.4))
            frames.append(st.frame(Image.blend(a, b, k), glow))
        out["motion"] = frames
    return out


def _room_module():
    spec = importlib.util.spec_from_file_location("lights_room", src("lights", "room.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lights(motion: bool):
    room = _room_module()

    def render(phase, breath, w=2560, h=1440):
        return svg(room.scene(phase, breath), w, h).convert("RGB")

    still = render(0.0, 0.5)
    bg = still.resize((W, H)).filter(ImageFilter.GaussianBlur(12))
    bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), 0.35)
    pink = (255, 60, 140, 150)
    stills = {"full": full_stage("lights", "lights", bg), "grid": grid_stage("lights", bg)}
    out = {k: st.frame(still, st.glow(pink)) for k, st in stills.items()}
    if motion:
        # the rainbow slides one lap per loop; the wall light swells 4
        # times per loop (0.67 a second)
        st = stills["grid"]
        glow, T = st.glow(pink), 6.0
        frames = []
        for i in range(round(T * FPS)):
            t = i / FPS
            breath = 0.5 - 0.5 * math.cos(2 * math.pi * 4 * t / T)
            frames.append(st.frame(render(t / T, breath, 1600, 900), glow))
        out["motion"] = frames
    return out


def kde(motion: bool):
    bg = svg(src("kde", "wallpaper.svg").read_text(), W, H).convert("RGB")
    song = Image.open(src("kde", "desktop-song.png")).convert("RGB")
    yours = Image.open(src("kde", "desktop-yours.png")).convert("RGB")
    red, blue = (230, 40, 40, 150), (60, 120, 240, 150)
    stills = {"full": full_stage("kde", "kde desktop", bg, KDE_FULL_BOX),
              "grid": grid_stage("kde", bg, KDE_GRID_BOX)}
    out = {k: st.frame(song, st.glow(red)) for k, st in stills.items()}
    if motion:
        # your desktop, then the song's: the wallpaper level as it happens
        st = stills["grid"]
        g_blue, g_red, (w, h), T = st.glow(blue), st.glow(red), st.size, 7.2
        a, b = cover(yours, w, h), cover(song, w, h)
        frames = []
        for i in range(round(T * FPS)):
            k = two_state(i / FPS, T, 2.0, 1.2)
            frames.append(st.frame(Image.blend(a, b, k), Image.blend(g_blue, g_red, k)))
        out["motion"] = frames
    return out


BUILDERS = {"steamvr": steamvr, "lights": lights, "kde": kde}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--no-motion", action="store_true")
    args = ap.parse_args()
    for slug in args.slugs or list(BUILDERS):
        if slug not in BUILDERS:
            ap.error(f"no banner recipe for {slug}")
        print(slug)
        cuts = BUILDERS[slug](not args.no_motion)
        folder = REPO / slug / "store"
        for cut in ("grid", "full"):
            save_still(cuts[cut], folder / f"{cut}.webp")
        if "motion" in cuts:
            box = KDE_GRID_BOX if slug == "kde" else GRID_BOX
            save_motion(cuts["motion"], folder / "motion.webp", box)
    return 0


if __name__ == "__main__":
    sys.exit(main())
