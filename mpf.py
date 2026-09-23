#!/usr/bin/env -S sh -c 'exec "$(dirname "$0")/.venv/bin/python" "$0" "$@"'

"""mpf - Minimal YouTube Playlist Streaming TUI with MPV, Blessed, and PipeWire."""

import argparse
import logging
import os
from typing import List, Optional

from mpf_core.config import load_default_playlist
from mpf_core.paths import CACHE_DIR, CONFIG_FILE, LOG_FILE

DEFAULT_PLAYLIST_URL = ""
__version__ = "0.1.0"


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


def main() -> None:
    cli_args = parse_cli_args()

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        logging.basicConfig(
            filename=LOG_FILE,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    except OSError:
        logging.basicConfig(level=logging.CRITICAL)

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
