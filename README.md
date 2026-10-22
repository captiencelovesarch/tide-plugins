# tide plugins

plugins for [tide](https://github.com/captiencelovesarch/tide), the youtube music client. this repo holds the official ones, the signed index tide's plugin store reads, and the docs for writing your own.

| plugin | what it does | needs |
|---|---|---|
| [steamvr](steamvr/) | a watch on your wrist opens tide in vr: the song, lyrics, queue, search, your library | steamvr and a headset |
| [lights](lights/) | your rgb lights pulse with the bass in the cover's colours. openrgb and wled | openrgb with its sdk server on |
| [kde desktop](kde/) | plasma's accent colour follows the song, and the wallpaper too if you like | kde plasma 6 |
| [stream overlay](overlay/) | what's playing as a page for obs. not in the store yet | |

to install one, open tide and go to **plugins** (the button above settings). install, and it's running. uninstall, and it's gone.

---

- [writing a plugin](#writing-a-plugin)
  - [the smallest plugin](#the-smallest-plugin)
  - [where plugins live](#where-plugins-live)
  - [plugin.toml](#plugintoml)
  - [settings](#settings)
  - [the plugin class](#the-plugin-class)
  - [ctx](#ctx)
  - [tide.plugin_api](#tideplugin_api)
  - [python dependencies](#python-dependencies)
  - [when things go wrong](#when-things-go-wrong)
  - [rules](#rules)
  - [trust](#trust)
- [getting into the store](#getting-into-the-store)
  - [store.toml](#storetoml)
  - [banners and icons](#banners-and-icons)
- [this repo](#this-repo)

---

## writing a plugin

a plugin is a folder with a `plugin.toml` and some python. tide imports it, makes one instance of a class you name, and calls `start(ctx)`. everything you need from tide comes through `ctx`. when the plugin is switched off, tide calls `stop()` and undoes everything you connected through `ctx`, even what you forgot.

this is api 1, which tide 3.0 speaks.

### the smallest plugin

[`examples/now-playing-file/`](examples/now-playing-file/) writes the song that's playing to a text file, for an obs text source or a status bar. it's the whole shape of a plugin in about 40 lines.

`plugin.toml`:

```toml
[plugin]
slug        = "now-playing-file"
name        = "now playing file"
description = "writes the song that's playing to a text file, for an obs text source or a status bar."
version     = "0.1.0"
api         = 1
entry       = "plugin:NowPlayingFile"
uses        = ["playback", "files"]

[[setting]]
key     = "format"
label   = "line"
kind    = "str"
default = "{artists} - {title}"
tooltip = "{title}, {artists} and {album} are filled in."

[[setting]]
key     = "when_paused"
label   = "when paused"
kind    = "choice"
choices = [["keep", "keep the song"], ["clear", "empty the file"]]
default = "keep"

[[setting]]
key   = "open_folder"
label = "open the folder"
kind  = "action"
```

`plugin.py`:

```python
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
```

to try it:

```sh
mkdir -p ~/.config/tide/plugins
cp -r examples/now-playing-file ~/.config/tide/plugins/
```

start tide, open **plugins**, find "now playing file" (marked "not from the store") and press **turn on**. its settings show on its page and apply as you change them. the file is `~/.local/share/tide/plugins/now-playing-file/now-playing.txt`.

### where plugins live

tide looks in three places, in this order. a later one wins when two have the same slug.

| folder | what | starts |
|---|---|---|
| `~/.local/share/tide/plugins/installed/` | what the store installed. don't edit these, an update replaces them | on, once installed |
| `/usr/share/tide/plugins/` | plugins from a distro package | off until you turn it on |
| `~/.config/tide/plugins/` | yours | off until you turn it on |

a folder whose name starts with `_` or `.` is skipped.

tide reads these folders when it starts. a new folder shows up after a restart. after you change a plugin's python, turn it off and on again: tide forgets its modules and imports them fresh. a change to `plugin.toml` needs a restart. native extensions stay loaded until tide quits, that's python.

if you keep a plugin of yours with the same slug as a store plugin, yours wins and the store one does nothing. handy for working on an official plugin: clone this repo and symlink the folder into `~/.config/tide/plugins/`.

### plugin.toml

`[plugin]`:

| key | | |
|---|---|---|
| `slug` | | lowercase letters, digits and `-`. defaults to the folder's name. the store won't take `installed` |
| `name` | required | what people see. lowercase, like the rest of tide |
| `description` | required | a sentence or two |
| `version` | required | `major.minor.patch`. the store compares these for updates |
| `api` | required | `1`. a plugin built for another api is listed as "built for a different tide" and won't start |
| `entry` | required | `module:Class`. the module is a file in your folder, `plugin:MyPlugin` means `plugin.py` |
| `uses` | | what your plugin touches, from `playback`, `queue`, `lyrics`, `audio`, `network`, `files`, `ui`, `vr`. shown to people before they install. tide doesn't enforce it, so be honest |
| `requires` | | python modules the system has to provide, like `["openvr"]`. see [python dependencies](#python-dependencies) |

a broken `plugin.toml` doesn't stop tide, but the plugin isn't listed and tide 3.0 doesn't say why. `tools/check.py` does:

```sh
python tools/check.py ~/.config/tide/plugins/my-plugin
```

it reads the folder with tide's own parser, imports your entry class and checks `requires`.

### settings

each `[[setting]]` is one control on the plugin's page. tide stores the values in `~/.config/tide/plugin-settings/<slug>.toml` (0600) and builds the form for you.

| key | | |
|---|---|---|
| `key` | required | lowercase letters, digits and `_`. unique in the plugin |
| `label` | required | the control's label |
| `kind` | required | `bool`, `choice`, `int`, `str` or `action` |
| `default` | required, except for `action` | has to fit the kind. a choice's default has to be one of its values |
| `choices` | `choice` only | a list of values, or of `[value, label]` pairs. values can be strings, numbers or booleans |
| `tooltip` | | a line of help on hover |
| `secret` | `str` only | hides what's typed, for tokens |
| `help_url` | | adds a "get one →" button next to the control that opens this link, for "where do i get an api key" |

what each kind looks like: `bool` a checkbox, `choice` a dropdown, `int` a number box, `str` a text field (applied when you leave it), `action` a button.

reading and writing them:

```python
ctx.settings["format"]              # the current value
ctx.settings.set("format", "x")     # saves it. raises ValueError if it doesn't fit
ctx.settings_changed.connect(fn)    # fn(keys): the set of keys the user just changed
```

a key your `plugin.toml` doesn't declare raises `KeyError`, so a plugin can't keep a setting nobody can see. `settings.set()` doesn't fire `settings_changed`, only the user's changes do. a value someone edited into the file by hand that doesn't fit is ignored and the default used.

an `action` has no value. its button calls `on_action(key)` on your plugin, and is greyed out while the plugin is off.

### the plugin class

```python
class MyPlugin:
    def start(self, ctx): ...       # required
    def stop(self): ...             # optional
    def on_action(self, key): ...   # optional, for action settings
```

tide calls your class with no arguments, then `start(ctx)`. keep `__init__` empty and do the work in `start`.

- **start** raising means the plugin didn't start. its status shows `crashed: <the error>`, tide shows a toast, and whatever you'd connected is undone.
- **stop** runs when the user turns the plugin off, uninstalls or updates it, or quits tide. stop your threads, close sockets, put back anything you changed outside tide (the kde plugin puts your colours back here). after `stop()`, tide runs your `ctx.on_stop` callbacks, deletes what you `adopt`ed, drops every signal connection made through `ctx`, and removes your modules from `sys.modules`.

your own modules are a package. import them relatively: `from . import helpers`, `from .show import Show`. `import helpers` won't find them.

### ctx

everything here is for the gui thread: call it from there, and its signals and callbacks arrive there. connect to `ctx`'s signals, never to tide's own objects, so tide can undo your connections.

#### the plugin itself

| | |
|---|---|
| `ctx.slug` | your slug |
| `ctx.manifest` | your parsed `plugin.toml` (`.name`, `.version`, `.path` and so on) |
| `ctx.tide_version` | tide's version, a string |
| `ctx.settings` | see [settings](#settings) |
| `ctx.settings_changed` | signal(set of keys) |
| `ctx.set_status(text)` | a short line under your plugin's name on its page: "waiting for steamvr", "connected". 120 characters at most |
| `ctx.log` | a `logging.Logger` named `tide.plugin.<slug>`. goes to tide's log |
| `ctx.on_stop(fn)` | `fn()` runs after your `stop()` |
| `ctx.adopt(obj)` | hand tide a qt object with no parent (a widget, a timer). it's deleted right after `stop()`. returns `obj` |
| `ctx.paths.data_dir` | `~/.local/share/tide/plugins/<slug>/`, made 0700 when you first ask |
| `ctx.paths.cache_dir` | `~/.cache/tide/plugins/<slug>/`, same |

use `adopt` for every parentless qt object you make. without it, a lambda connected to one of its signals keeps it alive until python exits, when deleting it crashes.

#### ctx.playback

| | |
|---|---|
| `track_changed` | signal(`TrackInfo` or `None`) |
| `state_changed` | signal(`"idle"`, `"loading"`, `"playing"` or `"paused"`) |
| `position_changed` | signal(seconds, float) |
| `duration_changed` | signal(seconds, float) |
| `speed_changed` | signal(rate, float) |
| `ended` | signal(), the song reached its end |
| `liked_changed` | signal(bool) |
| `current()` | the `TrackInfo` playing now, or `None` |
| `state()`, `position()`, `duration()` | the same as the signals, read now |
| `play()`, `pause()`, `toggle()`, `next()`, `previous()` | like the buttons |
| `seek(seconds)` | |
| `liked()`, `like(on=True)` | the current song's like, on your account |
| `speed()`, `set_speed(rate)` | 0.5 to 2.0, snapped to tide's 0.05 steps |
| `speed_supported()` | false on sources that can't change speed |
| `volume()`, `set_volume(percent)` | through tide's own volume, so the bar and the saved volume agree |
| `duck(fraction)`, `unduck()` | drop the music to a fraction of its volume for a moment without touching the saved volume (while someone talks) |

#### ctx.queue

| | |
|---|---|
| `changed` | signal(), songs were added, removed or moved |
| `modes_changed` | signal(), shuffle or repeat changed |
| `tracks()` | a list of `TrackInfo` |
| `current_index()` | -1 when empty |
| `play_at(index)` | |
| `up_next()` | the next `TrackInfo`, or `None` |
| `shuffle()`, `set_shuffle(on)` | |
| `repeat()`, `cycle_repeat()` | `"off"`, `"all"` or `"one"`. cycling goes off → all → one, like the button |

#### ctx.music

search and the library, through whatever source is active (youtube music, usually). lookups run off the gui thread and answer on it. `on_error` gets the exception; a source that can't do something raises `NotSupportedError`.

| | |
|---|---|
| `search(query, kind, on_done, on_error=None, limit=30)` | `kind`: `songs`, `albums`, `artists` or `playlists`. songs come back as `TrackInfo`, the rest as `ItemInfo` |
| `library(section, on_done, on_error=None)` | `section`: `playlists`, `liked`, `albums` or `artists` |
| `open(item, on_done, on_error=None)` | an `ItemInfo`'s songs: an album's or playlist's tracks, an artist's top songs |
| `play_now(track, radio=None)` | play one song. `radio`: keep going with songs like it. `None` does what tide's search does |
| `radio(track)` | start a radio from a song |
| `play_next(track)` | put it after the current song |
| `play(tracks, start=0, shuffle=False)` | replace the queue |
| `play_at(track, seconds)` | play a song and jump to `seconds` as soon as it plays, for a swap that feels like the same song carrying on |
| `search_all(query, limit=10)` | `(source name, TrackInfo)` pairs from every source that's switched on. it blocks: call it inside `ctx.jobs.run` |

only hand back `TrackInfo`s that tide gave you. they carry tide's own record of the song, which is what gets played.

#### ctx.track

| | |
|---|---|
| `current()` | same as `ctx.playback.current()` |
| `art(url_or_track, callback)` | the cover as a `QImage` (or `None`): right away if it's cached, later if not. never called after your plugin stops |

#### ctx.lyrics

nothing is fetched until a plugin subscribes. tide shares one lyrics tracker between everything that asks.

| | |
|---|---|
| `subscribe()`, `unsubscribe()` | tide unsubscribes you when you stop |
| `changed` | signal(`(times, lines)` or `None`), the whole timed sheet for a new song |
| `line_changed` | signal(the line being sung, or `None`) |
| `sheet()` | the current `(times, lines)`, or `None` |
| `lines_around(position)` | `(before, now, next)` at `position` seconds. all `None` without timed lyrics |
| `add_aligner(fn)` | precise word timing. `fn(sheet, stream, should_stop, say)` gets tide's word sheet (a list of `WordLine`) and the `StreamRef` playing, and returns a better sheet or `None`. it runs off the gui thread, after the lyrics' own word timing. precise lyric timing uses it |
| `remove_aligner()` | take it away again (tide does when you stop) |

#### ctx.beats

| | |
|---|---|
| `subscribe()`, `unsubscribe()` | |
| `pulse` | signal(0..1), tide's bass envelope, about 60 a second while music plays. the same pulse the modern backdrop moves with, whether or not the backdrop is on |

feed it through `Envelope`, `Kicks` and `FlashLimiter` from [`tide.plugin_api`](#tideplugin_api) so a beat looks the same in your plugin as in tide.

#### ctx.theme

| | |
|---|---|
| `changed` | signal(), the theme, fonts or type roles changed |
| `token(name, default="")` | a theme colour as `#rrggbb`: `bg`, `bg_alt`, `fg`, `dim`, `accent`, `accent_alt`, `border_col`, `hover_bg`, `sel_bg`, `sel_fg` and more |
| `dark()` | |
| `aesthetic()` | `"brutalist"` or `"modern"` |
| `font()` | `(family, is_mono)` of the ui font |
| `fonts()` | the user's type roles: `ui`, `title` and `numbers`, each `{family, weight, size, spacing}` |
| `case(text)` | text in the theme's letter case |
| `icon(key, px, color="")` | one of tide's icons from the user's icon set, as a `QPixmap` |
| `glyph(key)` | one of tide's control glyphs (`play`, `pause`, `next`, `like_on`...), from the user's glyph pack |

#### ctx.motion

animate the way tide does, so "motion off" means nothing of yours moves either.

| | |
|---|---|
| `intensity()` | `"off"`, `"lite"` or `"full"` (reduced motion turns full into lite) |
| `off()` | |
| `profile()` | `"mechanical"` or `"springy"` |
| `dur(key)` | milliseconds for `micro`, `short`, `med` or `long` |
| `ease(key)` | a `QEasingCurve` for `linear`, `out`, `out_strong`, `in_out` or `spring` |

#### ctx.jobs

| | |
|---|---|
| `run(fn, on_done=None, on_error=None)` | `fn()` on a thread of its own. `on_done(result)` or `on_error(exception)` arrive on the gui thread, and not at all after your plugin stops. when you stop, tide waits up to a second for jobs still running |

use this, or a thread you own and stop in `stop()`, for anything that blocks: network, disk, subprocesses.

#### ctx.ui

| | |
|---|---|
| `toast(text, action_label=None, on_action=None)` | a toast in tide's window, with an optional button |
| `add_tray_action(label, fn, checkable=False, checked=False)` | an item in the tray menu, above quit. returns a handle with `set_label`, `set_checked` (without calling you back), `checked` and `set_visible` |
| `add_visualizer_tile(kind, label, factory, knobs=())` | a tile any visualizer slot can show. `factory(engine)` builds one: subclass `tide.plugin_vis.PlayTile` and draw in `paint_graphics(p, w, h, f)`. `knobs` are choice settings of yours that the tile's menu offers, and `self.knob(key, default)` reads them. a tile can add rows to its menu with `menu_actions()` → `[(label, fn)]` |
| `add_lyrics_toggle(label, fn)` | a `[label]` toggle in the lyrics view's row (karaoke, sweep...). `fn(on, track)` on a click. returns a handle with `set_checked(on)` and `set_status(text)`, one line under the toggles |
| `add_settings_button(section, label, fn, tooltip="")` | a button in tide's settings dialog, in `"power tools"` or `"shortcuts"`. `fn(dialog)` runs a turn after the click, so you can open a window of your own over it |

everything you add goes when your plugin stops. there's no hook yet for a page of your own, a player bar control or a keyboard shortcut. a window of your own works (adopt it).

#### ctx.sources

a music source of your own: search, the library, playback. it sits in tide's source panel with its own switch, like youtube music. extra sources and local files are built on it.

| | |
|---|---|
| `register(source, on=True)` | add a `MusicSource` (from `tide.plugin_api`). `on`: whether it starts switched on when tide has never seen it; after that the user's switch is kept. if it was the active source last time, it is again |
| `is_on(slug)` | is that source switched on? |

a source with a `configure(parent)` method gets the gear in the source panel (local files' folder picker). tide takes your sources away when you stop.

#### ctx.speech

push-to-talk speech to text, on this computer, with whisper. steamvr's voice search uses it.

| | |
|---|---|
| `available()` | `(ok, why_not)`. needs onnxruntime and a ~77 mb model |
| `model_size_mb()` | |
| `download(on_progress=None, on_done=None, on_error=None)` | fetch the model. `on_progress(done, total)` may come from the download thread |
| `listen()`, `listening()`, `seconds()` | open the default mic and record |
| `finish(on_done, on_error=None)` | stop and transcribe. `on_done(text)`, `""` for nothing said |
| `cancel()` | stop without transcribing |

### tide.plugin_api

the one tide module you may import. everything else under `tide.*` can change in any release.

| | |
|---|---|
| `API_VERSION` | `1` |
| `TrackInfo` | a song: `id`, `title`, `artists`, `album`, `duration` (seconds, 0 when unknown), `source`, `link` (a web page for it, or `""`), `art_url`, and `as_dict()` |
| `ItemInfo` | an album, playlist or artist: `kind`, `id`, `title`, `subtitle`, `art_url` |
| `FlashLimiter(per_second=3, jump=0.3)` | `update(now, level)` → the level, with no more than `per_second` flashes a second. past the limit a hit still shows, just under a flash |
| `Envelope(release_s=0.03)` | `update(now, peak)` → the pulse at your own frame rate, up at once and a quick glide down, like tide's backdrop. feed it every frame |
| `Kicks()` | `update(now, pulse)` → `"full"`, `"soft"` or `None`: which pulses are kicks, 3 full ones a second at most |
| `glow_level(mode, pulse, playing)` | how bright a glow is for a pulse, for `mode` in `PULSE_MODES` (`off`, `kick`, `breathing`) |
| `STRENGTH` | `{"subtle": 0.45, "normal": 0.7, "loud": 1.0}`, for a strength setting |
| `cover_colors(qimage)` | two rgb 0..1 tuples from a cover, lifted to be shown as light. a black-and-white cover gives white |
| `cover_palette(qimage)` | up to three `((h, s, v), share)` from a cover, biggest first |
| `MusicSource`, `Track`, `StreamRef`, `NotSupportedError`, the `*Entry` / `*Detail` / `Shelf` types | for a source of your own (`ctx.sources`). `resolve_stream` hands back a `StreamRef(backend="mpv", payload=url_or_path)` |
| `stream_cache_get(source, key)`, `stream_cache_put(source, key, url, ttl_seconds)`, `STREAM_NEVER_EXPIRES` | resolved urls kept across launches |
| `store_art(key, data)` | a cover you read from a file, kept in tide's art cache. returns the url to put in `Track.thumbnail` |
| `Word`, `WordLine`, `shift_line`, `split_words` | for an aligner (`ctx.lyrics.add_aligner`) |
| `TrackInfo.artist_link`, `TrackInfo.album_link` | the first artist's and the album's pages, or `""` |

for a visualizer tile there's `tide.plugin_vis`: `PlayTile`, `CanvasTile`, `Frame`, `VisEngine`, and the helpers tide's own tiles draw with (`alpha`, `mix`, `bloom`, `paint_outline`, `styled_case`).

`now` is seconds from `time.monotonic()`.

### python dependencies

you can import python's standard library and what tide itself depends on: PySide6, numpy and cryptography. beyond that:

- **carry it.** put pure python modules, or the contents of wheels that work on any python (`py3-none-any`, like pyopenvr), in a `vendor/` folder in your plugin. tide puts that folder first on `sys.path` while your plugin runs and takes it, and every module imported from it, off when it stops. the store's publish script fills `vendor/` from your `store.toml`, so you don't commit it.
- **ask for it.** list it in `requires`. if it isn't installed, your plugin is listed with "needs <module>" and can't be turned on. for things a distro packages better than you can carry.

don't vendor something tide imports (PySide6, numpy), and keep in mind two plugins carrying two versions of one module will clash.

programs and hardware (openrgb, steamvr) aren't python dependencies. check for them in `start()`, say what's missing with `ctx.set_status`, and for store plugins list them as [needs](#storetoml).

### when things go wrong

- an uncaught exception in your code (a slot, a timer, a thread) is counted against your plugin and written with its traceback to `~/.local/share/tide/plugins/<slug>/errors.log`. **five inside ten seconds and tide turns your plugin off** and says so in a toast.
- if your `start()` takes the whole of tide down (a native crash), tide notices on the next launch, leaves your plugin off and says so.
- `tide --no-plugins` starts tide without any.
- `ctx.log` goes to tide's log: run `tide` from a terminal to see it.

### rules

- **gui thread only** for `ctx` and qt widgets. blocking work goes through `ctx.jobs` or your own thread.
- **undo what you change outside tide** in `stop()`. a desktop colour, a light, a file: put it back.
- **nothing flashes more than three times a second.** anything that lights a room or fills someone's view goes through `FlashLimiter`. this is photosensitive seizure guidance, and the store holds every plugin to it.
- **use tide's look.** lowercase text, `ctx.theme` colours and fonts, `ctx.motion` for anything that moves.
- **say what you touch.** `uses` is shown to people before they install. don't hide network access.

### trust

a plugin is python running inside tide with your permissions. it can read your files and your youtube music sign-in, and talk to the network. tide doesn't sandbox plugins, because python can't sandbox python. `ctx` hands out no credentials, so a plugin that wants your session has to go around it, which shows in its code.

plugins from the store are read before they're listed, and their zips are checked against a signed index. plugins you add yourself are as safe as their author, same as an aur package.

---

## getting into the store

for now the store lists the official plugins. community plugins come next: you open a pull request that adds your plugin's folder with a `store.toml`, it gets read, and its banner gets made with you, like the official ones. pull requests are welcome already, they'll be listed once the store takes community plugins.

### store.toml

next to `plugin.toml`. none of it goes in the zip.

```toml
blurb    = "one line for the card."
about    = """
longer text for the plugin's page. defaults to plugin.toml's description.
"""
category = "hardware"          # sources, lyrics, visuals, hardware or tools
min_tide = "3.0.0"
added    = "2026-10-21"        # when it first went in the store
listed   = true                # false keeps it out of the index
vendor   = ["openvr==2.12.1401"]   # pip requirements for vendor/
vendor_drop = ["openvr/*.dll"] # files in vendor/ nobody on linux loads
icon     = "icon.svg"

[banner]
grid   = "store/grid.webp"     # required: no banner, no store entry
full   = "store/full.webp"
motion = "store/motion.webp"

[[need]]
what      = "kde plasma 6"
check     = "plasma"           # see below
how       = "the command or step that gets it"
hard      = true               # greys install out when it's missing
hard_note = "only works on kde plasma"

[[preview]]
kind = "image"                 # or clip, an animated webp
file = "store/screenshot.webp"
```

`check` is what tide tests before install: `steamvr`, `openrgb`, `plasma`, `program:<name>` (on `PATH`), `process:<name>` (running) or `desktop:<name>` (in `XDG_CURRENT_DESKTOP`). a need without one is shown without a ✓ or ✗.

### banners and icons

**the banner** is 1280×720 webp, in two cuts: **grid** (the background, your plugin's mark and a real capture, no text) and **full** (the same, plus "<name> × tide"). the capture is real, from your plugin running. the background is your plugin's world, dark or saturated so white text reads. **motion** is optional: the grid cut with only the capture frame moving, 4 to 8 seconds, looping without a jump, under 3 flashes a second. it plays on hover, and reduced motion shows the still.

**the icon** is a 24 px outline svg in `currentColor`, drawn like tide's own icons: `viewBox="0 0 24 24"`, `stroke-width="1.8"`, round caps and joins, no fill. tide recolours it to the theme and thickens or thins it with the user's icon set.

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round">
  ...
</svg>
```

---

## this repo

```
<slug>/                one folder per plugin. its zip is this folder minus store.toml and store/
  plugin.toml
  store.toml           what the store says about it
  icon.svg
  store/               banners, and their sources in store/src/
examples/              plugins to learn from, not in the store
tests/                 run against a tide checkout
tools/
  publish.py           build, upload and sign
  keygen.py            the store's signing key
  banners.py           rebuild the official banners
  check.py             check a plugin folder the way tide reads it
index.json             the published index
featured.toml          the plugin of the week
```

**tests** need a tide checkout:

```sh
PYTHONPATH=~/tide/src python -m pytest tests
```

**publishing** (the maintainer):

```sh
python tools/publish.py steamvr lights      # those two, then the index
python tools/publish.py --index-only        # a new pick of the week
python tools/publish.py --all --dry-run     # build and check, upload nothing
python tools/publish.py --all --local DIR   # a whole store in DIR, to point a test tide at
```

each plugin goes up as a `<slug>-<version>` release with its zip and pictures. then `index.json` is rebuilt, signed with ed25519, checked with tide's own parser and uploaded to the `index` release. a version that's out already can't be uploaded again: bump it. commit `index.json` after.

---

[tide](https://github.com/captiencelovesarch/tide) · [GPL-3.0-or-later](LICENSE) · steamvr is a trademark of valve, kde and plasma of kde e.v. not affiliated with either.
