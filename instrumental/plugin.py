"""instrumental finder: a [mute lyrics] toggle in the lyrics view that
swaps the song for its instrumental (or karaoke) cut from any source
that's on, at the same spot, and back again. it was in tide's core until
3.0 (tide/instrumental.py, now finder.py, and the window's swap)."""
from __future__ import annotations

from .finder import find_instrumental


class Instrumental:
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.vocal = None            # the song we swapped away from
        self.instrumental = None     # what's playing instead, once found
        self.position = 0.0
        self._gen = 0                # a search for a song that's gone is dropped
        self.toggle = ctx.ui.add_lyrics_toggle("mute lyrics", self._on_toggle)
        ctx.playback.track_changed.connect(self._on_track)

    # ---- the toggle ----

    def _on_toggle(self, on: bool, track) -> None:
        if on:
            if track is None:
                self.toggle.set_checked(False)
                return
            self.vocal = track
            self.position = self.ctx.playback.position()
            self.toggle.set_status("looking for an instrumental version...")
            self._gen += 1
            gen = self._gen
            self.ctx.jobs.run(lambda: find_instrumental(track, self.ctx.music.search_all),
                              lambda match: self._found(gen, match),
                              lambda exc: self._failed(gen, exc))
            return
        # off: back to the vocal, where you are now
        self._gen += 1
        vocal, swapped = self.vocal, self.instrumental is not None
        self._reset(status="")
        if vocal is not None and swapped:
            self.ctx.music.play_at(vocal, self.ctx.playback.position())

    def _found(self, gen: int, match) -> None:
        if gen != self._gen or self.vocal is None:
            return
        current = self.ctx.playback.current()
        if current is None or current.id != self.vocal.id:
            return                   # you moved on while it looked
        if match is None:
            self._reset(status="no instrumental version in your sources")
            return
        self.instrumental = match.track
        self.toggle.set_status(f"playing the instrumental, from {match.source_name}")
        self.ctx.music.play_at(match.track, self.position)

    def _failed(self, gen: int, exc) -> None:
        if gen == self._gen:
            self._reset(status=f"the search didn't work: {exc}"[:120])

    def _reset(self, status: str) -> None:
        self.vocal = None
        self.instrumental = None
        self.toggle.set_checked(False)
        self.toggle.set_status(status)

    # ---- you picked another song ----

    def _on_track(self, track) -> None:
        if self.vocal is None:
            return
        ids = {self.vocal.id, getattr(self.instrumental, "id", None)}
        if track is None or track.id not in ids:
            self._gen += 1
            self._reset(status="")

    def stop(self) -> None:
        pass    # ctx takes the toggle away
