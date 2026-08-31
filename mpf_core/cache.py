"""Persistent caching for playlists and playback state.

Playlist cache format (v2):
  - File:       .cache_playlists.mpk.zst
  - Encoding:   msgpack serialised dict, compressed with zstandard (level 3)
  - Track rows: positional tuples [id, title, duration_int_or_null, uploader]
                URL domains and thumbnail parameters are NOT stored.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, List, Optional

import msgpack
import zstandard as zstd

from mpf_core.models import Track
from mpf_core.paths import PLAYBACK_CACHE_FILE, PLAYLIST_CACHE_FILE

logger = logging.getLogger(__name__)

# Re-sync cached playlist against upstream once per week (7 days in seconds)
CACHE_TTL_SECONDS: float = 7 * 24 * 3600

# zstandard compressor / decompressor (level 3 – fast writes, good ratio)
_ZCTX = zstd.ZstdCompressor(level=3)
_DCTX = zstd.ZstdDecompressor()


# ── Low-level binary I/O ─────────────────────────────────────────────────────

@contextmanager
def _cache_lock(cache_file: str, exclusive: bool) -> Iterator[None]:
    """Coordinate cache access between threads and MPF processes."""
    parent = os.path.dirname(os.path.abspath(cache_file))
    os.makedirs(parent, exist_ok=True)
    with open(f"{cache_file}.lock", "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_binary_cache_unlocked(cache_file: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "rb") as fh:
            compressed = fh.read()
        raw = _DCTX.decompress(compressed)
        data = msgpack.unpackb(raw, raw=False)
        if not isinstance(data, dict):
            raise ValueError("cache root is not a mapping")
        return data
    except (OSError, ValueError, TypeError, msgpack.exceptions.UnpackException, zstd.ZstdError) as err:
        logger.warning("Ignoring corrupt playlist cache %s: %s", cache_file, err)
        return None


def _read_binary_cache(cache_file: str) -> Optional[Dict[str, Any]]:
    """Read a msgpack+zstd cache file, returning None if it is missing or corrupt."""
    with _cache_lock(cache_file, exclusive=False):
        return _read_binary_cache_unlocked(cache_file)


def _atomic_write(payload: bytes, cache_file: str) -> None:
    """Replace a file only after its contents have reached stable storage."""
    parent = os.path.dirname(os.path.abspath(cache_file))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_file = tempfile.mkstemp(prefix=f".{os.path.basename(cache_file)}.", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_file, cache_file)
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_file)
        except FileNotFoundError:
            pass


def _write_binary_cache(data: Dict, cache_file: str) -> None:
    """Atomically write a dict to a msgpack+zstd file."""
    raw = msgpack.packb(data, use_bin_type=True)
    compressed = _ZCTX.compress(raw)
    _atomic_write(compressed, cache_file)


# ── Staleness check ──────────────────────────────────────────────────────────

def is_playlist_cache_stale(url: str, cache_file: str = PLAYLIST_CACHE_FILE) -> bool:
    """Return True when cached playlist is older than CACHE_TTL_SECONDS or missing."""
    cache = _read_binary_cache(cache_file)
    if cache is None:
        return True
    entry = cache.get(url)
    if not entry:
        return True
    try:
        cached_at = float(entry.get("cached_at", 0))
        return (time.time() - cached_at) >= CACHE_TTL_SECONDS
    except (TypeError, ValueError, AttributeError):
        return True


# ── PlaybackState ─────────────────────────────────────────────────────────────

@dataclass
class PlaybackState:
    """Stores exact playback position and metadata to resume playback."""
    playlist_url: str
    track_id: str
    track_index: int
    time_pos: float
    duration: float = 0.0
    repeat_mode: str = "All"
    volume: int = 100
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlaybackState":
        if not isinstance(data, dict):
            raise ValueError("playback state is not a mapping")
        repeat_mode = str(data.get("repeat_mode", "All"))
        if repeat_mode not in {"Off", "All", "One"}:
            raise ValueError(f"invalid repeat mode: {repeat_mode}")
        return cls(
            playlist_url=data.get("playlist_url", ""),
            track_id=data.get("track_id", ""),
            track_index=int(data.get("track_index", 0)),
            time_pos=float(data.get("time_pos", 0.0)),
            duration=float(data.get("duration", 0.0)),
            repeat_mode=repeat_mode,
            volume=max(0, min(100, int(data.get("volume", 100)))),
            updated_at=float(data.get("updated_at", 0.0)),
        )


# ── Playlist cache – public API ───────────────────────────────────────────────

def load_cached_playlist(url: str, cache_file: str = PLAYLIST_CACHE_FILE) -> Optional[List[Track]]:
    """Load playlist tracks from binary cache."""
    cache = _read_binary_cache(cache_file)
    if cache is not None:
        entry = cache.get(url)
        if entry and "tracks" in entry:
            try:
                return [Track.from_cache_tuple(t) for t in entry["tracks"]]
            except (TypeError, ValueError, IndexError) as err:
                logger.warning("Ignoring invalid playlist entry for %s: %s", url, err)

    return None


def save_cached_playlist(url: str, tracks: List[Track], cache_file: str = PLAYLIST_CACHE_FILE) -> bool:
    """Save playlist tracks to binary cache as compact positional tuples."""
    try:
        with _cache_lock(cache_file, exclusive=True):
            cache: Dict[str, Any] = _read_binary_cache_unlocked(cache_file) or {}
            cache[url] = {
                "cached_at": time.time(),
                "count": len(tracks),
                "tracks": [list(t.to_cache_tuple()) for t in tracks],
            }
            _write_binary_cache(cache, cache_file)
        return True
    except (OSError, TypeError, ValueError, zstd.ZstdError) as err:
        logger.error("Unable to save playlist cache %s: %s", cache_file, err)
        return False


# ── Playback state ────────────────────────────────────────────────────────────

def load_playback_state(url: str, cache_file: str = PLAYBACK_CACHE_FILE) -> Optional[PlaybackState]:
    """Load last saved playback state (exact timestamp, track, volume) for playlist URL."""
    if not os.path.exists(cache_file):
        return None
    try:
        with _cache_lock(cache_file, exclusive=False):
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
        if not isinstance(cache, dict):
            raise ValueError("cache root is not a mapping")
        entry = cache.get(url)
        if entry:
            return PlaybackState.from_dict(entry)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as err:
        logger.warning("Ignoring corrupt playback cache %s: %s", cache_file, err)
    return None


def save_playback_state(state: PlaybackState, cache_file: str = PLAYBACK_CACHE_FILE) -> bool:
    """Save exact playback state (timestamp, track, index) for playlist URL."""
    try:
        with _cache_lock(cache_file, exclusive=True):
            cache: Dict[str, Any] = {}
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        cache = loaded
                    else:
                        logger.warning("Replacing invalid playback cache root in %s", cache_file)
                except (OSError, json.JSONDecodeError) as err:
                    logger.warning("Replacing corrupt playback cache %s: %s", cache_file, err)

            state_dict = state.to_dict()
            state_dict["updated_at"] = time.time()
            cache[state.playlist_url] = state_dict
            payload = json.dumps(cache, indent=2, ensure_ascii=False).encode("utf-8")
            _atomic_write(payload, cache_file)
        return True
    except (OSError, TypeError, ValueError) as err:
        logger.error("Unable to save playback cache %s: %s", cache_file, err)
        return False
