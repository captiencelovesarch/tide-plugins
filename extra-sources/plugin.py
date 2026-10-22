"""soundcloud, bandcamp and mixcloud as tide sources. they were in tide's
core until 3.0 (tide/sources/); now they come with this plugin and show
in the source panel while it's installed, each with its own on switch."""
from __future__ import annotations

from .bandcamp import BandcampSource
from .mixcloud import MixcloudSource
from .soundcloud import SoundCloudSource


class ExtraSources:
    def start(self, ctx) -> None:
        ctx.sources.register(SoundCloudSource())
        ctx.sources.register(BandcampSource())
        # dj mixes and radio shows: a different thing, so it waits for a yes
        ctx.sources.register(MixcloudSource(), on=False)
        if any(ctx.sources.is_on(s) for s in ("soundcloud", "bandcamp", "mixcloud")):
            ctx.set_status("soundcloud, bandcamp and mixcloud are in the source panel")
        else:
            ctx.set_status("all three are switched off: turn them on in the source panel")

    def stop(self) -> None:
        pass    # ctx takes the sources away
