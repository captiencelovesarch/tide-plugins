"""discord presence: the profile line tide used to set from core
(tide/discord_rpc.py until 3.0). presence.py is that class; this file
feeds it from ctx and keeps the tray's "hide from discord" item."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from .presence import PYPRESENCE_AVAILABLE, DiscordPresence

OPTIONS = ("details_template", "state_template", "show_paused", "show_progress",
           "activity_type", "status_display", "links", "hidden", "hide_local")


class Feed(QObject):
    """what the presence class listens to, from ctx.playback."""
    track_changed = Signal(object)
    state_changed = Signal(str)
    duration_changed = Signal(float)
    position_changed = Signal(float)
    speed_changed = Signal(float)

    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx
        pb = ctx.playback
        pb.track_changed.connect(self.track_changed)
        pb.state_changed.connect(self.state_changed)
        pb.duration_changed.connect(self.duration_changed)
        pb.position_changed.connect(self.position_changed)
        pb.speed_changed.connect(self.speed_changed)

    def speed(self) -> float:
        return self._ctx.playback.speed()


class Discord:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.feed = ctx.adopt(Feed(ctx))
        self.presence = ctx.adopt(DiscordPresence(self.feed, ctx.theme.case, ctx.log))
        self.presence.start_wire()
        self.presence.connection_changed.connect(lambda _on: self._status())
        ctx.lyrics.changed.connect(self._on_lyrics)
        ctx.settings_changed.connect(self._apply)
        self.tray = ctx.ui.add_tray_action("hide from discord", self._hide, checkable=True,
                                           checked=ctx.settings["hidden"])
        self._apply(set())
        # whatever is playing now, so the profile doesn't wait for a skip
        track = ctx.playback.current()
        if track is not None:
            self.presence._on_current_changed(track)
            self.presence._on_state_changed(ctx.playback.state())

    def _apply(self, _keys) -> None:
        s = self.ctx.settings
        self.presence.set_options(**{k: s[k] for k in OPTIONS})
        self.presence.configure(s["app_id"], True)
        if s["lyrics"]:
            self.ctx.lyrics.subscribe()
        else:
            self.ctx.lyrics.unsubscribe()
            self.presence.set_lyric_timeline(None)
        self.tray.set_checked(s["hidden"])
        self._status()

    def _on_lyrics(self, sheet) -> None:
        if self.ctx.settings["lyrics"]:
            self.presence.set_lyric_timeline(sheet)

    def _hide(self, hidden: bool) -> None:
        # saved, so it holds across restarts like it did in core
        self.ctx.settings.set("hidden", bool(hidden))
        self._apply({"hidden"})

    def _status(self) -> None:
        if not PYPRESENCE_AVAILABLE:
            text = "needs pypresence"
        elif not self.ctx.settings["app_id"].strip():
            text = "paste your app id below to start"
        elif self.presence.connected:
            text = "hidden for now" if self.ctx.settings["hidden"] else "on your profile"
        else:
            text = "waiting for discord"
        self.ctx.set_status(text)

    def stop(self) -> None:
        self.presence.shutdown()
