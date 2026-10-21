"""Publish plugins to tide's store.

    python tools/publish.py steamvr lights        # those two, then the index
    python tools/publish.py --all                 # every listed plugin
    python tools/publish.py --index-only          # re-sign the index (a new pick of the week)
    python tools/publish.py --all --dry-run       # build and check, upload nothing
    python tools/publish.py --all --local DIR     # a store in DIR with file:// links

For each plugin: reads <slug>/plugin.toml and <slug>/store.toml, vendors
what store.toml lists into vendor/, zips the folder with plugin.toml at
the zip's root, and uploads the zip and its pictures to a github release
named <slug>-<version>. Then it rebuilds index.json, signs it with the
store's key (ed25519, hex in index.json.sig), checks the result with
tide's own store.parse_index, and uploads both to the `index` release.

index.json in this repo is the published index: commit it after a
publish. A plugin whose store.toml says `listed = false` (the overlay)
never goes in it.

--local writes a whole store to DIR instead and prints the env vars that
point tide at it. --dry-run builds into build/ and stops before github.
Neither touches index.json here. Uploading needs `gh` signed in with
push rights to the repo.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
INDEX = REPO / "index.json"
FEATURED = REPO / "featured.toml"
GITHUB = "captiencelovesarch/tide-plugins"
INDEX_TAG = "index"
DEFAULT_KEY = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") \
    / "tide-plugins" / "signing-key.pem"
FORMAT = 1
BANNER_SIZE = (1280, 720)
MOTION_SECONDS = (4.0, 8.0)

# never in a zip: store-only files, caches, tests
SKIP_NAMES = {"store.toml", "store", "__pycache__", "tests", ".DS_Store"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
# a fixed time on every zip member, so the same files zip to the same bytes
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class Fail(Exception):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# reading a plugin
# ---------------------------------------------------------------------------

def plugin_slugs() -> list[str]:
    return sorted(p.parent.name for p in REPO.glob("*/plugin.toml"))


def read_plugin(slug: str) -> tuple[dict, dict]:
    folder = REPO / slug
    if not (folder / "plugin.toml").is_file():
        raise Fail(f"{slug}: no {slug}/plugin.toml")
    meta = tomllib.loads((folder / "plugin.toml").read_text("utf-8")).get("plugin", {})
    if meta.get("slug") != slug:
        raise Fail(f"{slug}: plugin.toml says slug {meta.get('slug')!r}")
    for key in ("name", "version", "api", "description"):
        if key not in meta:
            raise Fail(f"{slug}: plugin.toml has no {key}")
    store_file = folder / "store.toml"
    if not store_file.is_file():
        raise Fail(f"{slug}: no store.toml")
    store = tomllib.loads(store_file.read_text("utf-8"))
    if not str(store.get("blurb", "")).strip():
        raise Fail(f"{slug}: store.toml has no blurb")
    return meta, store


# ---------------------------------------------------------------------------
# building the zip
# ---------------------------------------------------------------------------

def _copy_plugin(src: Path, dest: Path) -> None:
    def ignore(_dir, names):
        return [n for n in names if n in SKIP_NAMES or Path(n).suffix in SKIP_SUFFIXES]
    shutil.copytree(src, dest, ignore=ignore)


def vendor(specs: list[str], dest: Path) -> list[str]:
    """pip-install ``specs`` (no dependencies, wheels only) into ``dest``.
    returns the top-level module names that landed there."""
    if not specs:
        return []
    if (dest).exists():
        raise Fail(f"{dest.parent.name}: has a vendor/ folder of its own. publish.py makes it")
    uv = shutil.which("uv")
    base = [uv, "pip", "install", "--quiet"] if uv else [sys.executable, "-m", "pip", "install", "-q"]
    cmd = base + ["--target", str(dest), "--no-deps", "--only-binary=:all:", *specs]
    if subprocess.run(cmd).returncode != 0:
        raise Fail(f"couldn't vendor {', '.join(specs)}")
    for junk in [dest / ".lock", *dest.rglob("__pycache__")]:
        if junk.is_dir():
            shutil.rmtree(junk)
        elif junk.exists():
            junk.unlink()
    names = []
    for child in sorted(dest.iterdir()):
        if child.name.endswith((".dist-info", ".egg-info", ".data")) or child.name == "bin":
            continue
        names.append(child.name.split(".")[0] if child.is_file() else child.name)
    return sorted(set(names))


def zip_folder(folder: Path) -> bytes:
    import io
    buf = io.BytesIO()
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            if path.is_symlink():
                raise Fail(f"{path} is a symlink. tide refuses those")
            info = zipfile.ZipInfo(path.relative_to(folder).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def check_zip(data: bytes, slug: str, meta: dict) -> None:
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if "plugin.toml" not in names:
            raise Fail(f"{slug}: plugin.toml isn't at the zip's root")
        bad = [n for n in names if n.startswith("/") or ".." in Path(n).parts or "\\" in n]
        if bad:
            raise Fail(f"{slug}: zip paths tide would refuse: {bad[:3]}")
        inner = tomllib.loads(zf.read("plugin.toml").decode("utf-8"))["plugin"]
    for key in ("slug", "version", "api"):
        if inner.get(key) != meta.get(key):
            raise Fail(f"{slug}: the zipped plugin.toml's {key} doesn't match")


# ---------------------------------------------------------------------------
# pictures
# ---------------------------------------------------------------------------

def _image_info(path: Path):
    try:
        from PIL import Image
    except ImportError:
        warn("no pillow here, so banner sizes aren't checked")
        return None
    with Image.open(path) as im:
        size = im.size
    durations = _webp_frame_ms(path.read_bytes())
    return size, max(1, len(durations)), sum(durations) / 1000.0


def _webp_frame_ms(data: bytes) -> list[int]:
    """each frame's duration in an animated webp, read from its ANMF
    chunks (pillow folds repeated frames together and doesn't report the
    timing). empty for a still."""
    out, i = [], 12
    while i + 8 <= len(data):
        tag, n = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
        if tag == b"ANMF":
            out.append(int.from_bytes(data[i + 20:i + 23], "little"))
        i += 8 + n + (n & 1)
    return out


def check_banner(path: Path, slug: str, cut: str) -> None:
    if path.suffix.lower() != ".webp" and not (cut == "motion" and path.suffix.lower() == ".webm"):
        raise Fail(f"{slug}: the {cut} banner should be webp, not {path.suffix}")
    if path.suffix.lower() == ".webm":
        warn(f"{slug}: the motion banner is webm, its size and length aren't checked here")
        return
    info = _image_info(path)
    if info is None:
        return
    size, frames, seconds = info
    if size != BANNER_SIZE:
        raise Fail(f"{slug}: the {cut} banner is {size[0]}×{size[1]}, not 1280×720")
    if cut == "motion":
        lo, hi = MOTION_SECONDS
        if frames < 2:
            raise Fail(f"{slug}: the motion banner doesn't move")
        if not lo <= seconds <= hi:
            raise Fail(f"{slug}: the motion banner runs {seconds:.1f} s, it should be 4 to 8")
    elif frames > 1:
        raise Fail(f"{slug}: the {cut} banner moves. only the motion cut may")


def pictures(slug: str, store: dict) -> list[tuple[str, str, Path]]:
    """(role, release asset name, file) for every picture store.toml lists.
    role is banner:grid, banner:full, banner:motion, icon, icon_filled or
    preview:<kind>."""
    folder = REPO / slug
    out = []

    def find(rel: str, what: str) -> Path:
        path = (folder / rel).resolve()
        if folder.resolve() not in path.parents or not path.is_file():
            raise Fail(f"{slug}: {what} {rel!r} isn't a file in {slug}/")
        return path

    banner = store.get("banner") or {}
    if "grid" not in banner:
        raise Fail(f"{slug}: no grid banner. no banner, no store entry")
    for cut in ("grid", "full", "motion"):
        if cut in banner:
            path = find(banner[cut], f"the {cut} banner")
            check_banner(path, slug, cut)
            out.append((f"banner:{cut}", f"{slug}-{cut}{path.suffix.lower()}", path))
    for key in ("icon", "icon_filled"):
        if store.get(key):
            path = find(store[key], key)
            if path.suffix.lower() != ".svg":
                raise Fail(f"{slug}: {key} should be an svg")
            out.append((key, f"{slug}-{key.replace('_', '-')}.svg", path))
    for i, prev in enumerate(store.get("preview", []) or [], 1):
        kind = prev.get("kind")
        if kind not in ("clip", "image"):
            raise Fail(f"{slug}: preview {i} has kind {kind!r}, not clip or image")
        path = find(prev.get("file", ""), f"preview {i}")
        out.append((f"preview:{kind}", f"{slug}-preview-{i}{path.suffix.lower()}", path))
    return out


# ---------------------------------------------------------------------------
# one plugin
# ---------------------------------------------------------------------------

class Built:
    def __init__(self, slug, meta, store, zip_path, zip_bytes, vendors, pics):
        self.slug, self.meta, self.store = slug, meta, store
        self.zip_path, self.zip_bytes = zip_path, zip_bytes
        self.vendors, self.pics = vendors, pics

    @property
    def tag(self) -> str:
        return f"{self.slug}-{self.meta['version']}"

    def entry(self, url_for) -> dict:
        m, s = self.meta, self.store
        e = {
            "slug": self.slug,
            "name": m["name"],
            "blurb": s["blurb"].strip(),
            "about": (s.get("about") or m["description"]).strip(),
            "version": m["version"],
            "api": m["api"],
            "size": len(self.zip_bytes),
            "sha256": sha256(self.zip_bytes),
            "url": url_for(self, f"{self.slug}.zip", self.zip_path),
            "uses": list(m.get("uses", [])),
            "needs": [_need(n) for n in s.get("need", []) or []],
            "author": s.get("author", "captience"),
            "official": bool(s.get("official", True)),
            "category": s.get("category", "tools"),
            "repo": f"https://github.com/{GITHUB}/tree/main/{self.slug}",
            "added": s.get("added") or date.today().isoformat(),
            "vendors": self.vendors,
        }
        if s.get("min_tide"):
            e["min_tide"] = s["min_tide"]
        banner, previews = {}, []
        for role, name, path in self.pics:
            asset = {"url": url_for(self, name, path), "sha256": sha256(path.read_bytes())}
            kind, _, sub = role.partition(":")
            if kind == "banner":
                banner[sub] = asset
            elif kind == "preview":
                previews.append({"kind": sub, **asset})
            else:
                e[kind] = asset
        e["banner"] = banner
        e["previews"] = previews
        return e


def _need(raw: dict) -> dict:
    if not isinstance(raw.get("what"), str):
        raise Fail(f"a need has no `what`: {raw}")
    out = {"what": raw["what"]}
    for key in ("check", "how", "hard_note"):
        if raw.get(key):
            out[key] = str(raw[key])
    if raw.get("hard"):
        out["hard"] = True
    return out


def build(slug: str) -> Built:
    meta, store = read_plugin(slug)
    if store.get("category", "tools") not in ("sources", "lyrics", "visuals", "hardware", "tools"):
        raise Fail(f"{slug}: category {store.get('category')!r} isn't one tide knows")
    out = BUILD / slug
    shutil.rmtree(out, ignore_errors=True)
    stage = out / "stage"
    _copy_plugin(REPO / slug, stage)
    vendors = vendor(list(store.get("vendor", []) or []), stage / "vendor")
    for pattern in store.get("vendor_drop", []) or []:
        for path in (stage / "vendor").glob(pattern):
            if path.is_file():
                path.unlink()
    for name in vendors:
        if importlib.util.find_spec(name) is not None:
            warn(f"{slug}: vendors {name}, which this python already has. "
                 "if tide imports it too, the two can clash")
    data = zip_folder(stage)
    check_zip(data, slug, meta)
    zip_path = out / f"{slug}.zip"
    zip_path.write_bytes(data)
    pics = pictures(slug, store)
    say(f"{slug} {meta['version']}: {len(data) / 1024:.0f} kb"
        + (f", vendors {', '.join(vendors)}" if vendors else "")
        + f", {len(pics)} picture{'s' if len(pics) != 1 else ''}")
    return Built(slug, meta, store, zip_path, data, vendors, pics)


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

def featured() -> dict | None:
    if not FEATURED.is_file():
        return None
    raw = tomllib.loads(FEATURED.read_text("utf-8")).get("featured")
    if not raw:
        return None
    if not raw.get("slug"):
        raise Fail("featured.toml has no slug")
    out = {"slug": raw["slug"], "note": str(raw.get("note", ""))}
    if raw.get("until"):
        out["until"] = str(raw["until"])
    return out


def make_index(entries: list[dict]) -> bytes:
    index = {
        "format": FORMAT,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": sorted(entries, key=lambda e: e["slug"]),
    }
    feat = featured()
    if feat:
        if feat["slug"] not in {e["slug"] for e in entries}:
            raise Fail(f"featured.toml picks {feat['slug']}, which isn't in the index")
        index["featured"] = feat
    return (json.dumps(index, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def vendor_clashes(entries: list[dict]) -> None:
    seen: dict[str, str] = {}
    for e in entries:
        for name in e.get("vendors", []):
            if name in seen:
                warn(f"{seen[name]} and {e['slug']} both vendor {name}. "
                     "two versions of one module in one tide can clash")
            else:
                seen[name] = e["slug"]


def load_key(path: Path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    if not path.is_file():
        raise Fail(f"no signing key at {path}. make one with tools/keygen.py")
    if path.stat().st_mode & 0o077:
        raise Fail(f"{path} can be read by other users. chmod 600 it")
    key = load_pem_private_key(path.read_bytes(), None)
    if not isinstance(key, Ed25519PrivateKey):
        raise Fail(f"{path} isn't an ed25519 key")
    return key


def public_hex(key) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def tide_store(tide_src: str | None):
    """tide's own store module, to read the index exactly as tide will."""
    candidates = [tide_src] if tide_src else []
    candidates += [os.environ.get("TIDE_SRC"), str(Path.home() / "tide" / "src")]
    for src in filter(None, candidates):
        if (Path(src).expanduser() / "tide" / "store.py").is_file():
            sys.path.insert(0, str(Path(src).expanduser()))
            import tide.store as store
            return store
    try:
        import tide.store as store
        return store
    except ImportError:
        raise Fail("can't find tide's store.py to check the index with. "
                   "pass --tide path/to/tide/src") from None


def check_with_tide(store, data: bytes, sig: bytes, pub: str, slugs: set[str]) -> None:
    index = store.parse_index(data, sig, pub)
    if index.skipped:
        raise Fail("tide would skip: " + "; ".join(index.skipped))
    missing = slugs - set(index.entries)
    if missing:
        raise Fail(f"tide doesn't see {', '.join(sorted(missing))}")
    if getattr(store, "PUBLIC_KEY", "") and store.PUBLIC_KEY != pub:
        warn("this key isn't the one in that tide's store.PUBLIC_KEY. "
             "that tide will refuse this index")
    say(f"tide's store.parse_index reads it: {', '.join(sorted(index.entries))}")


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------

def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("gh"):
        raise Fail("needs the gh cli")
    res = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise Fail(f"gh {' '.join(args[:3])}: {res.stderr.strip()}")
    return res


def release_exists(repo: str, tag: str) -> bool:
    return gh("release", "view", tag, "--repo", repo, check=False).returncode == 0


def upload_plugin(b: Built, repo: str, replace: bool) -> None:
    files = [str(b.zip_path)]
    staged = BUILD / b.slug / "assets"
    staged.mkdir(parents=True, exist_ok=True)
    for _role, name, path in b.pics:
        # release assets take the file's own name, so stage them renamed
        shutil.copyfile(path, staged / name)
        files.append(str(staged / name))
    if release_exists(repo, b.tag):
        if not replace:
            raise Fail(f"{b.tag} is published already. bump the version in "
                       f"{b.slug}/plugin.toml (or --replace, if nobody has it yet)")
        gh("release", "upload", b.tag, *files, "--repo", repo, "--clobber")
    else:
        gh("release", "create", b.tag, *files, "--repo", repo, "--target", "main",
           "--title", f"{b.meta['name']} {b.meta['version']}",
           "--notes", b.store["blurb"].strip(), "--latest=false")
    say(f"uploaded {b.tag}")


def upload_index(repo: str, index_path: Path, sig_path: Path) -> None:
    if not release_exists(repo, INDEX_TAG):
        gh("release", "create", INDEX_TAG, "--repo", repo, "--target", "main",
           "--title", "the store's index",
           "--notes", "the signed list of plugins tide's store reads. don't delete this release.",
           "--latest=false")
    # the signature goes up second: until it lands tide sees a mismatch,
    # refuses the new index and keeps its cached one, which is fine
    gh("release", "upload", INDEX_TAG, str(index_path), "--repo", repo, "--clobber")
    gh("release", "upload", INDEX_TAG, str(sig_path), "--repo", repo, "--clobber")
    say(f"uploaded the index to the {INDEX_TAG} release")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--all", action="store_true", help="every plugin with listed = true")
    ap.add_argument("--index-only", action="store_true", help="re-sign and upload the index only")
    ap.add_argument("--dry-run", action="store_true", help="build and check, upload nothing")
    ap.add_argument("--local", type=Path, metavar="DIR", help="write a whole store to DIR")
    ap.add_argument("--key", type=Path, default=Path(os.environ.get("TIDE_PLUGINS_KEY", DEFAULT_KEY)))
    ap.add_argument("--tide", metavar="SRC", help="tide's src/ folder, to check the index with")
    ap.add_argument("--repo", default=GITHUB)
    ap.add_argument("--replace", action="store_true",
                    help="re-upload a version that's out already")
    args = ap.parse_args()

    try:
        listed = []
        for slug in plugin_slugs():
            _meta, store = read_plugin(slug)
            if store.get("listed", True):
                listed.append(slug)
        if args.all or args.local:
            slugs = listed
        else:
            slugs = args.slugs
        if args.index_only and (slugs or args.local):
            raise Fail("--index-only takes no plugins")
        if not slugs and not args.index_only:
            ap.error("name the plugins to publish, or --all, or --index-only")
        for slug in slugs:
            if slug not in listed:
                raise Fail(f"{slug} isn't listed (no folder, or listed = false in its store.toml)")

        key = load_key(args.key.expanduser())
        pub = public_hex(key)
        store_mod = tide_store(args.tide)
        BUILD.mkdir(exist_ok=True)
        built = [build(s) for s in slugs]

        if args.local:
            out = args.local.expanduser().resolve()
            out.mkdir(parents=True, exist_ok=True)
            # tide only takes file:// links while TIDE_STORE_INDEX is set
            os.environ[store_mod.INDEX_ENV] = (out / "index.json").as_uri()

            def url_for(b, name, path):
                shutil.copyfile(path, out / name)
                return (out / name).as_uri()
            entries = [b.entry(url_for) for b in built]
        else:
            def url_for(b, name, _path):
                return f"https://github.com/{args.repo}/releases/download/{b.tag}/{name}"
            old = json.loads(INDEX.read_text("utf-8")).get("plugins", []) if INDEX.is_file() else []
            fresh = {b.slug: b.entry(url_for) for b in built}
            for b in built:
                prev = next((e for e in old if e["slug"] == b.slug), None)
                if prev and prev.get("added") and not b.store.get("added"):
                    fresh[b.slug]["added"] = prev["added"]     # the first publish's date stays
            entries = [e for e in old if e["slug"] in listed and e["slug"] not in fresh]
            entries += fresh.values()

        vendor_clashes(entries)
        data = make_index(entries)
        sig = key.sign(data).hex().encode("ascii")
        key.public_key().verify(bytes.fromhex(sig.decode()), data)
        check_with_tide(store_mod, data, sig, pub, {e["slug"] for e in entries})

        target = args.local.expanduser().resolve() if args.local else BUILD
        (target / "index.json").write_bytes(data)
        (target / "index.json.sig").write_bytes(sig)

        if args.local:
            say(f"\na store with {len(entries)} plugins in {target}")
            say(f"TIDE_STORE_INDEX={(target / 'index.json').as_uri()}")
            say(f"TIDE_STORE_KEY={pub}")
            return 0
        if args.dry_run:
            say(f"\ndry run: built into {BUILD}, nothing uploaded")
            return 0

        # the active account only: a stale second login shouldn't stop a publish
        say(f"publishing as {gh('api', 'user', '-q', '.login').stdout.strip()}")
        for b in built:
            upload_plugin(b, args.repo, args.replace)
        upload_index(args.repo, target / "index.json", target / "index.json.sig")
        INDEX.write_bytes(data)
        say("index.json here is updated. commit it.")
        return 0
    except Fail as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
