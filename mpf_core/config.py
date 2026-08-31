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


def load_default_playlist(config_file: str = CONFIG_FILE) -> str:
    """Return a configured default playlist, or an empty string when unset."""
    try:
        with open(config_file, encoding="utf-8") as file:
            config: Dict[str, Any] = json.load(file)
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as err:
        logger.warning("Ignoring invalid configuration %s: %s", config_file, err)
        return ""

    playlist = config.get("default_playlist", "")
    if not isinstance(playlist, str) or not playlist.strip():
        return ""
    try:
        return validate_media_url(playlist)
    except InvalidMediaURL as err:
        logger.warning("Ignoring invalid default playlist in %s: %s", config_file, err)
        return ""


def save_default_playlist(playlist: str, config_file: str = CONFIG_FILE) -> bool:
    """Persist a validated default playlist URL."""
    try:
        playlist = validate_media_url(playlist)
        try:
            with open(config_file, encoding="utf-8") as file:
                config: Dict[str, Any] = json.load(file)
        except FileNotFoundError:
            config = {}
        except (OSError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        config["default_playlist"] = playlist
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
    except (InvalidMediaURL, OSError) as err:
        logger.warning("Unable to save default playlist to %s: %s", config_file, err)
        return False
