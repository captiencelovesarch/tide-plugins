"""local files: the music on this computer as a tide source. it was in
tide's core until 3.0 (tide/sources/local.py); library.py is that source,
with covers added. the index lives in the plugin's cache folder."""
from __future__ import annotations

from .library import LocalSource


class LocalFiles:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.source = LocalSource(ctx.settings["folder"].strip() or None,
                                  index_path=ctx.paths.cache_dir / "index.sqlite")
        self.source.configure = self._pick_folder
        ctx.sources.register(self.source)
        ctx.settings_changed.connect(self._on_settings)
        self._scan()

    def _scan(self) -> None:
        self.ctx.set_status(f"reading {self.source.music_dir}...")
        self.ctx.jobs.run(self.source.rescan, self._scanned, self._failed)

    def _scanned(self, count: int) -> None:
        folder = self.source.music_dir
        self.ctx.set_status(f"{count:,} songs in {folder}" if count
                            else f"no music found in {folder}")
        if self.ctx.settings["watch"]:
            self.source.start_watcher()

    def _failed(self, exc) -> None:
        self.ctx.set_status(f"couldn't read the folder: {exc}"[:120])

    def _on_settings(self, keys) -> None:
        if "folder" in keys:
            self.source.set_music_dir(self.ctx.settings["folder"].strip())
            self._scan()
        elif "watch" in keys:
            if self.ctx.settings["watch"]:
                self.source.start_watcher()
            else:
                self.source.stop_watcher()

    def on_action(self, key: str) -> None:
        if key == "rescan":
            self._scan()

    def _pick_folder(self, parent) -> None:
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(parent, "your music folder",
                                                  self.source.music_dir)
        if folder and folder != self.source.music_dir:
            self.ctx.settings.set("folder", folder)
            self._on_settings({"folder"})

    def stop(self) -> None:
        self.source.shutdown()
