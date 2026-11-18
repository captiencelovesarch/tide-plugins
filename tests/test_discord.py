"""Discord presence: lyrics that don't skip lines, inside Discord's budget.

The complaint (2026-10-07): live lyrics refreshed every ~4 s, so two lines
sung inside one gap meant the first never showed. Discord drops writes
past 5 per 20 s, so the fix can't be "push faster": each write now carries
the line about to be sung plus every line that starts before the next
write could go out, a little ahead of time. Also pinned here: the member
list shows the song, links and the listen button, the privacy switches,
and the fallback when a client refuses the newer fields.

Moved here from tide's tests/test_discord_presence.py with the plugin
(tide 3.0). Run:  PYTHONPATH=~/tide/src python -m pytest tests/test_discord.py
"""
import dataclasses
import importlib
import sys
import types
import unittest
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from tests._support import PLUGINS_DIR
from tide.api import Track
from tide.plugin_api import track_info

_pkg = types.ModuleType("tide_plugins_test_discord")
_pkg.__path__ = [str(PLUGINS_DIR / "discord")]
sys.modules["tide_plugins_test_discord"] = _pkg
discord_rpc = importlib.import_module("tide_plugins_test_discord.presence")


class PlayState:
    PLAYING = "playing"
    PAUSED = "paused"


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now


class _Timer:
    """Stands in for the flush QTimer on the fake clock."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.due: float | None = None

    def start(self, ms: int) -> None:
        self.due = self.clock.now + ms / 1000.0

    def stop(self) -> None:
        self.due = None

    def isActive(self) -> bool:
        return self.due is not None

    def remainingTime(self) -> int:
        return int(max(0.0, self.due - self.clock.now) * 1000)


class _Client:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.writes: list[tuple[float, str, dict]] = []
        self.refuse_newer = False

    def update(self, **kw) -> None:
        if self.refuse_newer and "status_display_type" in kw:
            raise RuntimeError("unknown field")
        self.writes.append((self.clock.now, "update", kw))

    def clear(self) -> None:
        self.writes.append((self.clock.now, "clear", {}))

    def states(self) -> list[str]:
        return [kw.get("state", "") for _t, kind, kw in self.writes if kind == "update"]


def _track(source="ytmusic", **extras):
    return track_info(Track(video_id="vid1" if source != "soundcloud" else
                 "https://soundcloud.com/a/b",
                 title="Let Down", artists="Radiohead", album="OK Computer",
                 duration_seconds=300, source=source, extras=extras))


@unittest.skipUnless(discord_rpc.PYPRESENCE_AVAILABLE, "needs pypresence")
class _Case(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.clock = _Clock()
        self._real_time = discord_rpc.time
        discord_rpc.time = SimpleNamespace(monotonic=self.clock.monotonic,
                                           time=self.clock.time)
        self.addCleanup(setattr, discord_rpc, "time", self._real_time)
        feed = SimpleNamespace(speed=lambda: 1.0)
        self.p = discord_rpc.DiscordPresence(feed)
        self.p._flush_timer = _Timer(self.clock)
        self.client = _Client(self.clock)
        self.p._client = self.client
        self.p._connected = True
        self.pos = 0.0

    def play(self, track=None) -> None:
        self.p._on_current_changed(track or _track())
        self.p._on_position_changed(0.0)
        self.p._on_state_changed(PlayState.PLAYING)
        self.settle()

    def settle(self) -> None:
        t = self.p._flush_timer
        if t.due is not None and self.clock.now >= t.due:
            t.due = None
            self.p._flush()

    def listen(self, seconds: float, step: float = 0.05) -> None:
        """Playback for ``seconds``: the clock and the playhead move
        together, position ticks arrive every ``step``."""
        end = self.clock.now + seconds
        while self.clock.now < end - 1e-9:
            self.clock.now += step
            self.pos += step
            self.p._on_position_changed(self.pos)
            self.settle()

    def assert_budget(self) -> None:
        times = [t for t, _k, _kw in self.client.writes]
        for i, t in enumerate(times):
            inside = [u for u in times[i:] if u < t + discord_rpc.PUSH_WINDOW_S]
            self.assertLessEqual(len(inside), discord_rpc.PUSH_BUDGET,
                                 f"over budget from t={t}")


class LyricTests(_Case):
    def test_fast_lines_are_never_skipped(self) -> None:
        # a line a second for 30 s: the old 4.5 s spacing lost most of them
        # (from 5 s in: a song's start spends two writes on the track and
        # on play, as it always has)
        lines = [f"line {n}" for n in range(30)]
        self.play()
        self.p.set_lyric_timeline(([5.0 + n for n in range(30)], lines))
        self.listen(36)
        shown = " / ".join(s.removeprefix("♪ ") for s in self.client.states())
        for line in lines:
            self.assertIn(line, shown)
        self.assert_budget()

    def test_slow_lines_go_one_at_a_time(self) -> None:
        # past the first 20 s, so the start's own writes are out of the window
        times = [21.0, 27.0, 33.0, 39.0]
        self.play()
        self.p.set_lyric_timeline((times, ["one", "two", "three", "four"]))
        self.listen(41)
        lyric_states = [s for s in self.client.states() if s.startswith("♪")]
        self.assertEqual(lyric_states, ["♪ one", "♪ two", "♪ three", "♪ four"])

    def test_a_line_lands_ahead_of_its_timestamp(self) -> None:
        self.play()
        self.listen(4)
        self.p.set_lyric_timeline(([10.0], ["late line"]))
        self.listen(10)
        when = [t for t, _k, kw in self.client.writes
                if kw.get("state") == "♪ late line"][0]
        # 1000 + 4 s already played; the line's at song time 10
        self.assertLess(when - 1000.0, 10.0)
        self.assertGreater(when - 1000.0, 10.0 - 1.0)

    def test_a_gap_puts_the_artist_line_back(self) -> None:
        self.play()
        self.p.set_lyric_timeline(([0.0, 3.0, 12.0], ["sung", "", "again"]))
        self.listen(8)
        self.assertEqual(self.client.states()[-1].lower(), "radiohead · ok computer")

    def test_merged_text_fits_discords_field(self) -> None:
        self.play()
        long = ["x" * 60 + str(n) for n in range(10)]
        self.p.set_lyric_timeline(([n * 0.5 for n in range(10)], long))
        self.listen(6)
        for state in self.client.states():
            self.assertLessEqual(len(state), discord_rpc.STATE_MAX)


def _breaks(*spans):
    """A stand-in for ctx.lyrics.break_at over fixed (begin, end) spans."""
    def at(t: float):
        return next(((b, e) for b, e in spans if b <= t < e), None)
    return at


class BreakTests(_Case):
    """The lyrics tab's three dots for a break between lines, lighting up
    as the next line gets close (2026-11-09)."""

    STAGES = ["♪ " + d for d in discord_rpc.DOT_STAGES]

    def _break(self, end: float) -> list[str]:
        """a line at 21, a break from 23 to ``end``, the next line at
        ``end``: the lyric states the profile got, in order."""
        self.play()
        self.p.set_break_source(_breaks((0.0, 21.0), (23.0, end)))
        self.p.set_lyric_timeline(([21.0, end], ["one", "two"]))
        self.listen(end + 1.0)
        self.assert_budget()
        when = [t for t, _k, kw in self.client.writes if kw.get("state") == "♪ two"][0]
        self.assertLess(when - 1000.0, end, "the line after the break came late")
        return [s for s in self.client.states() if s.startswith("♪")]

    def test_a_long_break_fills_one_dot_at_a_time(self) -> None:
        self.assertEqual(self._break(55.0), ["♪ one", *self.STAGES, "♪ two"])

    def test_a_medium_break_goes_from_one_to_all_three(self) -> None:
        self.assertEqual(self._break(38.0),
                         ["♪ one", self.STAGES[0], self.STAGES[2], "♪ two"])

    def test_a_break_with_room_for_one_write_shows_the_full_row(self) -> None:
        self.assertEqual(self._break(33.0), ["♪ one", self.STAGES[2], "♪ two"])

    def test_a_break_too_short_for_its_write_keeps_the_line(self) -> None:
        # right after a line the next write is 5 s off; a 7 s break would
        # spend it on dots and make the line after late, so nothing goes
        self.assertEqual(self._break(30.0), ["♪ one", "♪ two"])

    def test_a_short_break_is_not_worth_a_write(self) -> None:
        self.play()
        self.p.set_break_source(_breaks((23.0, 27.0)))
        self.p.set_lyric_timeline(([21.0, 27.0], ["one", "two"]))
        self.listen(28)
        self.assertFalse(set(self.STAGES) & set(self.client.states()))

    def test_the_intro_keeps_the_artist(self) -> None:
        self.play()
        self.p.set_break_source(_breaks((0.0, 21.0)))
        self.p.set_lyric_timeline(([21.0], ["one"]))
        self.listen(15)
        self.assertFalse(set(self.STAGES) & set(self.client.states()))
        self.assertEqual(self.client.states()[-1].lower(), "radiohead · ok computer")

    def test_a_pause_in_a_break_doesnt_skip_ahead(self) -> None:
        # a write that isn't the dots' own (pause, then play) keeps them
        self.play()
        self.p.set_break_source(_breaks((0.0, 21.0), (23.0, 55.0)))
        self.p.set_lyric_timeline(([21.0, 55.0], ["one", "two"]))
        self.listen(25)
        self.p._on_state_changed(PlayState.PAUSED)
        self.listen(0.5)
        self.p._on_state_changed(PlayState.PLAYING)
        self.listen(31)
        lyric = [s for s in self.client.states() if s.startswith("♪")]
        stages = [s for s in lyric if s in self.STAGES]
        self.assertEqual(stages, sorted(stages, key=self.STAGES.index))
        self.assertEqual(lyric[-1], "♪ two")
        self.assert_budget()

    def test_no_break_source_no_dots(self) -> None:
        # tide 3.2.0 and older: the plugin works as it did
        self.play()
        self.p.set_lyric_timeline(([21.0, 40.0], ["one", "two"]))
        self.listen(30)
        self.assertEqual(self.client.states()[-1], "♪ one")


class BudgetTests(_Case):
    def test_pause_still_lands_while_lyrics_are_dense(self) -> None:
        self.play()
        self.p.set_lyric_timeline(([n * 0.7 for n in range(60)],
                                   [f"l{n}" for n in range(60)]))
        self.listen(9)
        self.p._on_state_changed(PlayState.PAUSED)
        paused_at = self.clock.now
        self.listen(5)
        clears = [t for t, kind, _kw in self.client.writes if kind == "clear"]
        self.assertTrue(clears)
        # the reserved write: no waiting out a 20 s window
        self.assertLess(clears[0] - paused_at, discord_rpc.MIN_PUSH_INTERVAL_S + 0.1)
        self.assert_budget()

    def test_skipping_fast_never_breaks_the_budget(self) -> None:
        self.play()
        for _ in range(12):
            self.p._on_current_changed(_track())
            self.p._on_state_changed(PlayState.PLAYING)
            self.listen(0.5)
        self.listen(25)
        self.assert_budget()
        self.assertEqual(self.client.writes[-1][2].get("details").lower(), "let down")


class ShapeTests(_Case):
    def _last(self) -> dict:
        return [kw for _t, kind, kw in self.client.writes if kind == "update"][-1]

    def test_member_list_shows_the_song(self) -> None:
        self.play()
        self.assertEqual(self._last()["status_display_type"],
                         discord_rpc.StatusDisplayType.DETAILS)
        self.p.set_options(status_display="app")
        self.listen(3)
        self.assertNotIn("status_display_type", self._last())

    def test_links_and_button(self) -> None:
        self.play(_track(artists=[{"name": "Radiohead", "id": "UC123"}],
                         album={"name": "OK Computer", "id": "MPREb_9"}))
        kw = self._last()
        self.assertEqual(kw["details_url"], "https://music.youtube.com/watch?v=vid1")
        self.assertEqual(kw["state_url"], "https://music.youtube.com/channel/UC123")
        self.assertEqual(kw["large_url"], "https://music.youtube.com/browse/MPREb_9")
        self.assertEqual(kw["buttons"][0]["url"], kw["details_url"])
        self.assertLessEqual(len(kw["buttons"][0]["label"]), 32)

    def test_no_artist_link_on_a_lyric_line(self) -> None:
        self.play(_track(artists=[{"name": "Radiohead", "id": "UC123"}]))
        self.p.set_lyric_timeline(([0.0], ["a line"]))
        self.listen(3)
        self.assertEqual(self._last()["state"], "♪ a line")
        self.assertNotIn("state_url", self._last())

    def test_local_files_get_no_links(self) -> None:
        self.play(_track(source="local"))
        kw = self._last()
        for key in ("details_url", "state_url", "large_url", "buttons"):
            self.assertNotIn(key, kw)

    def test_soundcloud_links_its_permalink(self) -> None:
        self.play(_track(source="soundcloud"))
        self.assertEqual(self._last()["details_url"], "https://soundcloud.com/a/b")

    def test_links_can_be_switched_off(self) -> None:
        self.p.set_options(links=False)
        self.play()
        self.assertNotIn("buttons", self._last())

    def test_a_client_refusing_newer_fields_still_gets_presence(self) -> None:
        self.client.refuse_newer = True
        self.play()
        kw = self._last()
        self.assertEqual(kw["details"].lower(), "let down")
        self.assertNotIn("buttons", kw)
        self.listen(3)
        self.p._on_current_changed(_track())
        self.p._on_state_changed(PlayState.PLAYING)
        self.listen(3)
        self.assertNotIn("status_display_type", self._last())


class PrivacyTests(_Case):
    def test_hiding_clears_and_stays_quiet(self) -> None:
        self.play()
        self.p.set_lyric_timeline(([n * 1.0 for n in range(20)],
                                   [f"l{n}" for n in range(20)]))
        self.listen(3)
        self.p.set_hidden(True)
        self.listen(3)
        before = len(self.client.writes)
        self.assertEqual(self.client.writes[-1][1], "clear")
        self.listen(15)
        self.p._on_current_changed(_track())
        self.listen(3)
        self.assertEqual(len(self.client.writes), before)

    def test_unhiding_brings_it_back(self) -> None:
        self.play()
        self.p.set_hidden(True)
        self.listen(3)
        self.p.set_hidden(False)
        self.listen(3)
        self.assertEqual(self.client.writes[-1][1], "update")

    def test_local_files_can_stay_private(self) -> None:
        self.p.set_options(hide_local=True)
        self.play(_track(source="local"))
        self.listen(3)
        self.assertEqual([k for _t, k, _kw in self.client.writes], [])
        self.p._on_current_changed(_track())
        self.p._on_state_changed(PlayState.PLAYING)
        self.listen(3)
        self.assertEqual(self.client.writes[-1][1], "update")

class CensorTests(_Case):
    def test_masks_whole_words_keeping_the_first_letter(self) -> None:
        c = discord_rpc.censor
        self.assertEqual(c("what the fuck"), "what the f***")
        self.assertEqual(c("Shit happens"), "S*** happens")
        self.assertEqual(c("SHIT"), "S***")
        self.assertEqual(c("fucking shitty bitches"), "f****** s***** b******")
        self.assertEqual(c("fuckin' right, fuckin\u2019 left"), "f*****' right, f*****\u2019 left")
        self.assertEqual(c("clean line"), "clean line")

    def test_leaves_words_that_only_contain_one(self) -> None:
        c = discord_rpc.censor
        for text in ("Scunthorpe", "Dickens and Hancock", "a classic assassin",
                     "shiitake", "peacock feathers"):
            self.assertEqual(c(text), text)

    def test_length_is_unchanged(self) -> None:
        text = "motherfucker, you bullshit bitch"
        self.assertEqual(len(discord_rpc.censor(text)), len(text))

    def _lyric_states(self, **opts) -> list[str]:
        self.p.set_options(**opts)
        self.play()
        self.p.set_lyric_timeline(([21.0, 21.5, 40.0], ["oh shit", "damn fucking right", "clean"]))
        self.listen(42)
        return [s for s in self.client.states() if s.startswith("\u266a")]

    def test_setting_on_masks_the_lyric_even_when_merged(self) -> None:
        states = self._lyric_states(censor=True)
        shown = " / ".join(s.removeprefix("\u266a ") for s in states)
        self.assertIn("oh s***", shown)
        self.assertIn("damn f****** right", shown)
        self.assertNotIn("shit", shown)
        self.assertNotIn("fuck", shown)

    def test_setting_off_sends_the_lyric_as_sung(self) -> None:
        states = self._lyric_states(censor=False)
        shown = " / ".join(s.removeprefix("\u266a ") for s in states)
        self.assertIn("oh shit", shown)
        self.assertIn("damn fucking right", shown)

    def test_song_title_is_not_censored(self) -> None:
        self.p.set_options(censor=True)
        self.play(dataclasses.replace(_track(), title="Shit Show"))
        self.assertIn("Shit Show", self.client.writes[-1][2].get("details", ""))



if __name__ == "__main__":
    unittest.main()


class PluginTests(unittest.TestCase):
    """the plugin under a real host: the tray item and the status line.
    no app id, so nothing ever talks to a real discord."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from tests._support import FakePlayer, FakeQueue
        from tide.plugin_host import PluginHost
        _app()
        tmp = Path(tempfile.mkdtemp(prefix="tide-discord-"))
        self.tray = SimpleNamespace(actions=[])
        self.tray.add_plugin_action = self.tray.actions.append
        self.tray.remove_plugin_action = self.tray.actions.remove
        window = SimpleNamespace(_tray=self.tray)
        self.host = PluginHost(window, FakePlayer(), FakeQueue(), roots=[(PLUGINS_DIR, "store")],
                               state_file=tmp / "p.toml", settings_dir=tmp / "s",
                               stamp=tmp / "stamp", data_dir=tmp / "d")
        self.addCleanup(self.host.shutdown)

    @unittest.skipUnless(discord_rpc.PYPRESENCE_AVAILABLE, "needs pypresence")
    def test_tray_item_and_status(self) -> None:
        self.assertTrue(self.host.set_enabled("discord", True),
                        self.host.record("discord").status)
        rec = self.host.record("discord")
        self.assertEqual(rec.detail, "paste your app id below to start")
        self.assertEqual([a.text() for a in self.tray.actions], ["hide from discord"])
        self.tray.actions[0].setChecked(True)
        self.assertTrue(rec.settings["hidden"], "the tray switch is saved")
        self.host.set_enabled("discord", False)
        self.assertEqual(self.tray.actions, [], "the tray item goes with the plugin")
