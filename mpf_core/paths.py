"""XDG-compliant locations for MPF configuration, cache, and logs."""

from __future__ import annotations

import os


def _xdg_home(variable: str, fallback: str) -> str:
    return os.environ.get(variable, os.path.join(os.path.expanduser("~"), fallback))


CONFIG_DIR = os.path.join(_xdg_home("XDG_CONFIG_HOME", ".config"), "mpf")
CACHE_DIR = os.path.join(_xdg_home("XDG_CACHE_HOME", ".cache"), "mpf")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
PLAYLIST_CACHE_FILE = os.path.join(CACHE_DIR, "playlists.mpk.zst")
PLAYBACK_CACHE_FILE = os.path.join(CACHE_DIR, "playback.json")
THUMBS_CACHE_DIR = os.path.join(CACHE_DIR, "thumbnails")
LOG_FILE = os.path.join(CACHE_DIR, "mpf.log")
