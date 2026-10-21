"""Check a plugin folder the way tide reads it.

    python tools/check.py ~/.config/tide/plugins/my-plugin

Parses plugin.toml with tide's own parser, says whether the entry class
imports and whether `requires` is there. tide 3.0 skips a plugin whose
plugin.toml doesn't parse without saying why; this says why.

Needs tide importable: installed, or PYTHONPATH=path/to/tide/src.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().split("\n\n")[1].strip())
        return 2
    folder = Path(sys.argv[1]).expanduser().resolve()
    try:
        from tide.plugin_api import API_VERSION
        from tide.plugin_host import ManifestError, PluginHost, parse_manifest
    except ImportError:
        print("can't import tide. install it, or set PYTHONPATH=path/to/tide/src")
        return 2
    try:
        m = parse_manifest(folder)
    except ManifestError as exc:
        print(f"plugin.toml: {exc}")
        return 1
    print(f"{m.name} {m.version} ({m.slug}), api {m.api}, {len(m.settings)} settings")
    ok = True
    if m.api != API_VERSION:
        print(f"api {m.api}: this tide speaks api {API_VERSION}, so it won't start")
        ok = False

    from PySide6.QtCore import QCoreApplication
    QCoreApplication.instance() or QCoreApplication([])
    host = PluginHost(None, None, None, roots=[(folder.parent, "yours")],
                      state_file=Path("/dev/null"), settings_dir=Path("/nonexistent"))
    rec = host.record(m.slug)
    missing = rec.missing() if rec is not None else []
    if missing:
        print("missing python modules: " + ", ".join(missing))
        ok = False
    else:
        try:
            cls = host._import_entry(m)
        except Exception as exc:  # noqa: BLE001 - reported
            print(f"entry {m.entry}: {type(exc).__name__}: {exc}")
            ok = False
        else:
            if not callable(getattr(cls, "start", None)):
                print(f"entry {m.entry}: has no start(ctx)")
                ok = False
        finally:
            host._purge_modules(m)
    print("ok" if ok else "not ok")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
