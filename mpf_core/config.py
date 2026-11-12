"""User configuration loading for MPF."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, Optional

from mpf_core.fetcher import InvalidMediaURL, validate_media_url
from mpf_core.paths import CONFIG_FILE

logger = logging.getLogger(__name__)
VISUALIZER_STYLES = ("waterfall", "bars", "braille", "waveform")
DEFAULT_VIM_MODE = True
# yt-dlp picks the YouTube client itself. When mpv fails a track (YouTube's media server sometimes
# answers HTTP 403, or yt-dlp errors out), the track is first retried once with yt-dlp's own choice
# again (a fresh URL is usually enough), then with these clients in order, then skipped. Edit the
# list with the "ytdl_fallback_clients" key in config.json; an empty list leaves only the first retry.
# Which clients work changes as YouTube changes; names are yt-dlp "player_client" values.
DEFAULT_YTDL_FALLBACK_CLIENTS = ("tv_simply", "mweb")
_CLIENT_NAME = re.compile(r"[A-Za-z0-9_]+")  # ends up inside an mpv option string: no commas or '='


def _parse_config(config_file: str) -> Any:
    with open(config_file, encoding="utf-8") as file:
        return json.load(file)


def _load_config(config_file: str) -> Dict[str, Any]:
    try:
        config = _parse_config(config_file)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        logger.warning("Ignoring invalid configuration %s: %s", config_file, err)
        return {}
    return config if isinstance(config, dict) else {}


def _load_config_for_update(config_file: str) -> Optional[Dict[str, Any]]:
    """Load the config for a read-modify-write, never discarding a file we cannot parse.

    An unparseable file is moved aside to ``<config>.bad`` so saving a setting does not
    destroy the user's hand-edited content. Returns None when it cannot be updated safely.
    """
    try:
        config = _parse_config(config_file)
    except FileNotFoundError:
        return {}
    except OSError as err:
        logger.warning("Not updating unreadable configuration %s: %s", config_file, err)
        return None
    except ValueError:
        config = None
    if isinstance(config, dict):
        return config

    backup = f"{config_file}.bad"
    try:
        os.replace(config_file, backup)
    except OSError as err:
        logger.warning("Not updating invalid configuration %s; backup failed: %s", config_file, err)
        return None
    logger.warning("Invalid configuration %s was moved to %s", config_file, backup)
    return {}


def load_default_playlist(config_file: str = CONFIG_FILE) -> str:
    """Return a configured default playlist, or an empty string when unset."""
    config = _load_config(config_file)

    playlist = config.get("default_playlist", "")
    if not isinstance(playlist, str) or not playlist.strip():
        return ""
    try:
        return validate_media_url(playlist)
    except InvalidMediaURL as err:
        logger.warning("Ignoring invalid default playlist in %s: %s", config_file, err)
        return ""


def load_visualizer_preferences(config_file: str = CONFIG_FILE) -> tuple[str, bool]:
    """Return the preferred style and visibility, using defaults for invalid values."""
    config = _load_config(config_file)
    style = config.get("visualizer_style")
    visible = config.get("show_visualizer")
    return (
        style if style in VISUALIZER_STYLES else "waterfall",
        visible if isinstance(visible, bool) else True,
    )


def load_ytdl_fallback_clients(config_file: str = CONFIG_FILE) -> tuple[str, ...]:
    """Return the YouTube clients to retry a failed track with, in order."""
    value = _load_config(config_file).get("ytdl_fallback_clients")
    if value is None:
        return DEFAULT_YTDL_FALLBACK_CLIENTS
    if not isinstance(value, list) or not all(
        isinstance(name, str) and _CLIENT_NAME.fullmatch(name) for name in value
    ):
        logger.warning("Ignoring invalid ytdl_fallback_clients in %s", config_file)
        return DEFAULT_YTDL_FALLBACK_CLIENTS
    return tuple(dict.fromkeys(value))


def _save_config(config: Dict[str, Any], config_file: str) -> bool:
    try:
        directory = os.path.dirname(config_file) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp_file = tempfile.mkstemp(prefix=".config-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(config, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_file, config_file)
        finally:
            try:
                os.unlink(temp_file)
            except FileNotFoundError:
                pass
        return True
    except OSError as err:
        logger.warning("Unable to save configuration to %s: %s", config_file, err)
        return False


def save_default_playlist(playlist: str, config_file: str = CONFIG_FILE) -> bool:
    """Persist a validated default playlist URL."""
    try:
        playlist = validate_media_url(playlist)
    except InvalidMediaURL as err:
        logger.warning("Unable to save default playlist to %s: %s", config_file, err)
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["default_playlist"] = playlist
    return _save_config(config, config_file)


def save_visualizer_preferences(style: str, visible: bool, config_file: str = CONFIG_FILE) -> bool:
    """Persist the visualizer choice without replacing other configuration."""
    if style not in VISUALIZER_STYLES or not isinstance(visible, bool):
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["visualizer_style"] = style
    config["show_visualizer"] = visible
    return _save_config(config, config_file)


def load_vim_mode(config_file: str = CONFIG_FILE) -> bool:
    """Return whether Vim navigation keys are enabled."""
    value = _load_config(config_file).get("vim_mode")
    return value if isinstance(value, bool) else DEFAULT_VIM_MODE


def save_vim_mode(enabled: bool, config_file: str = CONFIG_FILE) -> bool:
    """Persist the Vim navigation preference without replacing other settings."""
    if not isinstance(enabled, bool):
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["vim_mode"] = enabled
    return _save_config(config, config_file)
