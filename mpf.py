#!/usr/bin/env -S sh -c 'exec "$(dirname "$0")/.venv/bin/python" "$0" "$@"'

"""mpf - Minimal YouTube Playlist Streaming TUI with MPV, Blessed, and PipeWire."""

import argparse
import logging
import logging.handlers
import os
from typing import List, Optional

from mpf_core.config import load_default_playlist
from mpf_core.paths import CACHE_DIR, CONFIG_FILE, LOG_FILE

DEFAULT_PLAYLIST_URL = ""
__version__ = "1.1.0"  # x-release-please-version


def parse_cli_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Terminal YouTube playlist player with MPV, Kitty previews, and audio visualization"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_PLAYLIST_URL,
        help="YouTube playlist or video URL to play",
    )
    parser.add_argument(
        "--no-auto-play",
        action="store_true",
        help="Do not start playing immediately after loading playlist",
    )
    parser.add_argument(
        "--config",
        default=CONFIG_FILE,
        metavar="PATH",
        help=f"Configuration file (default: {CONFIG_FILE})",
    )
    parser.add_argument(
        "--no-visualizer",
        action="store_true",
        help="Disable the audio visualizer for this session",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Disable Kitty thumbnail previews for this session",
    )
    vim_group = parser.add_mutually_exclusive_group()
    vim_group.add_argument("--vim", dest="vim", action="store_true", help="Enable Vim navigation keys")
    vim_group.add_argument("--no-vim", dest="vim", action="store_false", help="Disable Vim navigation keys")
    parser.set_defaults(vim=None)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(args)


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating log whose files are only readable by their owner, including after a rotation."""

    def _open(self):  # type: ignore[no-untyped-def]
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def configure_logging(
    cache_dir: str = CACHE_DIR, log_file: str = LOG_FILE, max_bytes: int = 512 * 1024, backups: int = 2
) -> None:
    """Log to a private, size-capped file; logging is silenced if the file cannot be used.

    The log can contain track titles and player errors, so the directory is 0700 and the files 0600.
    """
    try:
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        os.chmod(cache_dir, 0o700)
        handler = _PrivateRotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        os.chmod(log_file, 0o600)  # a log created by an older version had the default mode
    except OSError:
        logging.basicConfig(level=logging.CRITICAL)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> None:
    cli_args = parse_cli_args()
    configure_logging()

    # Lazy loading
    import asyncio

    from mpf_core import BlessedMusicPlayer
    
    playlist_url = cli_args.url or load_default_playlist(cli_args.config)
    player = BlessedMusicPlayer(
        playlist_url=playlist_url,
        auto_play=not cli_args.no_auto_play,
        config_file=cli_args.config,
        show_visualizer=None if not cli_args.no_visualizer else False,
        show_preview=not cli_args.no_preview,
        vim_mode=cli_args.vim,
    )
    try:
        asyncio.run(player.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
