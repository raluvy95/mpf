"""YouTube metadata and playlist extractor using yt_dlp."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp

from mpf_core.models import Track

logger = logging.getLogger(__name__)

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


class InvalidMediaURL(ValueError):
    """Raised when input is not a supported public YouTube URL."""


class PlaylistFetchError(RuntimeError):
    """Raised when yt-dlp cannot retrieve playlist metadata."""


def validate_media_url(url: str) -> str:
    """Validate and normalize a public HTTP(S) YouTube playlist or video URL."""
    value = url.strip()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as err:
        raise InvalidMediaURL(f"Invalid URL: {err}") from err
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _YOUTUBE_HOSTS:
        raise InvalidMediaURL("Enter a youtube.com or youtu.be playlist/video URL")
    if parsed.username or parsed.password or port not in {None, 80, 443}:
        raise InvalidMediaURL("URL credentials and non-standard ports are not supported")
    if host == "youtu.be" and not parsed.path.strip("/"):
        raise InvalidMediaURL("YouTube video URL is missing a video ID")
    query = parse_qs(parsed.query)
    is_playlist = parsed.path == "/playlist" and bool(query.get("list"))
    is_short = parsed.path.startswith("/shorts/") and bool(parsed.path.removeprefix("/shorts/").strip("/"))
    is_video = (parsed.path == "/watch" and bool(query.get("v"))) or is_short
    if host != "youtu.be" and not (is_playlist or is_video):
        raise InvalidMediaURL("URL is not a YouTube playlist or video")
    return value


def fetch_playlist_tracks(url: str, ydl_opts: Optional[Dict[str, Any]] = None) -> List[Track]:
    """Extract tracks from YouTube playlist or single video URL using yt_dlp."""
    url = validate_media_url(url)
    opts: Dict[str, Any] = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    if ydl_opts:
        opts.update(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as err:
        raise PlaylistFetchError(f"Unable to retrieve URL: {err}") from err
    except OSError as err:
        raise PlaylistFetchError(f"Network error while retrieving URL: {err}") from err

    if not info:
        raise PlaylistFetchError("The URL returned no media information")

    entries = info.get("entries")
    if entries is None:
        entries = [info]

    tracks: List[Track] = []
    skipped = 0
    for i, entry in enumerate(entries):
        if not entry:
            skipped += 1
            continue
        video_id = entry.get("id") or str(i)

        # yt-dlp leaves unavailable entries empty or with placeholder titles.
        raw_title = entry.get("title") or ""
        if not raw_title or raw_title in {"[Private video]", "[Deleted video]"}:
            skipped += 1
            continue
        fallback_title = f"Track {i + 1}"
        if raw_title == fallback_title and not entry.get("duration") and not entry.get("uploader"):
            skipped += 1
            continue

        # Preserve an extractor-provided URL only for a non-YouTube entry.
        entry_url = entry.get("url") or ""
        url_override = ""
        if entry_url.startswith("http") and "youtube.com" not in entry_url and "youtu.be" not in entry_url:
            url_override = entry_url

        duration = entry.get("duration")
        uploader = entry.get("uploader") or entry.get("channel") or ""

        tracks.append(
            Track(
                id=video_id,
                title=raw_title,
                duration=float(duration) if duration is not None else None,
                uploader=uploader,
                _url=url_override,
            )
        )
    if skipped:
        logger.info("Skipped %d unavailable playlist entries", skipped)
    return tracks
