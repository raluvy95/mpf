"""Cleaning and parsing of untrusted track text (video titles, channel names, error lines).

Anything that comes from YouTube, yt-dlp or mpv can contain terminal escape sequences, NUL bytes or
lone surrogates. Those must never reach the terminal or D-Bus, so text is cleaned where it enters.
This module imports nothing from the rest of the package so every layer can use it.
"""

from __future__ import annotations

import logging
import re

TITLE_LIMIT = 300
NAME_LIMIT = 200
ID_LIMIT = 64
URL_LIMIT = 2048

_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r\x0b\x0c  ]+")
# C0 and C1 controls (ESC, BEL, NUL, CSI...), DEL, bidi overrides/isolates, and lone surrogates.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩\ud800-\udfff]")


_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*)://([^\s/?#]+)([^\s?#]*)([?#]\S*)?")
# Hosts whose URLs are public identifiers worth keeping in messages (?v=VIDEO_ID, ?list=...).
_PLAIN_URL_HOSTS = ("youtube.com", "youtu.be", "ytimg.com")
_URL_TRAILING = ")]}>.,;:!'\""  # punctuation that ends a sentence, not a query


def _is_plain_host(host: str) -> bool:
    return any(host == plain or host.endswith("." + plain) for plain in _PLAIN_URL_HOSTS)


def redact_urls(text: str) -> str:
    """Drop the query/fragment of URLs of any scheme, except YouTube page and thumbnail URLs.

    Stream URLs from googlevideo.com carry the client's IP address and signed tokens that stay valid
    for hours; they must not end up in log files or on screen. ``user:password@`` is dropped too.
    """

    def redact(match: re.Match[str]) -> str:
        scheme, authority, path = match.group(1), match.group(2), match.group(3)
        query = match.group(4) or ""
        hostport = authority.split("@")[-1]  # never keep user:password@
        trailing = ""
        if query and not _is_plain_host(hostport.lower().split(":")[0]):
            trailing = query[len(query.rstrip(_URL_TRAILING)) :]
            query = "?..."
        return f"{scheme}://{hostport}{path}{query}{trailing}"

    return _URL.sub(redact, text)


def clean_text(text: object, limit: int = TITLE_LIMIT) -> str:
    """Return ``text`` without control characters, bidi overrides or lone surrogates, capped to ``limit``."""
    if not isinstance(text, str):
        return ""
    text = _WHITESPACE_CONTROLS.sub(" ", text)
    return _UNSAFE.sub("", text).strip()[:limit]


# Bracketed tags that describe the upload, not the song: "(Official Video)", "[HD]", "(Lyrics)"...
# Tags that matter to the song ("(Remix)", "(feat. X)", "(Live)") are deliberately kept.
_NOISE_WORD = (
    r"official(?:\s+[\w'’-]+){0,4}|(?:music\s+|lyric\s+|lyrics\s+|audio\s+|full\s+|hd\s+|hq\s+|4k\s+)*"
    r"(?:video|audio|lyrics?|visuali[sz]er|mv|hd|hq|4k|1080p)(?:\s+version)?|with\s+lyrics|"
    r"explicit|clean|out\s+now"
)
_NOISE_TAG = re.compile(rf"\s*[\(\[]\s*(?:{_NOISE_WORD})\s*[\)\]]", re.IGNORECASE)
_SEPARATOR = re.compile(r"\s+[-–—‒]\s+")  # hyphen, en dash, em dash, figure dash with spaces around
_TOPIC_SUFFIX = " - Topic"


def strip_noise(title: str) -> str:
    """Remove "(Official Video)"-style tags from a video title."""
    return _NOISE_TAG.sub("", title).strip()


def parse_track(track: object) -> tuple[str, str]:
    """Best-effort (artist, title) from a track's YouTube title and channel name.

    "Artist - Title (Official Video)" splits on the first dash; a channel called "X - Topic" (YouTube's
    auto-generated music channels) means artist X; otherwise the channel is the artist and the whole
    (cleaned) title is the title. Either part may be empty when the track has no usable text.
    """
    raw_title = clean_text(getattr(track, "title", ""))
    channel = clean_text(getattr(track, "uploader", ""), NAME_LIMIT)
    title = strip_noise(raw_title) or raw_title

    if channel.endswith(_TOPIC_SUFFIX) and len(channel) > len(_TOPIC_SUFFIX):
        return channel[: -len(_TOPIC_SUFFIX)].strip(), title

    parts = _SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return clean_text(parts[0], NAME_LIMIT), clean_text(parts[1])
    return channel, title


class RedactingFilter(logging.Filter):
    """Log filter: redact URLs everywhere and keep untrusted text from forging log lines.

    Covers the message and any traceback (exception text can carry stream URLs or attacker-chosen
    newlines). Continuation lines are indented so a forged "timestamp LEVEL ..." line cannot start a
    line of its own.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = clean_text(redact_urls(record.getMessage()), 2000)
            record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = self._safe_block(logging.Formatter().formatException(record.exc_info))
            if record.stack_info:
                record.stack_info = self._safe_block(record.stack_info)
        except Exception:  # a broken record must never break logging itself
            pass
        return True

    @staticmethod
    def _safe_block(text: str) -> str:
        lines = [_UNSAFE.sub("", line) for line in redact_urls(text).splitlines()]
        return "\n".join(("" if index == 0 else "    ") + line for index, line in enumerate(lines))
