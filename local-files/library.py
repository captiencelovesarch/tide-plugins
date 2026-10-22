"""Local files source, out of tide's core in 3.0 (it was
tide/sources/local.py).

Walks a user-chosen music directory, reads ID3/Vorbis/etc. tags via
``mutagen``, and indexes everything into a SQLite FTS5 database in the
plugin's cache folder. Search hits FTS; track resolution hands the file
path straight to mpv. Covers come from a cover.jpg (or folder.jpg...)
next to the files, else the art embedded in them, once per folder, and
go in tide's own art cache.

A ``watchdog`` filesystem observer keeps the index fresh while the app
runs — added/removed/renamed files are reflected without a full rescan.
``watchdog`` is an optdepend; if it's not installed the index is rebuilt
on every app launch instead.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
from pathlib import Path

from tide.plugin_api import (
    MusicSource,
    PlaylistDetail,
    PlaylistEntry,
    Shelf,
    ShelfItem,
    StreamRef,
    Track,
    store_art,
)


SOURCE_SLUG = "local"

SUPPORTED_EXTS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".wav", ".aiff", ".aif", ".wv", ".ape", ".mka",
}


def default_music_dir() -> str:
    """Resolve the user's preferred music directory.

    Honors ``$XDG_MUSIC_DIR`` from the user-dirs spec, then falls back to
    ``~/Music`` (Arch/KDE/GNOME convention).
    """
    env = os.environ.get("XDG_MUSIC_DIR")
    if env and Path(env).is_dir():
        return env
    user_dirs = Path.home() / ".config" / "user-dirs.dirs"
    if user_dirs.is_file():
        try:
            for line in user_dirs.read_text().splitlines():
                if line.startswith("XDG_MUSIC_DIR="):
                    raw = line.split("=", 1)[1].strip().strip('"')
                    expanded = raw.replace("$HOME", str(Path.home()))
                    if Path(expanded).is_dir():
                        return expanded
        except OSError:
            pass
    return str(Path.home() / "Music")


# ---------- tag reading ----------

def _read_tags(path: Path) -> dict | None:
    try:
        from mutagen import File as MFile
    except Exception:
        return None
    try:
        mf = MFile(str(path), easy=True)
    except Exception:
        return None
    if mf is None:
        return None
    tags = getattr(mf, "tags", None) or {}
    def _first(key: str) -> str:
        v = tags.get(key) if hasattr(tags, "get") else None
        if isinstance(v, list) and v:
            return str(v[0])
        if isinstance(v, str):
            return v
        return ""
    duration = float(getattr(mf.info, "length", 0.0) or 0.0)
    return {
        "title": _first("title") or path.stem,
        "artist": _first("artist") or _first("albumartist"),
        "album": _first("album"),
        "albumartist": _first("albumartist"),
        "year": _first("date") or _first("year"),
        "tracknumber": _first("tracknumber"),
        "duration_seconds": int(duration),
    }


# ---------- covers ----------

COVER_NAMES = ("cover", "folder", "front", "album", "albumart")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _folder_image(folder: Path) -> Path | None:
    try:
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() in COVER_EXTS and f.stem.lower() in COVER_NAMES and f.is_file():
                return f
    except OSError:
        pass
    return None


def _embedded_cover(path: Path) -> bytes | None:
    """the first picture in a file's tags: id3 APIC, flac/ogg pictures,
    mp4 covr, or a vorbis METADATA_BLOCK_PICTURE."""
    try:
        from mutagen import File as MFile
        mf = MFile(str(path))
    except Exception:
        return None
    if mf is None:
        return None
    pics = getattr(mf, "pictures", None)
    if pics:
        return bytes(pics[0].data)
    tags = getattr(mf, "tags", None)
    if tags is None:
        return None
    if hasattr(tags, "getall"):
        apic = tags.getall("APIC")
        if apic:
            return bytes(apic[0].data)
    get = getattr(tags, "get", None)
    if get is None:
        return None
    covr = get("covr")
    if covr:
        return bytes(covr[0])
    block = get("metadata_block_picture")
    if block:
        try:
            from mutagen.flac import Picture
            return bytes(Picture(base64.b64decode(block[0])).data)
        except Exception:
            return None
    return None


def cover_for(path: Path, seen: dict[str, str]) -> str:
    """the art url for ``path``'s folder ("" when it has none), worked out
    once per folder (``seen`` remembers)."""
    folder = path.parent
    key = str(folder)
    if key in seen:
        return seen[key]
    url = ""
    img = _folder_image(folder)
    try:
        data = img.read_bytes() if img is not None else _embedded_cover(path)
    except OSError:
        data = None
    if data:
        digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()
        url = store_art(f"local:{digest}", data)
    seen[key] = url
    return url


# ---------- sqlite store ----------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    path        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    artist      TEXT NOT NULL,
    album       TEXT NOT NULL,
    albumartist TEXT NOT NULL,
    year        TEXT NOT NULL,
    tracknumber TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    art         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tracks_album_idx ON tracks(album, tracknumber);
CREATE INDEX IF NOT EXISTS tracks_artist_idx ON tracks(artist);
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    path UNINDEXED, title, artist, album,
    content='tracks', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
    INSERT INTO tracks_fts(rowid, path, title, artist, album)
    VALUES (new.rowid, new.path, new.title, new.artist, new.album);
END;
CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, path, title, artist, album)
    VALUES ('delete', old.rowid, old.path, old.title, old.artist, old.album);
END;
CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, path, title, artist, album)
    VALUES ('delete', old.rowid, old.path, old.title, old.artist, old.album);
    INSERT INTO tracks_fts(rowid, path, title, artist, album)
    VALUES (new.rowid, new.path, new.title, new.artist, new.album);
END;
"""


class _LocalIndex:
    """SQLite index over the music library. Thread-safe via a single lock —
    the index is small and writes are infrequent, so coarse locking is fine."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _ensure(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def upsert(self, path: str, tags: dict, mtime: float) -> None:
        with self._lock:
            conn = self._ensure()
            conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
            conn.execute(
                "INSERT INTO tracks (path, title, artist, album, albumartist, year, "
                "tracknumber, duration_seconds, mtime, art) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    path,
                    tags.get("title") or Path(path).stem,
                    tags.get("artist") or "",
                    tags.get("album") or "",
                    tags.get("albumartist") or tags.get("artist") or "",
                    tags.get("year") or "",
                    tags.get("tracknumber") or "",
                    int(tags.get("duration_seconds") or 0),
                    float(mtime),
                    tags.get("art") or "",
                ),
            )
            conn.commit()

    def remove(self, path: str) -> None:
        with self._lock:
            conn = self._ensure()
            conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
            conn.commit()

    def known_paths(self) -> set[str]:
        return set(self.mtimes())

    def mtimes(self) -> dict[str, float]:
        with self._lock:
            conn = self._ensure()
            cur = conn.execute("SELECT path, mtime FROM tracks")
            return {row[0]: float(row[1]) for row in cur.fetchall()}

    def count(self) -> int:
        with self._lock:
            conn = self._ensure()
            cur = conn.execute("SELECT COUNT(*) FROM tracks")
            return int(cur.fetchone()[0])

    def search(self, query: str, limit: int = 50) -> list[dict]:
        if not query.strip():
            return []
        # FTS5 MATCH: quote each token to dodge punctuation pitfalls, and
        # double any embedded quote (FTS5's escape) — a query like
        # `12" remix` would otherwise terminate the string early and make
        # the whole MATCH a syntax error.
        terms = " ".join(
            '"{}"*'.format(t.replace('"', '""')) for t in query.split() if t
        )
        if not terms:
            return []
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "SELECT t.path, t.title, t.artist, t.album, t.duration_seconds, t.art "
                "FROM tracks t JOIN tracks_fts f ON f.rowid = t.rowid "
                "WHERE tracks_fts MATCH ? ORDER BY rank LIMIT ?",
                (terms, limit),
            )
            rows = cur.fetchall()
        return [
            {"path": r[0], "title": r[1], "artist": r[2], "album": r[3], "duration_seconds": r[4],
             "art": r[5]}
            for r in rows
        ]

    def recent(self, limit: int = 30) -> list[dict]:
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "SELECT path, title, artist, album, duration_seconds, art FROM tracks "
                "ORDER BY mtime DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
        return [
            {"path": r[0], "title": r[1], "artist": r[2], "album": r[3], "duration_seconds": r[4],
             "art": r[5]}
            for r in rows
        ]

    def all_tracks(self, limit: int = 5000) -> list[dict]:
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "SELECT path, title, artist, album, duration_seconds, art FROM tracks "
                "ORDER BY albumartist, album, tracknumber, title LIMIT ?", (limit,))
            rows = cur.fetchall()
        return [
            {"path": r[0], "title": r[1], "artist": r[2], "album": r[3], "duration_seconds": r[4],
             "art": r[5]}
            for r in rows
        ]

    def albums(self) -> list[dict]:
        """List unique (albumartist, album) pairs with track counts."""
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "SELECT COALESCE(NULLIF(albumartist, ''), artist) AS aa, album, "
                "COUNT(*) AS n, MAX(year) AS year, MAX(art) AS art "
                "FROM tracks WHERE album != '' "
                "GROUP BY aa, album ORDER BY aa, album"
            )
            rows = cur.fetchall()
        return [{"albumartist": r[0], "album": r[1], "count": r[2], "year": r[3] or "",
                 "art": r[4] or ""} for r in rows]

    def album_tracks(self, albumartist: str, album: str) -> list[dict]:
        with self._lock:
            conn = self._ensure()
            cur = conn.execute(
                "SELECT path, title, artist, album, duration_seconds, tracknumber, art "
                "FROM tracks WHERE album = ? AND "
                "COALESCE(NULLIF(albumartist, ''), artist) = ? "
                "ORDER BY tracknumber, title",
                (album, albumartist),
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "path": r[0], "title": r[1], "artist": r[2], "album": r[3],
                "duration_seconds": r[4], "tracknumber": r[5] or "", "art": r[6] or "",
            })
        return out


# ---------- scanning ----------

def _scan_dir(root: Path, index: _LocalIndex, on_progress=None, stop=lambda: False) -> int:
    """Walk ``root``, upsert any new or mtime-changed files, drop missing ones.
    Returns total tracks indexed afterwards."""
    if not root.is_dir():
        return 0
    seen: set[str] = set()
    known_mtimes = index.mtimes()
    covers: dict[str, str] = {}
    n = 0
    for dirpath, _, filenames in os.walk(root):
        if stop():
            return index.count()            # the plugin is going: leave the rest
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in SUPPORTED_EXTS:
                continue
            p = Path(dirpath) / name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            seen.add(str(p))
            n += 1
            if known_mtimes.get(str(p)) == mtime:
                continue                    # unchanged since the last scan
            tags = _read_tags(p)
            if not tags:
                continue
            tags["art"] = cover_for(p, covers)
            index.upsert(str(p), tags, mtime)
            if on_progress and (n % 50 == 0):
                on_progress(n)
    # Drop stale entries (files outside ``root`` or no longer present).
    known = index.known_paths()
    for stale in known - seen:
        if stale.startswith(str(root)):
            try:
                if not Path(stale).is_file():
                    index.remove(stale)
            except OSError:
                index.remove(stale)
        else:
            index.remove(stale)
    return index.count()


# ---------- watchdog (optional) ----------

class _Watcher:
    """Wraps a watchdog observer. Silent no-op if watchdog isn't installed."""

    def __init__(self, root: str, index: _LocalIndex) -> None:
        self.root = root
        self.index = index
        self.observer = None

    def start(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            return  # graceful fallback

        idx = self.index

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory:
                    return
                self._touch(event.src_path)
            def on_modified(self, event):
                if event.is_directory:
                    return
                self._touch(event.src_path)
            def on_deleted(self, event):
                if event.is_directory:
                    return
                idx.remove(event.src_path)
            def on_moved(self, event):
                if event.is_directory:
                    return
                idx.remove(event.src_path)
                self._touch(event.dest_path)
            def _touch(self, path: str) -> None:
                ext = os.path.splitext(path)[1].lower()
                if ext not in SUPPORTED_EXTS:
                    return
                p = Path(path)
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    return
                tags = _read_tags(p)
                if tags:
                    tags["art"] = cover_for(p, {})
                    idx.upsert(path, tags, mtime)

        observer = Observer()
        observer.schedule(_Handler(), self.root, recursive=True)
        observer.daemon = True
        try:
            observer.start()
        except Exception:
            return
        self.observer = observer

    def stop(self) -> None:
        if self.observer is None:
            return
        try:
            self.observer.stop()
            self.observer.join(timeout=2)
        except Exception:
            pass
        self.observer = None


# ---------- source ----------

class LocalSource(MusicSource):
    slug = SOURCE_SLUG
    name = "local files"
    icon = "local"
    needs_auth = False
    backend_slug = "mpv"
    short_tag = "LO"
    capabilities = frozenset({"home", "library", "albums", "artists"})

    def __init__(self, music_dir: str | None = None, *, index_path: Path) -> None:
        self._music_dir = music_dir or default_music_dir()
        self._index = _LocalIndex(index_path)
        self._watcher: _Watcher | None = None
        self._scan_in_progress = False
        self._stopping = False
        # the source panel's gear calls this; plugin.py sets it (a folder
        # picker), None hides nothing, it just does nothing
        self.configure = None

    # ---------- config ----------

    @property
    def music_dir(self) -> str:
        return self._music_dir

    def set_music_dir(self, path: str | None) -> None:
        """Switch the indexed directory ("" or None: the default one).
        Caller should follow up with rescan()."""
        self._music_dir = path or default_music_dir()
        self.stop_watcher()

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def track_count(self) -> int:
        return self._index.count()

    def is_authenticated(self) -> bool:
        return True

    def status_text(self) -> str:
        return f"{self._music_dir} · {self.track_count():,} tracks"

    # ---------- scanning ----------

    def rescan(self, on_progress=None) -> int:
        """Walk the music_dir, sync the index. Returns total track count."""
        if self._scan_in_progress:
            return self._index.count()
        self._scan_in_progress = True
        try:
            return _scan_dir(Path(self._music_dir), self._index, on_progress=on_progress,
                             stop=lambda: self._stopping)
        finally:
            self._scan_in_progress = False

    def start_watcher(self) -> None:
        if self._watcher is not None:
            return
        self._watcher = _Watcher(self._music_dir, self._index)
        self._watcher.start()

    def shutdown(self) -> None:
        self._stopping = True
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._index.close()

    # ---------- required surface ----------

    def search_songs(self, query: str, limit: int = 50) -> list[Track]:
        rows = self._index.search(query, limit=limit)
        return [self._row_to_track(r) for r in rows]

    def resolve_stream(self, track: Track) -> StreamRef:
        # video_id is the absolute path for local tracks.
        return StreamRef(backend="mpv", payload=track.video_id)

    # ---------- discovery surfaces ----------

    def get_home(self, limit: int = 5) -> list[Shelf]:
        recent = self._index.recent(limit=24)
        if not recent:
            return []
        items: list[ShelfItem] = []
        for r in recent:
            tr = self._row_to_track(r)
            items.append(ShelfItem(
                kind="song",
                title=tr.title,
                subtitle=tr.artists,
                thumbnail=tr.thumbnail,
                track=tr,
            ))
        return [Shelf(title="recently added", items=items)]

    # ---------- library (synthetic) ----------

    def get_library_playlists(self, limit: int = 100) -> list[PlaylistEntry]:
        """Synthesize one entry per (albumartist, album) pair, plus a global
        'all tracks' entry pinned to the top.

        ``playlist_id`` is encoded so ``get_playlist`` can decode the album
        without an extra round-trip.
        """
        entries: list[PlaylistEntry] = [
            PlaylistEntry(
                playlist_id="local::all",
                title="all tracks",
                description=f"{self._index.count():,} files",
                thumbnail="",
            ),
        ]
        for alb in self._index.albums():
            aa = alb.get("albumartist") or ""
            title = alb.get("album") or ""
            desc = f"{aa} · {alb.get('count', 0)} tracks"
            if alb.get("year"):
                desc += f" · {alb['year']}"
            entries.append(PlaylistEntry(
                playlist_id=f"local::alb::{aa}::{title}",
                title=title,
                description=desc,
                thumbnail=alb.get("art") or "",
            ))
        return entries

    def get_playlist(self, playlist_id: str, limit: int = 1000) -> PlaylistDetail:
        if playlist_id == "local::all":
            rows = self._index.all_tracks(limit=limit)
            return PlaylistDetail(
                playlist_id=playlist_id,
                title="all tracks",
                description=f"{len(rows)} files",
                track_count=len(rows),
                tracks=[self._row_to_track(r) for r in rows],
            )
        if playlist_id.startswith("local::alb::"):
            rest = playlist_id[len("local::alb::"):]
            try:
                aa, album = rest.split("::", 1)
            except ValueError:
                return PlaylistDetail(playlist_id=playlist_id, title="(invalid)")
            rows = self._index.album_tracks(aa, album)
            return PlaylistDetail(
                playlist_id=playlist_id,
                title=album,
                description=aa,
                track_count=len(rows),
                tracks=[self._row_to_track(r) for r in rows],
            )
        return PlaylistDetail(playlist_id=playlist_id, title="(unknown)")

    # ---------- helpers ----------

    def _row_to_track(self, row: dict) -> Track:
        secs = int(row.get("duration_seconds") or 0)
        dur = ""
        if secs >= 3600:
            dur = f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
        elif secs > 0:
            dur = f"{secs // 60}:{secs % 60:02d}"
        return Track(
            video_id=row["path"],
            title=row.get("title", "") or Path(row["path"]).stem,
            artists=row.get("artist", "") or "",
            album=row.get("album", "") or "",
            duration=dur,
            duration_seconds=secs,
            thumbnail=row.get("art") or "",
            source=SOURCE_SLUG,
            extras={"path": row["path"]},
        )
