"""No test may touch the real ~/.config/tide or ~/.cache/tide: tide.config
reads the XDG vars at import time, so they're redirected here, before
anything imports tide. Same as tide's own tests/conftest.py."""
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="tide-plugins-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")
os.environ["XDG_DATA_HOME"] = os.path.join(_SANDBOX, "data")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tide import config as _config  # noqa: E402

assert str(_config.CACHE_DIR).startswith(_SANDBOX), (
    "tide.config was imported before the test sandbox took effect; refusing to run")

# one QApplication for the whole run. some tests only need a
# QGuiApplication, and a widget test after one of those aborts.
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication(["tide-plugins-tests"])
