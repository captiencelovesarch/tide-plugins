"""The vr thread: every openvr call happens here, never on tide's gui
thread. Learned in the spike (tools/vr-spike, design/steamvr.md):

* when steamvr's compositor dies, every overlay call blocks about a second.
  on the gui thread that would freeze tide.
* frames go up as opengl textures. vrclient shares them with its vulkan
  side; setOverlayRaw and png files crashed the compositor on linux.
* a qimage is top-down and a gl texture bottom-up, so the texture bounds
  are flipped. steamvr then reports laser y top-down too: flip one, never
  both.
* overlays get no button input while a game has focus. tracking and laser
  clicks on our own overlays do arrive, so that's all this uses.
* an interactive overlay (one the laser can click) takes the controllers
  from the game while you point at it. so only the open panel is
  interactive; the watch and the visualizer never are (h3vr froze when the
  watch came up mid-aim). touch goes by controller poses, which keep
  coming either way.
* overlays draw over the game's controllers. seethrough.py thins the
  panel where they are.

The gui thread renders the qt panel and posts frames in; this thread
posts what happened back out through VrBridge's signals.
"""
from __future__ import annotations

import logging
import os
import queue
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from . import geometry as geo
from . import ring as ringmod
from . import seethrough, visual

log = logging.getLogger("tide.plugin.steamvr")

HERE = Path(__file__).resolve().parent
RETRY_S = 10.0             # reconnect backoff goes 2 → 4 → 8 → 10 s
STALL_S = 0.5
STALLS_TO_GIVE_UP = 3      # in a row: one slow upload (a big first texture) isn't a dead compositor
GROW_S = 0.25
SPRING_S = 0.42            # modern's springy open
CLOSE_S = 0.22
WATCH_POP_S = 0.3
SEE_THROUGH_HZ = 45        # redraws of the windows over your controllers, at most
IDLE_HZ = 30               # nothing of ours in view: only watching for the wrist turn
PANEL_PX = (1000, 640)
PANEL_WIDTH_M = {"small": 0.34, "medium": 0.42, "large": 0.50}
WATCH_PX = 256
WATCH_WIDTH_M = 0.045
WATCH_SWELL = 0.35         # how much bigger the watch grows as look-to-open fills
SIDES = ("left", "right")
TOAST_PX = (760, 184)
TOAST_WIDTH_M = 0.42
TOAST_HOLD_S = 4.0
TOAST_POP_S = 0.3


def to_np(m34) -> np.ndarray:
    a = np.eye(4)
    a[:3] = np.ctypeslib.as_array(m34.m)
    return a


def to_m34(a: np.ndarray):
    import openvr
    m = openvr.HmdMatrix34_t()
    for i in range(3):
        for j in range(4):
            m.m[i][j] = float(a[i, j])
    return m


class VrBridge(QObject):
    """lives on the gui thread; the vr thread emits, qt queues it over."""
    status = Signal(str)
    panel_mouse = Signal(str, float, float)   # move / down / up / leave, x, y
    panel_opened = Signal(str)                # how
    panel_closed = Signal(str)                # why
    watch_visible = Signal(bool)
    lens = Signal(object)                     # each eye's outline, for the ring
    panel_scroll = Signal(float)              # stick up / down while pointing at the panel
    keyboard_text = Signal(str)               # what's typed so far
    keyboard_done = Signal(str)               # enter on steamvr's keyboard
    keyboard_closed = Signal()


class _Gl:
    """an opengl context on the vr thread, over a surface the gui thread
    made (qt wants offscreen surfaces created there)."""

    def __init__(self, surface) -> None:
        from PySide6.QtGui import QOpenGLContext
        self.surface = surface
        self.ctx = QOpenGLContext()
        if not self.ctx.create() or not self.ctx.makeCurrent(surface):
            raise RuntimeError("couldn't get an opengl context")
        self.textures: list = []

    def upload(self, img, tex=None):
        from PySide6.QtGui import QImage
        from PySide6.QtOpenGL import QOpenGLTexture
        self.ctx.makeCurrent(self.surface)
        img = img.convertToFormat(QImage.Format_RGBA8888)
        if tex is None or tex.width() != img.width() or tex.height() != img.height():
            if tex is not None:
                tex.destroy()
                self.textures.remove(tex)
            tex = QOpenGLTexture(QOpenGLTexture.Target2D)
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            tex.setData(img, QOpenGLTexture.DontGenerateMipMaps)
            self.textures.append(tex)
        else:
            # the qimage overload re-runs texture setup, which an allocated
            # texture refuses with a warning per frame
            tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8, img.constBits())
        self.ctx.functions().glFlush()
        return tex

    def drop(self, tex) -> None:
        """one texture, its overlay gone mid-session."""
        if tex is None or tex not in self.textures:
            return
        if self.ctx.makeCurrent(self.surface):
            tex.destroy()
        self.textures.remove(tex)

    def release(self) -> None:
        """drop this session's textures; the context stays for the next."""
        if self.ctx.makeCurrent(self.surface):
            for tex in self.textures:
                tex.destroy()
            self.ctx.doneCurrent()
        self.textures.clear()


class _Overlay:
    def __init__(self, ov, gl: _Gl, key: str, name: str, width_m: float,
                 px: tuple[int, int], *, interactive: bool = False) -> None:
        import openvr
        self.ov, self.gl = ov, gl
        self.h = ov.createOverlay(key, name)
        ov.setOverlayWidthInMeters(self.h, width_m)
        # what steamvr has now: width and alpha are set every tick, and each
        # call is a round trip to the compositor, so unchanged ones are skipped
        self._width, self._alpha = float(width_m), 1.0
        self.interactive = False
        if interactive:
            scale = openvr.HmdVector2_t()
            scale.v[0], scale.v[1] = float(px[0]), float(px[1])
            ov.setOverlayMouseScale(self.h, scale)
            ov.setOverlayFlag(self.h, openvr.VROverlayFlags_SendVRSmoothScrollEvents, True)
            self.set_interactive(True)
        bounds = openvr.VRTextureBounds_t()
        bounds.uMin, bounds.uMax, bounds.vMin, bounds.vMax = 0.0, 1.0, 1.0, 0.0
        ov.setOverlayTextureBounds(self.h, bounds)
        self.tex = None
        self.has_frame = False
        self.visible = False

    def upload(self, img) -> float:
        import openvr
        t0 = time.perf_counter()
        self.tex = self.gl.upload(img, self.tex)
        t = openvr.Texture_t()
        t.handle = int(self.tex.textureId())
        t.eType = openvr.TextureType_OpenGL
        t.eColorSpace = openvr.ColorSpace_Auto
        self.ov.setOverlayTexture(self.h, t)
        self.has_frame = True
        return time.perf_counter() - t0

    def set_interactive(self, on: bool) -> None:
        """a laser target or not. while it is one, steamvr takes the
        controllers while you point at it and our hand-skeleton reads stop,
        so poking needs it off."""
        import openvr
        if on == self.interactive:
            return
        self.interactive = on
        self.ov.setOverlayInputMethod(
            self.h, openvr.VROverlayInputMethod_Mouse if on else openvr.VROverlayInputMethod_None)
        # steamvr draws its laser at it outside the dashboard
        self.ov.setOverlayFlag(self.h, openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible, on)

    def place(self, pose: np.ndarray) -> None:
        import openvr
        self.ov.setOverlayTransformAbsolute(self.h, openvr.TrackingUniverseStanding, to_m34(pose))

    def place_on(self, device: int, offset: np.ndarray) -> None:
        self.ov.setOverlayTransformTrackedDeviceRelative(self.h, device, to_m34(offset))

    def width(self, metres: float) -> None:
        metres = float(metres)
        if abs(metres - self._width) > 1e-5:
            self._width = metres
            self.ov.setOverlayWidthInMeters(self.h, metres)

    def alpha(self, a: float) -> None:
        a = float(a)
        if abs(a - self._alpha) > 1e-3:
            self._alpha = a
            self.ov.setOverlayAlpha(self.h, a)

    def show(self, on: bool) -> None:
        if on == self.visible:
            return
        self.visible = on
        (self.ov.showOverlay if on else self.ov.hideOverlay)(self.h)

    def events(self):
        """(type, the event) for each queued overlay event."""
        import openvr
        ev = openvr.VREvent_t()
        while True:
            ok, ev = self.ov.pollNextOverlayEvent(self.h, ev)
            if not ok:
                return
            yield ev.eventType, ev

    def destroy(self) -> None:
        try:
            self.ov.destroyOverlay(self.h)
        except Exception:  # noqa: BLE001 - steamvr may already be gone
            pass


class _Lost(Exception):
    """steamvr went away; reconnect later."""


class VrThread(QThread):
    """connects to steamvr when it's running, runs the watch, the panel and
    the visualizer, reconnects after steamvr restarts. ``post`` is the only way
    in."""

    def __init__(self, bridge: VrBridge, surface, settings: dict) -> None:
        super().__init__()
        self.bridge = bridge
        self.surface = surface
        self.settings = dict(settings)
        self._cmds: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = False
        self.panel_open = False
        self.keyboard_open = False
        self.input = None
        self.pulse = 0.0
        self.pulse_peak = 0.0
        self.beat = 0.0           # this frame's pulse: the peak since the last one
        self.playing = False
        self.vis: dict[str, _Overlay] = {}        # the visualizer's overlays (visual.LAYERS)
        self.vis_width: dict[str, float] = {}     # each layer's width as drawn
        # kept across reconnects: they're drawn once (per song, per theme)
        self.back_img = None
        self.glow_img = None
        self.glow_ratio = 1.0
        # one gl context for the thread's life: dropping it here would
        # destroy a qt object off the gui thread (the deadlock qthreads.py
        # describes). it goes with this QThread object, after run() ends.
        self._gl: _Gl | None = None

    # ---- from the gui thread ----

    def post(self, *cmd) -> None:
        self._cmds.put(cmd)

    def stop(self) -> None:
        self._stop = True

    def dispose(self) -> None:
        """gui thread, after run() returned: drop the gl context here."""
        if self._gl is not None and not self.isRunning():
            import shiboken6
            if shiboken6.isValid(self._gl.ctx):
                shiboken6.delete(self._gl.ctx)
            self._gl = None

    # ---- the thread ----

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(0.1)

    def _note(self, text: str) -> None:
        """a line in sessions.log (the plugin's data dir): every session's
        start and end, and each kind of per-tick error once. the first
        "it keeps disconnecting" report had nothing to go on."""
        path = self.settings.get("log_path")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    def run(self) -> None:
        retry = 2.0
        while not self._stop:
            if not self._steamvr_running():
                self.bridge.status.emit("waiting for steamvr")
                self._sleep(RETRY_S)
                continue
            started = time.monotonic()
            try:
                self._session()
                why = "stopped"
            except _Lost as lost:
                why = str(lost)
            except Exception as exc:  # noqa: BLE001 - never let the thread die quietly
                log.exception("vr session failed")
                import traceback
                why = f"error: {exc or type(exc).__name__}"
                self._note("".join(traceback.format_exception(exc)).rstrip())
            self._note(f"session ended after {time.monotonic() - started:.0f} s: {why}")
            if self._stop:
                return
            self.bridge.status.emit(f"{why} · reconnecting"[:120])
            # a session that ran a while gets a quick retry; one that keeps
            # failing backs off
            retry = 2.0 if time.monotonic() - started > 60 else min(RETRY_S, retry * 2)
            self._sleep(retry)

    def _steamvr_running(self) -> bool:
        """a background-type init fails instead of launching steamvr, so
        switching the plugin on never starts steamvr by itself.
        TIDE_VR_OFFLINE=1 pretends steamvr is never running: tests, and
        working on the panel with a headset that shouldn't be touched."""
        if os.environ.get("TIDE_VR_OFFLINE") == "1":
            return False
        import openvr
        try:
            openvr.init(openvr.VRApplication_Background)
        except openvr.OpenVRError:
            return False
        openvr.shutdown()
        return True

    def _session(self) -> None:
        import openvr
        try:
            self.system = openvr.init(openvr.VRApplication_Overlay)
        except openvr.OpenVRError as exc:
            raise _Lost(f"couldn't connect: {exc}") from None
        overlays: list[_Overlay] = []
        cpu0, wall0 = time.thread_time(), time.monotonic()
        try:
            if self._gl is None:
                self._gl = _Gl(self.surface)
            self.ov = openvr.VROverlay()
            try:
                self._make_overlays(overlays)
            except openvr.OpenVRError as exc:
                # vrserver is up but the compositor isn't yet, or steamvr
                # restarted itself mid-startup: the handle we just made is
                # already gone. try again in a bit
                raise _Lost(f"steamvr is still starting ({type(exc).__name__})") from None
            self._input()
            self.bridge.lens.emit(self._lens())
            self.bridge.status.emit("connected")
            self._note("connected")
            self._loop()
        finally:
            wall = time.monotonic() - wall0
            if wall > 30:
                # what this thread cost, to check performance reports against
                self._note(f"vr thread used {100 * (time.thread_time() - cpu0) / wall:.1f}% "
                           "of a cpu core")
            for o in overlays + list(self.vis.values()):
                o.destroy()
            self.vis, self.vis_width = {}, {}
            if self._gl is not None:
                self._gl.release()
            openvr.shutdown()
            if self.panel_open:
                self.panel_open = False
                self.bridge.panel_closed.emit("steamvr")
            self.bridge.watch_visible.emit(False)

    def _make_overlays(self, made: list) -> None:
        """the panel, watch, back, glow and toast. each goes into ``made``
        as soon as it exists, so a failure part way still destroys them."""
        self.panel = _Overlay(self.ov, self._gl, "tide.vr.panel", "tide", self._panel_width(),
                              PANEL_PX, interactive=True)
        made.append(self.panel)
        self.watch = _Overlay(self.ov, self._gl, "tide.vr.watch", "tide watch", WATCH_WIDTH_M,
                              (WATCH_PX, WATCH_PX))
        made.append(self.watch)
        # the panel's back (you walked round it) and its glow, under it
        self.back = _Overlay(self.ov, self._gl, "tide.vr.back", "tide back",
                             self._panel_width(), PANEL_PX)
        made.append(self.back)
        self.glow = _Overlay(self.ov, self._gl, "tide.vr.glow", "tide glow",
                             self._panel_width() * self.glow_ratio, PANEL_PX)
        made.append(self.glow)
        # the now playing toast: never a laser target, so it can't take
        # the controllers from a game
        self.toast = _Overlay(self.ov, self._gl, "tide.vr.toast", "tide now playing",
                              TOAST_WIDTH_M, TOAST_PX)
        made.append(self.toast)
        self.ov.setOverlaySortOrder(self.toast.h, 2)
        self.ov.setOverlaySortOrder(self.watch.h, 1)        # over its glow
        self.ov.setOverlaySortOrder(self.glow.h, 0)
        self.ov.setOverlaySortOrder(self.panel.h, 1)
        self.ov.setOverlaySortOrder(self.back.h, 1)

    def _input(self) -> None:
        """haptics. no buttons: an overlay never gets them while a game
        has focus."""
        import openvr
        self.input = None
        try:
            inp = openvr.VRInput()
            inp.setActionManifestPath(str(HERE / "actions.json"))
            self.act = {name: inp.getActionHandle(f"/actions/tide/in/{name}")
                        for name in ("hand_left", "hand_right")}
            self.haptic = inp.getActionHandle("/actions/tide/out/haptic")
            self.sources = {s: inp.getInputSourceHandle(f"/user/hand/{s}") for s in SIDES}
            self.sets = (openvr.VRActiveActionSet_t * 1)()
            self.sets[0].ulActionSet = inp.getActionSetHandle("/actions/tide")
            self.input = inp
        except openvr.OpenVRError as exc:
            log.warning("no hand input: %s", exc)

    def _lens(self) -> dict:
        """each eye's outline (viewport uv), projection and offset."""
        import openvr
        eyes = {}
        for eye, name in ((openvr.Eye_Left, "left"), (openvr.Eye_Right, "right")):
            uv = []
            try:
                mesh = self.system.getHiddenAreaMesh(eye, openvr.k_eHiddenAreaMesh_LineLoop)
                n = int(mesh.unTriangleCount)
                if n and mesh.pVertexData:
                    uv = [(float(mesh.pVertexData[i].v[0]), float(mesh.pVertexData[i].v[1]))
                          for i in range(n)]
            except Exception:  # noqa: BLE001 - an ellipse will do
                uv = []
            eyes[name] = {
                "uv": uv,
                "proj": list(self.system.getProjectionRaw(eye)),
                "eye_x": float(to_np(self.system.getEyeToHeadTransform(eye))[0, 3]),
            }
        return eyes

    def _panel_width(self) -> float:
        return PANEL_WIDTH_M.get(self.settings.get("size"), PANEL_WIDTH_M["medium"])

    def _animate(self) -> bool:
        return not self.settings.get("motion_off", False)

    # ---- per tick ----

    def _loop(self) -> None:
        import openvr
        try:
            hz = float(self.system.getFloatTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_DisplayFrequency_Float) or 0.0)
        except openvr.OpenVRError:
            hz = 0.0
        hz = hz if hz > 10 else 90.0
        self.poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()
        self.panel_open = False
        self.panel_pose = None
        self.opened_at = 0.0
        self.last_touch = -1e9
        self.attention = geo.Attention(float(self.settings.get("linger", 2.5)),
                                       animate=self._animate())
        self.watch_shown = geo.Shown(geo.WATCH_SHOW_S, geo.WATCH_HIDE_S)
        self.dwell = geo.Dwell()
        self.flash = geo.FlashLimiter()
        self.ripple = ringmod.Ripple()
        self.envelope = ringmod.Envelope()
        self.pokes = {side: geo.Poke() for side in SIDES}
        self.poke_px = (-1.0, -1.0)
        self.touch = geo.TouchMode()
        self.panel_base = None           # the gui's last frame, before seethrough
        self.see_caps: list = []
        self.see_cursor = None
        self.see_width = self._panel_width()
        self.drawn = ([], None)
        self.drawn_at = -1e9
        self.behind = False
        self.toast_at: float | None = None
        self.toast_follow = geo.LazyFollow()
        self.watch_pose = None
        self.ripples: dict[int, tuple[float, float]] = {}    # pool slot -> (started, power)
        self.ripple_n = 0
        self.chip_fade = visual.Fade()
        self.title_at: float | None = None
        self.glow_flash = geo.FlashLimiter()
        self.glow_env = ringmod.Envelope()
        self.closing_at: float | None = None
        self.closing_from = 1.0
        self.watch_popped_at = -1e9
        self.stalls = 0
        self._errors_seen: set[str] = set()
        # made before the back and glow go back in: used first, it ended
        # every session after the first (steamvr restarting itself while
        # it starts up was enough) and tide had to reinstall the plugin
        # (2026-11-04)
        frames: dict[str, object] = {}
        if self.back_img is not None:
            frames["back"] = self.back_img
        if self.glow_img is not None:
            frames["glow"] = self.glow_img
        next_t = time.perf_counter()
        while not self._stop:
            now = time.perf_counter()
            self.system.getDeviceToAbsoluteTrackingPose(
                openvr.TrackingUniverseStanding, 0.0, self.poses)
            self._events()
            if self.input is not None:
                try:
                    self.input.updateActionState(self.sets)
                except openvr.OpenVRError:
                    pass
            self._guard("commands", self._commands, frames)
            self.beat = max(self.pulse, self.pulse_peak) if self.playing else 0.0
            self.pulse_peak = 0.0
            if self.panel_open:
                self._guard("panel", self._panel_tick, now)
            else:
                self._guard("watch", self._watch_tick, now)
            self._guard("closing", self._closing_tick, now)
            self._guard("visualizer", self._vis_tick, now)
            self._guard("toast", self._toast_tick, now)
            for name, ov in (("watch", self.watch), ("back", self.back), ("glow", self.glow),
                             ("toast", self.toast)):
                img = frames.pop(name, None)
                if img is not None:
                    self._upload(ov, img)
            fresh = frames.pop("panel", None)
            if fresh is not None:
                self.panel_base = fresh
            img = self._panel_image(now, fresh is not None)
            if img is not None:
                self._upload(self.panel, img)
            next_t += 1.0 / (hz if self._busy() else min(hz, IDLE_HZ))
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()

    def _busy(self) -> bool:
        """something of ours is in view and moves with you or the music:
        it gets the headset's frame rate. the rest of the time IDLE_HZ
        still catches the wrist turn (the watch waits WATCH_SHOW_S anyway)
        for a third to a fifth of the ticks."""
        return (self.panel_open or self.closing_at is not None or self.watch_shown.on
                or self.toast_at is not None or any(o.visible for o in self.vis.values()))

    def _upload(self, ov: _Overlay, img) -> None:
        if ov.upload(img) > STALL_S:
            self.stalls += 1
            if self.stalls >= STALLS_TO_GIVE_UP:
                raise _Lost("steamvr stopped answering")
        else:
            self.stalls = 0

    def _panel_image(self, now: float, fresh: bool):
        """the frame to upload this tick, if any: the gui's last one with
        the windows over your controllers and the cursor on top."""
        if self.panel_base is None:
            return None
        caps, cur = self.see_caps, self.see_cursor
        if not fresh:
            if seethrough.same(caps, self.drawn[0]) and seethrough.same(cur, self.drawn[1]):
                return None
            if now - self.drawn_at < 1.0 / SEE_THROUGH_HZ:
                return None
        self.drawn = (caps, cur)
        self.drawn_at = now
        if not caps and cur is None:
            return self.panel_base
        return seethrough.compose(self.panel_base, caps, cur, self.see_width,
                                  self.settings.get("cursor", "#ffffff"))

    def _guard(self, where: str, fn, *args) -> None:
        """one tick of one part. an error there is noted once and skipped;
        it no longer ends the whole session (a stray bad pose or a failed
        read used to disconnect you). steamvr really going away still
        does (_Lost)."""
        try:
            fn(*args)
        except _Lost:
            raise
        except Exception as exc:  # noqa: BLE001 - see above
            key = f"{where}:{type(exc).__name__}:{exc}"
            if key not in self._errors_seen:
                self._errors_seen.add(key)
                log.exception("vr %s tick failed", where)
                import traceback
                self._note(f"{where} tick failed (once noted, carrying on): "
                           + "".join(traceback.format_exception(exc)).rstrip())

    def _springy(self) -> bool:
        return self._animate() and self.settings.get("springy", False)

    def _events(self) -> None:
        import openvr
        ev = openvr.VREvent_t()
        while self.system.pollNextEvent(ev):
            self._keyboard_event(ev.eventType)
            if ev.eventType in (openvr.VREvent_Quit, openvr.VREvent_DriverRequestedQuit):
                # without the ack steamvr waits on us at shutdown
                self.system.acknowledgeQuit_Exiting()
                raise _Lost("steamvr quit")

    def _commands(self, frames: dict) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            kind = cmd[0]
            if kind == "panel_frame":
                frames["panel"] = cmd[1]          # newest wins
            elif kind == "watch_frame":
                frames["watch"] = cmd[1]
            elif kind == "toast":
                if self.settings.get("toast", "off") != "off" and not self.panel_open:
                    frames["toast"] = cmd[1]
                    self.toast_at = time.perf_counter()
                    self.toast_follow.reset()
            elif kind == "back_frame":
                self.back_img = frames["back"] = cmd[1]
            elif kind == "glow_frame":
                self.glow_img = frames["glow"] = cmd[1]
                self.glow_ratio = float(cmd[2])
            elif kind == "close":
                self._close(cmd[1])
            elif kind == "keyboard":
                if self.panel_open:
                    self._keyboard(cmd[1])
            elif kind == "pulse":
                # several can land between two vr frames: keep the highest,
                # like tide's backdrop does, or a beat in the middle is lost
                self.pulse = float(cmd[1])
                self.pulse_peak = max(self.pulse_peak, self.pulse)
            elif kind == "playing":
                self.playing = bool(cmd[1])
            elif kind == "vis_frames":
                self._vis_setup(cmd[1], cmd[2])
            elif kind == "settings":
                old_mode, old_hand = self._vis_mode(), self.settings.get("watch_hand")
                self.settings.update(cmd[1])
                self._vis_settings(old_mode, old_hand)
                self.panel.width(self._panel_width())
                self.attention.linger_s = float(self.settings.get("linger", 2.5))
                self.attention.animate = self._animate()

    # ---- steamvr's keyboard ----

    def _keyboard_text(self) -> str:
        """pyopenvr's getKeyboardText passes its buffer where the length
        goes; call the function table directly."""
        import ctypes
        fn = self.ov.function_table.getKeyboardText
        n = int(fn(None, 0))
        buf = ctypes.create_string_buffer(max(1, n))
        fn(buf, max(1, n))
        return buf.value.decode("utf-8", "replace")

    def _keyboard(self, text: str) -> None:
        import openvr
        try:
            self.ov.showKeyboardForOverlay(
                self.panel.h, openvr.k_EGamepadTextInputModeNormal,
                openvr.k_EGamepadTextInputLineModeSingleLine, openvr.KeyboardFlag_Modal,
                "search", 120, text, 0)
            self.keyboard_open = True
        except openvr.OpenVRError as exc:
            log.warning("no keyboard: %s", exc)
            self.bridge.keyboard_closed.emit()

    def _keyboard_event(self, kind: int) -> None:
        import openvr
        if not self.keyboard_open:
            return
        if kind == openvr.VREvent_KeyboardCharInput:
            self.bridge.keyboard_text.emit(self._keyboard_text())
        elif kind == openvr.VREvent_KeyboardDone:
            self.keyboard_open = False
            self.bridge.keyboard_done.emit(self._keyboard_text())
        elif kind == openvr.VREvent_KeyboardClosed:
            self.keyboard_open = False
            self.bridge.keyboard_closed.emit()

    # ---- device reads ----

    def _hmd(self) -> np.ndarray | None:
        import openvr
        p = self.poses[openvr.k_unTrackedDeviceIndex_Hmd]
        return to_np(p.mDeviceToAbsoluteTracking) if p.bPoseIsValid else None

    def _controller(self, side: str) -> np.ndarray | None:
        import openvr
        role = (openvr.TrackedControllerRole_LeftHand if side == "left"
                else openvr.TrackedControllerRole_RightHand)
        idx = self.system.getTrackedDeviceIndexForControllerRole(role)
        if idx == openvr.k_unTrackedDeviceIndexInvalid:
            return None
        p = self.poses[idx]
        return to_np(p.mDeviceToAbsoluteTracking) if p.bPoseIsValid else None

    def _tip(self, side: str) -> np.ndarray | None:
        """the front tip of a controller, what you touch things with."""
        ctrl = self._controller(side)
        return None if ctrl is None else geo.controller_tip(ctrl, side)

    def _buzz_hand(self, side: str, seconds: float, freq: float, amp: float) -> None:
        if self.input is None or not self.settings.get("haptics", True):
            return
        try:
            self.input.triggerHapticVibrationAction(
                self.haptic, 0.0, seconds, freq, amp, self.sources[side])
        except Exception:  # noqa: BLE001 - haptics are a nicety
            pass

    def _buzz(self, seconds: float, freq: float, amp: float) -> None:
        if self.input is None or not self.settings.get("haptics", True):
            return
        for side in SIDES:
            try:
                self.input.triggerHapticVibrationAction(
                    self.haptic, 0.0, seconds, freq, amp, self.sources[side])
            except Exception:  # noqa: BLE001 - haptics are a nicety
                pass

    # ---- watch ----

    def _watch_tick(self, now: float) -> None:
        side = self.settings.get("watch_hand", "left")
        hmd, ctrl = self._hmd(), self._controller(side)
        wanted = hmd is not None and ctrl is not None and geo.checking_watch(hmd, ctrl, side)
        was = self.watch_shown.on
        shown = self.watch_shown.update(now, wanted)
        if shown != was:
            self.bridge.watch_visible.emit(shown)
            self.dwell.reset()
            self.watch_popped_at = now
        self.watch.show(shown and self.watch.has_frame)
        if not shown or hmd is None or ctrl is None:
            return
        pos = geo.wrist_point(ctrl, side)
        self.watch_pose = geo.facing(pos, hmd[:3, 3])        # its glow follows
        self.watch.place(self.watch_pose)
        # modern: it pops in with a little overshoot
        pop = (geo.ease_out_back((now - self.watch_popped_at) / WATCH_POP_S)
               if self._springy() else 1.0)

        fill = 0.0
        if self.settings.get("open_by_looking", True):
            ang, _d = geo.off_center_deg(hmd, pos)
            fill = self.dwell.update(now, ang <= geo.DWELL_LOOK_DEG)
        # it swells as you look, so you see it's about to open
        self.watch.width(WATCH_WIDTH_M * max(0.05, pop) * (1.0 + WATCH_SWELL * fill))
        if fill >= 1.0:
            self._open("watch (looked)", now)
            return
        other = "right" if side == "left" else "left"
        tip = self._tip(other)
        if tip is not None and float(np.linalg.norm(tip - pos)) <= geo.POKE_M:
            self._open("watch (poked)", now)

    # ---- panel ----

    def _open(self, how: str, now: float) -> None:
        if self.panel_open:
            return
        hmd = self._hmd()
        if hmd is None:
            return
        self.panel_open = True
        self.closing_at = None
        self.toast_at = None
        self.toast.show(False)
        for p in self.pokes.values():
            p.reset()
        self.touch.reset()
        self.panel.set_interactive(True)
        self.panel_pose = geo.spawn_in_front(hmd)
        self.panel.place(self.panel_pose)
        # the back faces the other way, so it reads right from behind
        self.back.place(self.panel_pose @ geo.turn_around())
        self.glow.place(self.panel_pose)
        self.behind = False
        self.opened_at = now
        self.last_touch = now
        self.attention.reset(now)
        self.watch_shown.reset()
        self.dwell.reset()
        self.watch.show(False)
        self.watch.width(WATCH_WIDTH_M)
        self.bridge.watch_visible.emit(False)
        self._panel_look(now, 0.0 if self._animate() else 1.0, self._panel_width())
        self._show_panel(True)
        self._buzz(0.08, 120, 0.9)
        self.bridge.panel_opened.emit(how)

    def _close(self, why: str) -> None:
        if not self.panel_open:
            return
        if self.keyboard_open:
            self.keyboard_open = False
            try:
                self.ov.hideKeyboard()
            except Exception:  # noqa: BLE001 - it may be gone already
                pass
            self.bridge.keyboard_closed.emit()
        self.panel_open = False
        for p in self.pokes.values():
            p.reset()
        self.touch.reset()
        self.see_caps, self.see_cursor = [], None
        if self._springy() and self.panel.visible:
            # modern: a quick shrink and fade instead of vanishing
            self.closing_at = time.perf_counter()
        else:
            self._show_panel(False)
        self.bridge.panel_mouse.emit("leave", 0.0, 0.0)
        self._buzz(0.03, 200, 0.6)
        self.bridge.panel_closed.emit(why)

    def _closing_tick(self, now: float) -> None:
        if self.closing_at is None:
            return
        t = (now - self.closing_at) / CLOSE_S
        if t >= 1.0:
            self.closing_at = None
            self._show_panel(False)
            return
        k = 1.0 - t * t
        self._panel_look(now, self.closing_from * k, self._panel_width() * (1.0 - 0.08 * t))

    def _panel_tick(self, now: float) -> None:
        import openvr
        mouse = {openvr.VREvent_MouseMove: "move", openvr.VREvent_MouseButtonDown: "down",
                 openvr.VREvent_MouseButtonUp: "up"}
        for kind, ev in self.panel.events():
            if kind in mouse:
                self.last_touch = now
                self.bridge.panel_mouse.emit(mouse[kind], float(ev.data.mouse.x),
                                             float(ev.data.mouse.y))
            elif kind == openvr.VREvent_ScrollSmooth:
                self.last_touch = now
                self.bridge.panel_scroll.emit(float(ev.data.scroll.ydelta))
            else:
                self._keyboard_event(kind)
        if self.keyboard_open:
            # you're looking at the keyboard, not the panel: keep it
            self.last_touch = now
        hmd = self._hmd()
        if hmd is None or self.panel_pose is None:
            return
        center = self.panel_pose[:3, 3]
        gaze, _d = geo.off_center_deg(hmd, center)
        hands = {s: self._controller(s) for s in SIDES}
        hand_m = min((float(np.linalg.norm(c[:3, 3] - center)) for c in hands.values()
                      if c is not None), default=99.0)
        # opening: the panel grows to size as it fades in; modern springs
        grow, fade_in = 1.0, 1.0
        if self.attention.animate:
            age = now - self.opened_at
            if self._springy():
                grow = 0.6 + 0.4 * geo.ease_out_back(age / SPRING_S)
                fade_in = geo.ease_out(age / 0.18)
            else:
                fade_in = geo.ease_out(age / GROW_S)
                grow = 0.3 + 0.7 * fade_in
        width = self._panel_width() * grow
        self.see_width = width
        self._poke(now, hands, width)
        self.see_caps = seethrough.holes(self.panel_pose, hmd[:3, 3], hands, width, PANEL_PX)
        alpha, close = self.attention.update(now, gaze, hand_m, now - self.last_touch)
        alpha = min(alpha, fade_in)
        self.closing_from = alpha
        self.behind = float(geo.to_local(self.panel_pose, hmd[:3, 3])[2]) < 0.0
        self._panel_look(now, alpha, width)
        if close:
            self._close("looked away")

    def _toast_tick(self, now: float) -> None:
        """the now playing toast: fades in a little under where you're
        looking, holds, fades out. "follow" trails your view lazily,
        "stay" keeps where it came up."""
        if self.toast_at is None:
            return
        mode = self.settings.get("toast", "off")
        age = now - self.toast_at
        alpha = geo.toast_alpha(age, TOAST_HOLD_S)
        hmd = self._hmd()
        if self.panel_open or mode == "off" or hmd is None or (age > 1.0 and alpha <= 0.0):
            self.toast_at = None
            self.toast.show(False)
            return
        target = geo.toast_spot(hmd)
        if mode == "stay" and self.toast_follow.pos is not None:
            pos = self.toast_follow.pos
        else:
            pos = self.toast_follow.update(now, target)
        self.toast.place(geo.facing(pos, hmd[:3, 3]))
        pop = geo.ease_out_back(age / TOAST_POP_S) if self._springy() else 1.0
        self.toast.width(TOAST_WIDTH_M * max(0.05, 0.85 + 0.15 * pop))
        self.toast.alpha(alpha if self._animate() else (1.0 if alpha > 0 else 0.0))
        self.toast.show(self.toast.has_frame and alpha > 0.0)

    def _show_panel(self, on: bool) -> None:
        self.panel.show(on)
        if not on:
            self.back.show(False)
            self.glow.show(False)

    def _panel_look(self, now: float, alpha: float, width: float) -> None:
        """the panel, its back and its glow at ``alpha`` and ``width``.
        from behind only the back shows: an overlay is seen from both
        sides, so the front would show through mirrored."""
        self.panel.width(width)
        self.panel.alpha(0.0 if self.behind else alpha)
        self.back.width(width)
        self.back.alpha(alpha)
        self.back.show(self.panel.visible and self.behind and self.back.has_frame)
        glow = self._glow_level(now) * alpha
        self.glow.width(width * self.glow_ratio)
        self.glow.alpha(glow)
        self.glow.show(self.panel.visible and glow > 0.003 and self.glow.has_frame)

    def _glow_level(self, now: float) -> float:
        """the panel's glow with the beat, like the ring: flash-limited,
        then smoothed to the headset's rate."""
        mode = self.settings.get("panel_glow", "off")
        if mode == "off" or not self._animate():
            return 0.0
        strength = ringmod.STRENGTH.get(self.settings.get("ring_strength", "normal"), 0.7)
        pulse = self.beat
        limited = self.glow_flash.update(now, ringmod.glow_level(mode, pulse, self.playing))
        return self.glow_env.update(now, limited) * strength

    def _poke(self, now: float, hands: dict, width: float) -> None:
        """a controller tip on the panel, as mouse events. with a tip right
        at it the panel stops being a laser target, so the laser and your
        hand don't both drive the cursor; pull away and the laser's back."""
        height = width * PANEL_PX[1] / PANEL_PX[0]
        local = {side: geo.to_local(self.panel_pose, geo.controller_tip(c, side))
                 for side, c in hands.items() if c is not None}
        pressing = next((s for s, p in self.pokes.items() if p.pressed), None)
        was = self.touch.touching
        if self.settings.get("touch", True) and not self.behind:
            touching = self.touch.update(list(local.values()), width, height, pressing is not None)
        else:
            # laser only, or you're round the back
            self.touch.reset()
            touching = False
        self.panel.set_interactive(not touching and not self.behind)
        if touching:
            self._touch(now, local, width, height, pressing)
        elif was:
            for p in self.pokes.values():
                p.reset()
            self.poke_px = (-1.0, -1.0)
            self.bridge.panel_mouse.emit("leave", 0.0, 0.0)
        pressing = next((s for s, p in self.pokes.items() if p.pressed), None)
        self.see_cursor = seethrough.cursor(local, width, PANEL_PX, pressing)

    def _touch(self, now: float, local: dict, width: float, height: float,
               pressing: str | None) -> None:
        order = [pressing] if pressing else list(SIDES)
        for side in order:
            if side not in local:
                continue
            for kind, u, v in self.pokes[side].update(local[side], width, height):
                x, y = u * PANEL_PX[0], v * PANEL_PX[1]
                if kind == "move" and abs(x - self.poke_px[0]) < 0.5 and abs(y - self.poke_px[1]) < 0.5:
                    continue
                self.poke_px = (x, y)
                self.bridge.panel_mouse.emit(kind, x, y)
                if kind == "down":
                    self._buzz_hand(side, 0.02, 220, 0.8)
                elif kind == "up":
                    self._buzz_hand(side, 0.012, 260, 0.4)
            if self.pokes[side].hovering:
                self.last_touch = now
                break

    # ---- the visualizer (visual.py) ----

    def _vis_mode(self) -> str:
        mode = self.settings.get("visualizer", "off")
        return mode if mode in visual.LAYERS else "off"

    def _vis_setup(self, mode: str, layers: dict) -> None:
        """this song's textures, {layer: (image, width)}. layers drawn for
        another mode (the setting changed on the way) are left out; the
        same layer again (a new song's colours) uploads in place."""
        if mode != self._vis_mode():
            return
        wanted = visual.LAYERS[mode]
        for name, (img, width_m) in layers.items():
            if name not in wanted:
                continue
            self.vis_width[name] = float(width_m)
            for key in (visual.ripple_keys() if name == "ripple" else [name]):
                o = self.vis.get(key)
                if o is None:
                    o = self.vis[key] = self._vis_overlay(key, name, width_m, img)
                elif name not in ("ripple", "watch_glow"):     # those two size themselves
                    o.width(width_m)
                o.upload(img)
            if name == "chip_title":
                self.title_at = time.perf_counter()

    def _vis_overlay(self, key: str, name: str, width_m: float, img) -> _Overlay:
        """none of these is a laser target: they never take the
        controllers from a game."""
        import openvr
        o = _Overlay(self.ov, self._gl, f"tide.vr.vis.{key}", f"tide {key}", width_m,
                     (img.width(), img.height()))
        o.alpha(0.0)
        if name in ("glow", "rim"):
            # each eye gets its own half: its own lens outline
            self.ov.setOverlayFlag(o.h, openvr.VROverlayFlags_SideBySide_Parallel, True)
            o.place_on(openvr.k_unTrackedDeviceIndex_Hmd,
                       geo.translate(0.0, 0.0, -ringmod.DISTANCE_M))
        elif name.startswith("chip"):
            self._vis_place_chip(o, name)
            self.ov.setOverlaySortOrder(o.h, 1 if name == "chip" else 0)
        elif name == "watch_glow":
            self.ov.setOverlaySortOrder(o.h, 0)       # under the watch
        return o

    def _vis_place_chip(self, o: _Overlay, name: str) -> None:
        """locked to your head: placed once, steamvr keeps it there."""
        import openvr
        side = visual.chip_side(self.settings.get("watch_hand", "left"))
        pose = visual.title_pose(side) if name == "chip_title" else visual.chip_pose(side)
        o.place_on(openvr.k_unTrackedDeviceIndex_Hmd, pose)

    def _vis_settings(self, old_mode: str, old_hand) -> None:
        """the settings changed: drop what the new mode doesn't use (the gui
        sends its textures), move the chip if the watch changed wrists."""
        mode = self._vis_mode()
        if mode != old_mode:
            keep = visual.LAYERS[mode]
            for key in list(self.vis):
                if ("ripple" if key.startswith("ripple") else key) not in keep:
                    o = self.vis.pop(key)
                    o.destroy()
                    self._gl.drop(o.tex)
            self.ripples.clear()
        if self.settings.get("watch_hand") != old_hand:
            for key in ("chip", "chip_ring", "chip_title"):
                if key in self.vis:
                    self._vis_place_chip(self.vis[key], key)

    def _vis_tick(self, now: float) -> None:
        if not self.vis:
            return
        mode = self._vis_mode()
        if mode == "off" or not self._animate():
            for o in self.vis.values():
                o.show(False)
            return
        strength = ringmod.STRENGTH.get(self.settings.get("ring_strength", "normal"), 0.7)
        pulse = self.beat
        # the flash limit judges the real jumps; the envelope then smooths
        # what's left to the headset's frame rate
        limited = self.flash.update(now, ringmod.glow_level("breathing", pulse, self.playing))
        level = self.envelope.update(now, limited) * strength
        rim_alpha, rim_scale = self.ripple.update(now, pulse)
        self._vis_watch(level)
        if mode == "floor":
            self._vis_floor(now, self.ripple.hit, strength)
        elif mode == "corner":
            self._vis_corner(now, level)
        elif mode == "bottom":
            self._vis_bottom(level, rim_alpha * strength * 0.8, rim_scale)

    def _vis_watch(self, level: float) -> None:
        o = self.vis.get("watch_glow")
        if o is None:
            return
        on = self.watch.visible and self.watch_pose is not None and level > 0.003
        o.show(on and o.has_frame)
        if on:
            o.place(self.watch_pose)
            o.width(self.watch._width * visual.WATCH_GLOW)
            o.alpha(level)

    def _vis_floor(self, now: float, hit: str | None, strength: float) -> None:
        """a kick starts a ripple under your head; it stays there as it
        spreads. the pool's overlays take turns."""
        keys = [k for k in visual.ripple_keys() if k in self.vis]
        if not keys:
            return
        if hit is not None and self.playing:
            hmd = self._hmd()
            if hmd is not None:
                slot = self.ripple_n % len(keys)
                self.ripple_n += 1
                self.vis[keys[slot]].place(visual.floor_pose(hmd[:3, 3]))
                self.ripples[slot] = (now, 1.0 if hit == "full" else ringmod.Ripple.SOFT)
        for slot, key in enumerate(keys):
            o = self.vis[key]
            start, power = self.ripples.get(slot, (None, 0.0))
            look = None if start is None else visual.ripple_look(now - start)
            if look is None:
                self.ripples.pop(slot, None)
                o.show(False)
                continue
            width, alpha = look
            o.width(width)
            o.alpha(alpha * power * strength)
            o.show(o.has_frame)

    def _vis_corner(self, now: float, level: float) -> None:
        """the chip shows while music plays and the panel's closed (the
        panel has it all); the title for a few seconds after a new song."""
        fade = self.chip_fade.update(now, self.playing and not self.panel_open)
        chip, ring, title = (self.vis.get(k) for k in ("chip", "chip_ring", "chip_title"))
        if chip is not None:
            chip.alpha(0.94 * fade)
            chip.show(chip.has_frame and fade > 0.003)
        if ring is not None:
            ring.alpha(fade * level)
            ring.width(self.vis_width.get("chip_ring", visual.CHIP_M) * (1.0 + 0.06 * level))
            ring.show(ring.has_frame and fade * level > 0.003)
        if title is not None:
            age = 1e9 if self.title_at is None else now - self.title_at
            a = geo.toast_alpha(age, visual.TITLE_HOLD_S) * fade
            title.alpha(a)
            title.show(title.has_frame and a > 0.003)

    def _vis_bottom(self, level: float, rim: float, rim_scale: float) -> None:
        glow, rim_o = self.vis.get("glow"), self.vis.get("rim")
        if glow is not None:
            glow.show(glow.has_frame and level > 0.003)
            glow.alpha(level)
        if rim_o is not None:
            rim_o.show(rim_o.has_frame and rim > 0.003)
            rim_o.alpha(rim)
            # the ripple travels inward (up, here): the head-locked quad
            # shrinks round the middle of your view
            rim_o.width(self.vis_width.get("rim", 1.0) * rim_scale)
