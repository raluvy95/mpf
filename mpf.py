#!/usr/bin/env -S sh -c 'exec "$(dirname "$0")/.venv/bin/python" "$0" "$@"'

"""mpf - Minimal YouTube Playlist Streaming TUI with MPV, Blessed, and PipeWire."""

import argparse
import logging
import os
from typing import List, Optional

from mpf_core.config import load_default_playlist
from mpf_core.paths import CACHE_DIR, LOG_FILE

DEFAULT_PLAYLIST_URL = ""


def parse_cli_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transparent TUI YouTube streamer with Blessed, MPV, Kitty icat, and PipeWire")
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
    
    playlist_url = cli_args.url or load_default_playlist()
    player = BlessedMusicPlayer(playlist_url=playlist_url, auto_play=not cli_args.no_auto_play)
    try:
        asyncio.run(player.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
