"""XDG-compliant locations for MPF configuration, cache, and logs."""

from __future__ import annotations

import os
import stat


def _xdg_home(variable: str, fallback: str) -> str:
    return os.environ.get(variable, os.path.join(os.path.expanduser("~"), fallback))


CONFIG_DIR = os.path.join(_xdg_home("XDG_CONFIG_HOME", ".config"), "mpf")
CACHE_DIR = os.path.join(_xdg_home("XDG_CACHE_HOME", ".cache"), "mpf")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
PLAYLIST_CACHE_FILE = os.path.join(CACHE_DIR, "playlists.mpk.zst")
PLAYBACK_CACHE_FILE = os.path.join(CACHE_DIR, "playback.json")
THUMBS_CACHE_DIR = os.path.join(CACHE_DIR, "thumbnails")
LOG_FILE = os.path.join(CACHE_DIR, "mpf.log")

SOCKET_NAME = "mpf-daemon.sock"
PID_NAME = "mpf-daemon.pid"


def state_dir(create: bool = False) -> str:
    """Where the background player keeps its socket and pid file.

    $XDG_RUNTIME_DIR when it is a private directory of this user, else the cache directory. Only the player
    itself needs the fallback directory to exist, so only it passes ``create``.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    try:
        info = os.stat(runtime_dir)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
            return runtime_dir
    except OSError:
        pass
    if create:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    return CACHE_DIR


def socket_path(create: bool = False) -> str:
    return os.path.join(state_dir(create), SOCKET_NAME)
