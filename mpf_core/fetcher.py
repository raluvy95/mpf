"""YouTube metadata and playlist extractor using the system yt-dlp, or the yt_dlp module as a fallback."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from mpf_core.metadata import ID_LIMIT, NAME_LIMIT, TITLE_LIMIT, URL_LIMIT, clean_text
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


YTDLP_TIMEOUT_SECONDS = 300  # large playlists take a while to list


def _host_environment() -> Dict[str, str]:
    """Environment for host programs: inside a PyInstaller app, undo its library path override."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        host_path = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if host_path is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = host_path
    return env


def _extract_info_with_executable(executable: str, url: str) -> Any:
    """List a playlist with the system yt-dlp, the same one mpv uses to play it, so it stays current."""
    try:
        result = subprocess.run(
            [executable, "-J", "--flat-playlist", "--no-warnings", "--", url],
            capture_output=True,
            text=True,
            timeout=YTDLP_TIMEOUT_SECONDS,
            env=_host_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise PlaylistFetchError(f"yt-dlp timed out after {YTDLP_TIMEOUT_SECONDS}s") from err
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        raise PlaylistFetchError(f"Unable to retrieve URL: {clean_text(lines[-1] if lines else 'yt-dlp failed')}")
    try:
        return json.loads(result.stdout)
    except ValueError as err:
        raise PlaylistFetchError(f"yt-dlp returned unreadable output: {err}") from err


def _extract_info_with_module(url: str, ydl_opts: Optional[Dict[str, Any]]) -> Any:
    # Imported here, not at module level: yt_dlp takes ~300 ms to import, which launches that
    # load a fresh playlist cache (and --help/--version) never need.
    try:
        import yt_dlp
    except ImportError as err:
        raise PlaylistFetchError(
            "yt-dlp was not found; install it (e.g. `pip install -U yt-dlp` or your package manager)"
        ) from err

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
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as err:
        raise PlaylistFetchError(f"Unable to retrieve URL: {clean_text(str(err))}") from err
    except OSError as err:
        raise PlaylistFetchError(f"Network error while retrieving URL: {clean_text(str(err))}") from err


def fetch_playlist_tracks(url: str, ydl_opts: Optional[Dict[str, Any]] = None) -> List[Track]:
    """Extract tracks from a YouTube playlist or single video URL.

    Prefers the ``yt-dlp`` executable on PATH (always as current as the user keeps it, and the very
    one mpv plays with); falls back to the bundled/installed ``yt_dlp`` module when there is none, or
    when custom ``ydl_opts`` need the module API.
    """
    url = validate_media_url(url)
    executable = None if ydl_opts else shutil.which("yt-dlp")
    info = (
        _extract_info_with_executable(executable, url)
        if executable
        else _extract_info_with_module(url, ydl_opts)
    )

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
        video_id = clean_text(entry.get("id"), ID_LIMIT) or str(i)

        # yt-dlp leaves unavailable entries empty or with placeholder titles.
        raw_title = clean_text(entry.get("title"), TITLE_LIMIT)
        if not raw_title or raw_title in {"[Private video]", "[Deleted video]"}:
            skipped += 1
            continue
        fallback_title = f"Track {i + 1}"
        if raw_title == fallback_title and not entry.get("duration") and not entry.get("uploader"):
            skipped += 1
            continue

        # Preserve an extractor-provided URL only for a non-YouTube entry.
        entry_url = clean_text(entry.get("url"), URL_LIMIT)
        url_override = ""
        if entry_url.startswith("http") and "youtube.com" not in entry_url and "youtu.be" not in entry_url:
            url_override = entry_url

        duration = entry.get("duration")
        uploader = clean_text(entry.get("uploader") or entry.get("channel"), NAME_LIMIT)

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
