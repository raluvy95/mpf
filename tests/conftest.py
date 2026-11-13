"""Keep the test suite away from the user's real configuration and cache.

mpf_core.paths reads XDG_CONFIG_HOME and XDG_CACHE_HOME when it is first imported, so they are
pointed at a throwaway directory here, before any test module imports the package. Without this, tests
that build a player with the default paths (and press keys that save settings) rewrite the real
~/.config/mpf/config.json and ~/.cache/mpf/playback.json on every run.
"""

import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="mpf-tests-")
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
os.environ["XDG_CONFIG_HOME"] = os.path.join(_ROOT, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_ROOT, "cache")
