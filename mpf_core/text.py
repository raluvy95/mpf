"""Cleaning and formatting of text, with nothing heavy imported.

Kept apart from the rest so the quick control commands can use it without loading logging, asyncio or
anything else they do not need. ``metadata`` and ``models`` re-export these names.
"""

from __future__ import annotations

import math
import re

TITLE_LIMIT = 300
NAME_LIMIT = 200
ID_LIMIT = 64
URL_LIMIT = 2048

_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r\x0b\x0c\u2028\u2029]+")
# C0 and C1 controls (ESC, BEL, NUL, CSI...), DEL, bidi overrides/isolates, and lone surrogates.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")


def clean_text(text: object, limit: int = TITLE_LIMIT) -> str:
    """Return ``text`` without control characters, bidi overrides or lone surrogates, capped to ``limit``."""
    if not isinstance(text, str):
        return ""
    text = _WHITESPACE_CONTROLS.sub(" ", text)
    return _UNSAFE.sub("", text).strip()[:limit]


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


def watch_url(video_id: str) -> str:
    """The URL mpv is given for a YouTube video id."""
    return f"https://youtu.be/{video_id}"
