"""User configuration loading for MPF."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

from mpf_core.fetcher import InvalidMediaURL, validate_media_url
from mpf_core.paths import CONFIG_FILE

logger = logging.getLogger(__name__)
VISUALIZER_STYLES = ("waterfall", "bars", "braille", "waveform")
DEFAULT_VIM_MODE = True


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
