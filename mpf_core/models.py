"""Data models and track queue management."""

from __future__ import annotations

import enum
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from mpf_core.fuzzy import fuzzy_filter_tracks
from mpf_core.metadata import ID_LIMIT, NAME_LIMIT, TITLE_LIMIT, clean_text

# ── URL/thumbnail helpers ────────────────────────────────────────────────────

_YT_WATCH_PREFIX = "https://www.youtube.com/watch?v="
_YT_SHORT_PREFIX = "https://youtu.be/"
_YTIMG_HQ_TEMPLATE = "https://i.ytimg.com/vi/{id}/hqdefault.jpg"


def _make_url(video_id: str) -> str:
    return f"{_YT_SHORT_PREFIX}{video_id}"


def _make_thumbnail(video_id: str) -> str:
    return _YTIMG_HQ_TEMPLATE.format(id=video_id)


# ── Utility ──────────────────────────────────────────────────────────────────

def format_time(seconds: float | int | None) -> str:
    """Format seconds into HH:MM:SS or MM:SS string."""
    if seconds is None or math.isnan(seconds) or seconds < 0:
        return "--:--"
    total_sec = int(round(seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hrs > 0:
        return f"{hrs:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


# ── Track ────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    """Represents a playable YouTube track.

    Only ``id``, ``title``, ``duration``, and ``uploader`` are stored.
    ``url`` and ``thumbnail`` are computed from the video ``id`` on access so
    that cache entries never need to repeat the static URL domain prefixes or
    thumbnail query-string parameters.

    For tracks whose video URL genuinely differs from the standard YouTube
    watch URL (e.g. a local file or a non-YouTube source) you can still pass
    an explicit ``_url`` override at construction time.  Cache serialization
    ignores ``_url`` – it is not persisted.
    """

    id: str
    title: str
    duration: Optional[float] = None
    uploader: str = ""
    # Private override – set only when the real URL is NOT a standard YT watch URL.
    # Not serialised to the compact cache format.
    _url: str = ""

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """Full video URL, reconstructed from *id* unless overridden."""
        return self._url if self._url else _make_url(self.id)

    @property
    def thumbnail(self) -> str:
        """High-res thumbnail URL, reconstructed from *id*."""
        return _make_thumbnail(self.id)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def display_label(self, index: int, is_active: bool = False, max_width: int = 80) -> str:
        dur_str = f" [{format_time(self.duration)}]" if self.duration else ""
        prefix = "▶ " if is_active else "  "
        artist = f" - {self.uploader}" if self.uploader else ""
        text = f"{prefix}{index + 1}. {self.title}{artist}{dur_str}"
        if len(text) > max_width:
            return text[: max_width - 1] + "…"
        return text

    # ------------------------------------------------------------------
    # Compact cache serialisation  (tuple-based, positional)
    # ------------------------------------------------------------------

    def to_cache_tuple(self) -> Tuple[str, str, Optional[int], str]:
        """Serialise to a compact positional tuple ``[id, title, duration_int, uploader]``."""
        dur = int(self.duration) if self.duration is not None else None
        return (self.id, self.title, dur, self.uploader)

    @classmethod
    def from_cache_tuple(cls, t: Tuple) -> "Track":
        """Reconstruct from a positional tuple produced by :meth:`to_cache_tuple`."""
        video_id, title, dur, uploader = t[0], t[1], t[2], t[3] if len(t) > 3 else ""
        # The cache is untrusted input too: it may predate sanitising or have been edited.
        return cls(
            id=clean_text(video_id, ID_LIMIT),
            title=clean_text(title, TITLE_LIMIT),
            duration=float(dur) if dur is not None else None,
            uploader=clean_text(uploader, NAME_LIMIT),
        )

    # ------------------------------------------------------------------
    # Legacy / full dict serialisation (kept for backward compat)
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a full dict (matches the pre-refactor JSON schema)."""
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "duration": self.duration,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Track":
        """Reconstruct from a full dict (handles legacy JSON cache entries)."""
        raw_url = data.get("url", "")
        # Strip the standard watch URL prefix – the id is enough.
        if raw_url.startswith(_YT_WATCH_PREFIX):
            raw_url = ""
        elif raw_url.startswith(_YT_SHORT_PREFIX):
            raw_url = ""
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            duration=data.get("duration"),
            uploader=data.get("uploader", ""),
            _url=raw_url,
        )


# ── RepeatMode ───────────────────────────────────────────────────────────────

class RepeatMode(enum.Enum):
    OFF = "Off"
    ALL = "All"
    ONE = "One"

    @property
    def icon(self) -> str:
        return {
            RepeatMode.OFF: "󰑗",
            RepeatMode.ALL: "󰑖",
            RepeatMode.ONE: "󰑘",
        }[self]

    def next_mode(self) -> "RepeatMode":
        modes = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        curr_idx = modes.index(self)
        return modes[(curr_idx + 1) % len(modes)]


# ── TrackQueue ───────────────────────────────────────────────────────────────

class TrackQueue:
    """Manages playlist state, active track, fuzzy search, and repeat modes."""

    def __init__(self, tracks: Optional[List[Track]] = None) -> None:
        self.tracks: List[Track] = tracks or []
        self.current_idx: int = 0
        self.repeat_mode: RepeatMode = RepeatMode.ALL
        # Shuffle is a mode: ``tracks`` holds the shuffled order while it is on, and
        # ``_ordered`` remembers the real playlist order so turning it off restores it.
        self.shuffle_enabled: bool = False
        self._ordered: List[Track] = []
        self._search_filter: str = ""
        self._filtered_cache: Optional[List[Tuple[int, Track]]] = None
        # Bumped on every change to the visible queue so the UI can skip no-op redraws.
        self.revision: int = 0

    def set_tracks(self, tracks: List[Track], reset_index: bool = True) -> None:
        current_id = self.current_track.id if self.current_track else None
        old_index = self.current_idx
        if self.shuffle_enabled and reset_index:
            # A different playlist: indexes computed by callers refer to its real order.
            self.shuffle_enabled = False
        if self.shuffle_enabled:
            # Same playlist re-synced: keep the shuffled order for tracks that remain.
            self._ordered = list(tracks)
            incoming = {track.id: track for track in tracks}
            kept = [incoming.pop(track.id) for track in self.tracks if track.id in incoming]
            fresh = list(incoming.values())
            random.shuffle(fresh)
            tracks = kept + fresh
        self.tracks = list(tracks)
        self._filtered_cache = None
        self.revision += 1
        if reset_index or not self.tracks:
            self.current_idx = 0
        elif current_id:
            self.current_idx = next(
                (index for index, track in enumerate(self.tracks) if track.id == current_id),
                min(old_index, len(self.tracks) - 1),
            )

    @property
    def current_track(self) -> Optional[Track]:
        if 0 <= self.current_idx < len(self.tracks):
            return self.tracks[self.current_idx]
        return None

    @property
    def filtered_tracks(self) -> List[Tuple[int, Track]]:
        """Return list of (original_index, track) matching fuzzy search."""
        if self._filtered_cache is not None:
            return self._filtered_cache
        query = self._search_filter.strip()
        if not query:
            self._filtered_cache = list(enumerate(self.tracks))
        else:
            self._filtered_cache = fuzzy_filter_tracks(self.tracks, query)
        return self._filtered_cache

    def set_filter(self, query: str) -> None:
        if query == self._search_filter:
            return
        self._search_filter = query
        self._filtered_cache = None
        self.revision += 1

    def clear_filter(self) -> None:
        self.set_filter("")

    def get_next_index(self, natural_end: bool = False) -> Optional[int]:
        if not self.tracks:
            return None
        if natural_end and self.repeat_mode == RepeatMode.ONE:
            return self.current_idx
        if self.current_idx + 1 < len(self.tracks):
            return self.current_idx + 1
        if self.repeat_mode in (RepeatMode.ALL, RepeatMode.ONE) or not natural_end:
            return 0
        return None

    def get_prev_index(self) -> Optional[int]:
        if not self.tracks:
            return None
        if self.current_idx - 1 >= 0:
            return self.current_idx - 1
        return len(self.tracks) - 1

    def set_shuffle(self, enabled: bool) -> None:
        """Turn shuffle on or off without losing the playlist order or the current track."""
        if enabled == self.shuffle_enabled:
            return
        current = self.current_track
        if enabled:
            self._ordered = list(self.tracks)
            random.shuffle(self.tracks)
        else:
            position = {track.id: index for index, track in enumerate(self._ordered)}
            self.tracks.sort(key=lambda track: position.get(track.id, len(position)))
            self._ordered = []
        self.shuffle_enabled = enabled
        self._filtered_cache = None
        self.revision += 1
        self.current_idx = self.tracks.index(current) if current in self.tracks else 0

    def toggle_shuffle(self) -> bool:
        self.set_shuffle(not self.shuffle_enabled)
        return self.shuffle_enabled
