"""User configuration loading for MPF."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict

from mpf_core.fetcher import InvalidMediaURL, validate_media_url
from mpf_core.paths import CONFIG_FILE

logger = logging.getLogger(__name__)
VISUALIZER_STYLES = ("waterfall", "bars", "braille", "waveform")


def _load_config(config_file: str) -> Dict[str, Any]:
    try:
        with open(config_file, encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as err:
        logger.warning("Ignoring invalid configuration %s: %s", config_file, err)
        return {}
    return config if isinstance(config, dict) else {}


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
    config = _load_config(config_file)
    config["default_playlist"] = playlist
    return _save_config(config, config_file)


def save_visualizer_preferences(style: str, visible: bool, config_file: str = CONFIG_FILE) -> bool:
    """Persist the visualizer choice without replacing other configuration."""
    if style not in VISUALIZER_STYLES or not isinstance(visible, bool):
        return False
    config = _load_config(config_file)
    config["visualizer_style"] = style
    config["show_visualizer"] = visible
    return _save_config(config, config_file)
