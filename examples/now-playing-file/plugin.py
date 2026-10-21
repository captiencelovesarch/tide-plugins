"""now playing file: the song that's playing, as one line in a text file."""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from tide.plugin_api import TrackInfo


class NowPlayingFile:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.path = ctx.paths.data_dir / "now-playing.txt"
        ctx.playback.track_changed.connect(lambda track: self.write())
        ctx.playback.state_changed.connect(lambda state: self.write())
        ctx.settings_changed.connect(lambda keys: self.write())
        self.write()

    def stop(self) -> None:
        self.path.write_text("", encoding="utf-8")

    def on_action(self, key: str) -> None:
        if key == "open_folder":
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

    def write(self) -> None:
        track: TrackInfo | None = self.ctx.playback.current()
        paused = self.ctx.playback.state() != "playing"
        if track is None or (paused and self.ctx.settings["when_paused"] == "clear"):
            line = ""
        else:
            line = self.ctx.settings["format"].format(
                title=track.title, artists=track.artists, album=track.album)
        self.path.write_text(line + "\n" if line else "", encoding="utf-8")
        self.ctx.set_status(f"writing {self.path}" if line else "nothing playing")
