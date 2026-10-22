"""precise lyric timing: a small speech model listens to the song and
moves each word to where it's sung. it was in tide's core until 3.0
(tide/lyric_align.py, now align.py); tide asks it through the aligner
hook, after the lyrics' own word timing."""
from __future__ import annotations

import shutil

from PySide6.QtCore import QTimer

from . import align


class LyricTiming:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self._downloading = False
        self._progress = (0, align.MODEL_BYTES)
        align.MODEL_ROOT = ctx.paths.data_dir / "models"
        self._adopt_old_model()
        self._timer = ctx.adopt(QTimer())
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._show_progress)
        self._registered = False
        self._sync()

    def _adopt_old_model(self) -> None:
        """tide 2 kept the model in <data>/models; move it, don't fetch it again."""
        old = self.ctx.paths.data_dir.parents[1] / "models" / align.MODEL_NAME
        new = align.model_dir()
        if old.is_dir() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(new))
            except OSError:
                pass

    def _sync(self) -> None:
        if align.model_installed():
            if not self._registered:
                self.ctx.lyrics.add_aligner(self._align)
                self._registered = True
            self.ctx.set_status("on: words follow the audio, from the next song")
        else:
            self.ctx.set_status("needs its speech model: download it below")

    def _align(self, sheet, ref, should_stop, say):
        if not align.available():
            return None
        say("timing words to the audio...")
        stereo = align.decode(ref.payload, getattr(ref, "headers", None), should_stop=should_stop)
        if stereo is None or should_stop():
            return None
        em = align.Emissions(align.vocals(stereo))
        fit = align.align_sheet(sheet, em, should_stop=should_stop)
        return fit.sheet if fit is not None else None

    def on_action(self, key: str) -> None:
        if key == "download" and not self._downloading and not align.model_installed():
            self._downloading = True
            self._progress = (0, align.MODEL_BYTES)
            self._timer.start()

            def work():
                align.download_model(progress=self._on_progress,
                                     should_stop=lambda: not self.ctx.alive)
            self.ctx.jobs.run(work, self._downloaded, self._failed)
        elif key == "remove":
            align.remove_model()
            if self._registered:
                self.ctx.lyrics.remove_aligner()
                self._registered = False
            self.ctx.set_status("model removed. lyrics keep their own timing")

    def _on_progress(self, done: int, total: int) -> None:
        self._progress = (done, total)      # read by the timer on the gui thread

    def _show_progress(self) -> None:
        done, total = self._progress
        self.ctx.set_status(f"downloading the model: {100 * done // max(1, total)}%")

    def _downloaded(self, _result) -> None:
        self._downloading = False
        self._timer.stop()
        self._sync()

    def _failed(self, exc) -> None:
        self._downloading = False
        self._timer.stop()
        self.ctx.set_status(f"the download didn't work: {exc}"[:120])

    def stop(self) -> None:
        pass    # ctx takes the aligner away
