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
VISUALIZER_STYLES = ("waterfall", "bars", "braille", "waveform", "fewbars", "thin", "galaxy", "rainbow")
# Where bar-type styles (bars, fewbars, thin, rainbow) are anchored; independent of the style.
BAR_POSITIONS = ("bottom", "top")
# "rainbow" colours any style's rows by height with rainbow_colors; "plain" keeps the default cyan.
COLOR_MODES = ("plain", "rainbow")
# Terminal palette names, so the colours follow the terminal's own theme.
_BASE_COLORS = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
PALETTE_COLORS = _BASE_COLORS + tuple(f"bright_{name}" for name in _BASE_COLORS)
# Rainbow rows run from the bottom (quiet) to the top (loud); two names make a two-colour split.
DEFAULT_RAINBOW_COLORS = ("blue", "cyan", "green", "yellow", "red")
DEFAULT_VIM_MODE = True
# yt-dlp picks the YouTube client itself. When mpv fails a track (YouTube's media server sometimes
# answers HTTP 403, or yt-dlp errors out), the track is first retried once with yt-dlp's own choice
# again (a fresh URL is usually enough), then with these clients in order, then skipped. Edit the
# list with the "ytdl_fallback_clients" key in config.json; an empty list leaves only the first retry.
# Which clients work changes as YouTube changes; names are yt-dlp "player_client" values.
DEFAULT_YTDL_FALLBACK_CLIENTS = ("tv_simply", "mweb")
_CLIENT_NAME = re.compile(r"[A-Za-z0-9_]+")  # ends up inside an mpv option string: no commas or '='


MAX_CONFIG_BYTES = 1024 * 1024


def _parse_config(config_file: str) -> Any:
    with open(config_file, encoding="utf-8") as file:
        text = file.read(MAX_CONFIG_BYTES + 1)
    if len(text) > MAX_CONFIG_BYTES:
        raise ValueError(f"configuration is larger than {MAX_CONFIG_BYTES} bytes")
    return json.loads(text)


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


def parse_ytdl_fallback_clients(text: str) -> Optional[tuple[str, ...]]:
    """Parse comma or space separated client names; None when any name is invalid."""
    names = [name for name in re.split(r"[,\s]+", text.strip()) if name]
    if not all(_CLIENT_NAME.fullmatch(name) for name in names):
        return None
    return tuple(dict.fromkeys(names))


def save_ytdl_fallback_clients(clients: tuple[str, ...], config_file: str = CONFIG_FILE) -> bool:
    """Persist the fallback client list without replacing other settings."""
    if not all(isinstance(name, str) and _CLIENT_NAME.fullmatch(name) for name in clients):
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["ytdl_fallback_clients"] = list(clients)
    return _save_config(config, config_file)


def parse_rainbow_colors(text: str) -> Optional[tuple[str, ...]]:
    """Parse comma or space separated palette names (at least two); None when invalid."""
    names = tuple(name for name in re.split(r"[,\s]+", text.strip().lower()) if name)
    if len(names) < 2 or not all(name in PALETTE_COLORS for name in names):
        return None
    return names


def load_rainbow_colors(config_file: str = CONFIG_FILE) -> tuple[str, ...]:
    """Return the rainbow colours, bottom to top, using the default for invalid values."""
    value = _load_config(config_file).get("rainbow_colors")
    if value is None:
        return DEFAULT_RAINBOW_COLORS
    if isinstance(value, list):
        colors = parse_rainbow_colors(",".join(map(str, value)))
        if colors:
            return colors
    logger.warning("Ignoring invalid rainbow_colors in %s", config_file)
    return DEFAULT_RAINBOW_COLORS


def save_rainbow_colors(colors: tuple[str, ...], config_file: str = CONFIG_FILE) -> bool:
    """Persist the rainbow colours without replacing other settings."""
    if parse_rainbow_colors(",".join(colors)) != tuple(colors):
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["rainbow_colors"] = list(colors)
    return _save_config(config, config_file)


def load_bar_position(config_file: str = CONFIG_FILE) -> str:
    """Return where bars are anchored, using "bottom" for invalid values."""
    value = _load_config(config_file).get("bar_position")
    return value if value in BAR_POSITIONS else "bottom"


def save_bar_position(position: str, config_file: str = CONFIG_FILE) -> bool:
    """Persist the bar position without replacing other settings."""
    if position not in BAR_POSITIONS:
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["bar_position"] = position
    return _save_config(config, config_file)


def load_color_mode(config_file: str = CONFIG_FILE) -> str:
    """Return the visualizer colour mode, using "plain" for invalid values."""
    value = _load_config(config_file).get("visualizer_color")
    return value if value in COLOR_MODES else "plain"


def save_color_mode(mode: str, config_file: str = CONFIG_FILE) -> bool:
    """Persist the visualizer colour mode without replacing other settings."""
    if mode not in COLOR_MODES:
        return False
    config = _load_config_for_update(config_file)
    if config is None:
        return False
    config["visualizer_color"] = mode
    return _save_config(config, config_file)
