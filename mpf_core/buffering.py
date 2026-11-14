"""Adaptive read-ahead: buffer little while the network keeps up, more after playback stalls."""

from __future__ import annotations

from typing import Tuple

# Seconds of audio mpv reads ahead. Audio is only ~20 KB/s, so even the top level is a couple of MB.
READAHEAD_LEVELS: Tuple[int, ...] = (10, 20, 45, 90)
STALL_COOLDOWN = 10.0  # one stall steps up once, however many paused-for-cache flickers it causes
STABLE_SECONDS = 300.0  # stall-free playback before stepping back down one level


class AdaptiveReadahead:
    """Pure state machine: feed it stall events and the clock, read ``secs``."""

    def __init__(
        self,
        levels: Tuple[int, ...] = READAHEAD_LEVELS,
        cooldown: float = STALL_COOLDOWN,
        stable_seconds: float = STABLE_SECONDS,
    ) -> None:
        self._levels = levels
        self._cooldown = cooldown
        self._stable_seconds = stable_seconds
        self._level = 0
        self._last_change: float | None = None  # when the level last moved, or the last stall

    @property
    def secs(self) -> int:
        return self._levels[self._level]

    def on_stall(self, now: float) -> bool:
        """A mid-track stall. Returns True if the read-ahead went up."""
        if self._last_change is not None and now - self._last_change < self._cooldown:
            return False
        self._last_change = now
        if self._level + 1 >= len(self._levels):
            return False
        self._level += 1
        return True

    def tick(self, now: float) -> bool:
        """Call regularly while playing. Returns True if the read-ahead went down."""
        if self._level == 0 or self._last_change is None:
            return False
        if now - self._last_change < self._stable_seconds:
            return False
        self._level -= 1
        self._last_change = now
        return True
