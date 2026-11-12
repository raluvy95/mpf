"""Cleaning and parsing of untrusted track text (video titles, channel names, error lines).

Anything that comes from YouTube, yt-dlp or mpv can contain terminal escape sequences, NUL bytes or
lone surrogates. Those must never reach the terminal or D-Bus, so text is cleaned where it enters.
This module imports nothing from the rest of the package so every layer can use it.
"""

from __future__ import annotations

import re

TITLE_LIMIT = 300
NAME_LIMIT = 200
ID_LIMIT = 64
URL_LIMIT = 2048

_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r\x0b\x0c  ]+")
# C0 and C1 controls (ESC, BEL, NUL, CSI...), DEL, bidi overrides/isolates, and lone surrogates.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩\ud800-\udfff]")


def clean_text(text: object, limit: int = TITLE_LIMIT) -> str:
    """Return ``text`` without control characters, bidi overrides or lone surrogates, capped to ``limit``."""
    if not isinstance(text, str):
        return ""
    text = _WHITESPACE_CONTROLS.sub(" ", text)
    return _UNSAFE.sub("", text).strip()[:limit]
