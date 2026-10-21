"""the lights banner scene: a dark room seen from above at an angle.
simple 3d boxes, projected and painted back to front, as svg.

scene(phase) -> svg text. phase 0..1 slides the rainbow one full lap,
breath 0..1 is the slow swell of the light on the walls."""
import colorsys
import math

W, H = 1280, 720
EYE = (0.55, 2.25, 1.55)
TARGET = (0.08, 0.66, -0.62)
FOCAL = 1250


def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def norm(a):
    n = math.sqrt(dot(a, a)); return (a[0] / n, a[1] / n, a[2] / n)


F = norm(sub(TARGET, EYE))
R = norm(cross(F, (0, 1, 0)))
U = cross(R, F)


def cam(p):
    d = sub(p, EYE)
    return dot(d, R), dot(d, U), dot(d, F)


def proj(p):
    x, y, z = cam(p)
    return (W / 2 + FOCAL * x / z, H / 2 - FOCAL * y / z)


def pts(poly):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in (proj(p) for p in poly))


def depth(poly):
    return sum(cam(p)[2] for p in poly) / len(poly)


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c))) for c in rgb)


def shade(rgb, k):
    return hexc(tuple(c * k for c in rgb))


def rainbow(t, sat=1.0, val=1.0):
    r, g, b = colorsys.hsv_to_rgb(t % 1.0, sat, val)
    return (r * 255, g * 255, b * 255)


class Scene:
    def __init__(self):
        self.faces = []      # (depth, svg)
        self.glows = []      # drawn blurred, on top of the room, additive-ish
        self.overlay = []    # crisp emissive bits drawn last

    def poly(self, poly, fill, extra="", layer=None, d=None):
        el = f'<polygon points="{pts(poly)}" fill="{fill}" {extra}/>'
        (layer if layer is not None else self.faces).append((depth(poly) if d is None else d, el))

    def box(self, x0, x1, y0, y1, z0, z1, color, *, top=None, front=None, side=None, d_bias=0.0):
        """an axis-aligned box. faces facing away from the eye are skipped.
        color is the base rgb; faces are shaded by which way they face."""
        c = color
        faces = {
            "top": ([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0, 1, 0), top or shade(c, 1.0)),
            "front": ([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1), front or shade(c, 0.62)),
            "back": ([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0, 0, -1), shade(c, 0.5)),
            "left": ([(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)], (-1, 0, 0), side or shade(c, 0.78)),
            "right": ([(x1, y0, z0), (x1, y0, z1), (x1, y1, z1), (x1, y1, z0)], (1, 0, 0), side or shade(c, 0.78)),
        }
        center = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        for name, (poly, n, fill) in faces.items():
            fc = tuple(sum(p[i] for p in poly) / 4 for i in range(3))
            if dot(n, sub(EYE, fc)) <= 0:
                continue
            self.poly(poly, fill, d=depth(poly) + d_bias)
        return center

    def glow_poly(self, poly, fill, blur="g2", opacity=1.0):
        self.glows.append((0, f'<polygon points="{pts(poly)}" fill="{fill}" opacity="{opacity:.2f}" filter="url(#{blur})"/>'))

    def glow_ellipse(self, p, rx, ry, fill, blur="g3", opacity=0.5):
        x, y = proj(p)
        self.glows.append((0, f'<ellipse cx="{x:.0f}" cy="{y:.0f}" rx="{rx}" ry="{ry}" fill="{fill}" '
                              f'opacity="{opacity:.2f}" filter="url(#{blur})"/>'))

    def ring(self, c, r, axis, color, width, n=40):
        """a glowing fan ring: a circle in the plane facing ``axis``."""
        out = []
        for i in range(n):
            a = 2 * math.pi * i / n
            if axis == "z":
                out.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a), c[2]))
            else:
                out.append((c[0], c[1] + r * math.sin(a), c[2] + r * math.cos(a)))
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in (proj(p) for p in out)) + " Z"
        el = f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{width}"/>'
        return el


def scene(phase=0.0, breath=0.5) -> str:
    s = Scene()
    hue = lambda t: rainbow(t + phase)  # noqa: E731
    wall = (10, 7, 20)
    floor = (6, 4, 11)

    # room: floor and two walls
    s.poly([(-4, 0, -1.05), (4, 0, -1.05), (4, 0, 3), (-4, 0, 3)], hexc(floor), d=99)
    s.poly([(-4, 0, -1.05), (4, 0, -1.05), (4, 3, -1.05), (-4, 3, -1.05)], hexc(wall), d=100)
    s.poly([(-1.75, 0, -1.05), (-1.75, 0, 3), (-1.75, 3, 3), (-1.75, 3, -1.05)], shade(wall, 0.8), d=98)

    # light washing the back wall from the strip behind the desk
    wash = 0.26 + 0.14 * breath
    for i in range(7):
        t = i / 6
        x = -0.85 + 1.7 * t
        s.glow_ellipse((x, 1.0, -1.04), 150, 120, hexc(hue(t * 0.85)), "g4", wash)
    # pools of light on the floor and the desk
    s.glow_ellipse((0.0, 0.0, -0.4), 420, 120, hexc(hue(0.55)), "g4", 0.08 + 0.04 * breath)

    # desk
    dy, dz0, dz1, dx0, dx1 = 0.75, -1.02, -0.3, -0.95, 0.95
    for lx in (dx0 + 0.04, dx1 - 0.08):
        for lz in (dz0 + 0.04, dz1 - 0.08):
            s.box(lx, lx + 0.04, 0, dy - 0.04, lz, lz + 0.04, (12, 10, 20))
    s.box(dx0, dx1, dy - 0.04, dy, dz0, dz1, (30, 24, 46), top="#1d1730")

    # led strip on the desk's back edge, glowing up the wall
    n = 24
    for i in range(n):
        t0, t1 = i / n, (i + 1) / n
        x0, x1 = dx0 + (dx1 - dx0) * t0, dx0 + (dx1 - dx0) * t1
        col = hexc(hue(t0))
        q = [(x0, dy + 0.005, dz0 + 0.01), (x1, dy + 0.005, dz0 + 0.01), (x1, dy + 0.02, dz0 + 0.01), (x0, dy + 0.02, dz0 + 0.01)]
        s.overlay.append((depth(q) - 0.005, f'<polygon points="{pts(q)}" fill="{col}"/>'))
        s.glow_poly([(x0, dy, dz0 + 0.005), (x1, dy, dz0 + 0.005), (x1, dy + 0.35, -1.04), (x0, dy + 0.35, -1.04)],
                    col, "g3", 0.4)

    # monitor on a stand
    s.box(-0.06, 0.06, dy, dy + 0.14, -0.86, -0.8, (16, 14, 24))
    s.box(-0.14, 0.14, dy, dy + 0.01, -0.9, -0.74, (16, 14, 24))
    mx0, mx1, my0, my1, mz = -0.38, 0.38, dy + 0.1, dy + 0.53, -0.8
    s.box(mx0, mx1, my0, my1, mz - 0.03, mz, (10, 9, 16))
    # the screen: tide playing, from the cover's colours
    scr = [(mx0 + 0.015, my0 + 0.015, mz + 0.001), (mx1 - 0.015, my0 + 0.015, mz + 0.001),
           (mx1 - 0.015, my1 - 0.015, mz + 0.001), (mx0 + 0.015, my1 - 0.015, mz + 0.001)]
    s.poly(scr, "url(#screen)", d=depth(scr) - 0.01)
    def on_screen(u0, v0, u1, v1, fill, extra=""):
        ax, ay = mx0 + 0.015, my0 + 0.015
        w, h = (mx1 - mx0 - 0.03), (my1 - my0 - 0.03)
        q = [(ax + w * u0, ay + h * (1 - v1), mz + 0.002), (ax + w * u1, ay + h * (1 - v1), mz + 0.002),
             (ax + w * u1, ay + h * (1 - v0), mz + 0.002), (ax + w * u0, ay + h * (1 - v0), mz + 0.002)]
        s.overlay.append((depth(scr) - 0.02 - len(s.overlay) * 1e-5, f'<polygon points="{pts(q)}" fill="{fill}" {extra}/>'))
    on_screen(0.06, 0.1, 0.3, 0.52, hexc(hue(0.88)))
    on_screen(0.06, 0.31, 0.3, 0.52, hexc(hue(0.62)))
    on_screen(0.36, 0.12, 0.7, 0.19, "#ffffff", 'opacity="0.9"')
    on_screen(0.36, 0.24, 0.56, 0.29, "#ffffff", 'opacity="0.45"')
    for i, hgt in enumerate((0.1, 0.18, 0.13, 0.22, 0.16, 0.09, 0.2, 0.12, 0.17, 0.11)):
        on_screen(0.36 + i * 0.035, 0.52 - hgt, 0.36 + i * 0.035 + 0.022, 0.52, "#cdb8ff", 'opacity="0.85"')
    on_screen(0.06, 0.66, 0.94, 0.68, "#ffffff", 'opacity="0.2"')
    on_screen(0.06, 0.66, 0.42, 0.68, "#ffffff", 'opacity="0.85"')
    s.glow_poly(scr, "#7a4dff", "g3", 0.22)

    # keyboard: a dark slab, then the keys lit in a rainbow that slides
    kx0, kx1, kz0, kz1 = -0.38, 0.2, -0.53, -0.31
    s.box(kx0, kx1, dy, dy + 0.02, kz0, kz1, (12, 10, 18), top="#0c0a14")
    kb_top = depth([(kx0, dy + 0.02, kz0), (kx1, dy + 0.02, kz0), (kx1, dy + 0.02, kz1), (kx0, dy + 0.02, kz1)])
    kb_near = min(depth([(kx0, dy, kz1), (kx1, dy, kz1), (kx1, dy + 0.02, kz1), (kx0, dy + 0.02, kz1)]), kb_top)
    cols, rows = 15, 5
    for r in range(rows):
        for c in range(cols):
            u0, u1 = c / cols, (c + 1) / cols
            v0, v1 = r / rows, (r + 1) / rows
            x0 = kx0 + 0.012 + (kx1 - kx0 - 0.024) * u0 + 0.003
            x1 = kx0 + 0.012 + (kx1 - kx0 - 0.024) * u1 - 0.003
            z0 = kz0 + 0.01 + (kz1 - kz0 - 0.02) * v0 + 0.003
            z1 = kz0 + 0.01 + (kz1 - kz0 - 0.02) * v1 - 0.003
            col = hexc(hue(u0 * 0.9 + r * 0.02))
            q = [(x0, dy + 0.021, z0), (x1, dy + 0.021, z0), (x1, dy + 0.021, z1), (x0, dy + 0.021, z1)]
            s.overlay.append((kb_near - 0.01, f'<polygon points="{pts(q)}" fill="{col}"/>'))
    s.glow_poly([(kx0 - 0.02, dy, kz0 - 0.02), (kx1 + 0.02, dy, kz0 - 0.02),
                 (kx1 + 0.02, dy, kz1 + 0.02), (kx0 - 0.02, dy, kz1 + 0.02)], "url(#kbglow)", "g2", 0.9)

    # mouse pad with a lit edge, and the mouse
    px0, px1, pz0, pz1 = 0.27, 0.6, -0.56, -0.32
    pad = [(px0, dy + 0.003, pz0), (px1, dy + 0.003, pz0), (px1, dy + 0.003, pz1), (px0, dy + 0.003, pz1)]
    s.poly(pad, "#0b0912", d=depth(pad) - 0.02)
    s.overlay.append((depth(pad) - 0.006, f'<polygon points="{pts(pad)}" fill="none" stroke="{hexc(hue(0.52))}" stroke-width="2.5"/>'))
    s.glow_poly(pad, hexc(hue(0.52)), "g1", 0.6)
    s.box(0.42, 0.48, dy + 0.003, dy + 0.035, -0.47, -0.37, (20, 18, 30), top="#1c1828")
    mz = [(0.43, dy + 0.036, -0.39), (0.47, dy + 0.036, -0.39)]
    s.overlay.append((depth(mz) - 0.05, f'<polyline points="{pts(mz)}" stroke="{hexc(hue(0.62))}" stroke-width="3" fill="none"/>'))

    # pc tower on the desk, glass side toward us, three fans
    tx0, tx1, ty1, tz0, tz1 = 0.66, 0.9, dy + 0.48, -0.98, -0.55
    s.box(tx0, tx1, dy, ty1, tz0, tz1, (14, 12, 22), top="#16121f", side="#0b0914", front="#0e0b17")
    s.glow_poly([(tx0 - 0.01, dy, tz0), (tx0 - 0.01, dy, tz1), (tx0 - 0.01, ty1, tz1), (tx0 - 0.01, ty1, tz0)],
                hexc(hue(0.78)), "g3", 0.3)
    fans = []
    for i, fy in enumerate((dy + 0.38, dy + 0.24, dy + 0.1)):
        col = hexc(hue(0.78 + i * 0.12))
        c = ((tx0 + tx1) / 2, fy, tz1 + 0.002)
        fans.append(s.ring(c, 0.055, "z", col, 4))
        s.glows.append((0, s.ring(c, 0.055, "z", col, 10).replace("/>", ' filter="url(#g1)" opacity="0.9"/>')))
    s.overlay += [(cam(((tx0 + tx1) / 2, dy + 0.24, tz1))[2] - 0.01, f) for f in fans]

    faces = sorted(s.faces + s.overlay, key=lambda f: -f[0])
    kb = "".join(f'<stop offset="{t:.2f}" stop-color="{hexc(hue(t * 0.9))}"/>' for t in (0, 0.2, 0.4, 0.6, 0.8, 1))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<defs>
  <filter id="g1" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5"/></filter>
  <filter id="g2" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="14"/></filter>
  <filter id="g3" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="34"/></filter>
  <filter id="g4" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="70"/></filter>
  <linearGradient id="screen" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1c0b3e"/><stop offset="1" stop-color="#06102a"/></linearGradient>
  <linearGradient id="night" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0c1838"/><stop offset="1" stop-color="#1a2a58"/></linearGradient>
  <linearGradient id="kbglow" x1="0" y1="0" x2="1" y2="0">{kb}</linearGradient>
</defs>
<rect width="{W}" height="{H}" fill="#06040c"/>
{"".join(f[1] for f in faces)}
<g style="mix-blend-mode:screen">{"".join(g[1] for g in s.glows)}</g>
</svg>'''


if __name__ == "__main__":
    import sys
    open(sys.argv[1] if len(sys.argv) > 1 else "room.svg", "w").write(scene())
