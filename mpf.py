#!/usr/bin/env -S sh -c 'exec "$(dirname "$0")/.venv/bin/python" "$0" "$@"'

"""mpf - Minimal YouTube Playlist Streaming TUI with MPV, Blessed, and PipeWire."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    import argparse

# Only the light modules are imported here. The control commands (mpf.py pause, status...) run from key bindings
# and status bars, so everything heavy (logging, asyncio, the player) is imported only by the paths that need it.
from mpf_core.paths import CACHE_DIR, CONFIG_FILE, LOG_FILE

DEFAULT_PLAYLIST_URL = ""
__version__ = "1.3.0"  # x-release-please-version


def _help_formatter_class() -> Any:
    """Keeps the epilog as written, and draws the control commands in the colours of the rest of --help."""
    import argparse

    class HelpFormatter(argparse.RawDescriptionHelpFormatter):
        def _format_text(self, text: str) -> str:
            from mpf_core.control import USAGE_HELP, usage_help

            if text == USAGE_HELP:
                theme = getattr(self, "_theme", None)  # Python 3.14: empty strings when colour is off
                return usage_help(theme) + "\n\n"
            return super()._format_text(text)

    return HelpFormatter


def parse_cli_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    import argparse

    from mpf_core.control import ALL_COMMANDS, USAGE_HELP, check_usage

    parser = argparse.ArgumentParser(
        description="Terminal YouTube playlist player with MPV, Kitty previews, and audio visualization",
        epilog=USAGE_HELP,
        formatter_class=_help_formatter_class(),
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_PLAYLIST_URL,
        help="YouTube playlist or video URL to play, or a control command (see below)",
    )
    parser.add_argument("value", nargs="?", help=argparse.SUPPRESS)  # the argument of a control command
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
    parser.add_argument(
        "--force-cache",
        action="store_true",
        help="Fetch the playlist from YouTube again and replace its cached copy, even if the cache is fresh",
    )
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)  # internal: the background player
    vim_group = parser.add_mutually_exclusive_group()
    vim_group.add_argument("--vim", dest="vim", action="store_true", help="Enable Vim navigation keys")
    vim_group.add_argument("--no-vim", dest="vim", action="store_false", help="Disable Vim navigation keys")
    parser.set_defaults(vim=None)
    parser.add_argument("--json", action="store_true", help="With status: print it as JSON")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parsed = parser.parse_args(args)
    if parsed.url in ALL_COMMANDS:
        problem = check_usage(parsed.url, parsed.value, parsed.json) if parsed.url != "quit" else (
            "quit takes no argument" if parsed.value is not None or parsed.json else None
        )
    elif parsed.value is not None or parsed.json:
        problem = "unexpected argument" if parsed.value is not None else "--json only applies to the status command"
    else:
        problem = None
    if problem:
        parser.error(problem)
    return parsed


def _private_log_handler(log_file: str, max_bytes: int, backups: int) -> Any:
    """A rotating log handler whose files are only readable by their owner, including after a rotation."""
    import logging.handlers

    class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
        def _open(self):  # type: ignore[no-untyped-def]
            fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    return PrivateRotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")


def configure_logging(
    cache_dir: str = CACHE_DIR, log_file: str = LOG_FILE, max_bytes: int = 512 * 1024, backups: int = 2
) -> None:
    """Log to a private, size-capped file; logging is silenced if the file cannot be used.

    The log can contain track titles and player errors, so the directory is 0700 and the files 0600.
    """
    import logging

    from mpf_core.metadata import RedactingFilter

    try:
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        os.chmod(cache_dir, 0o700)
        handler = _private_log_handler(log_file, max_bytes, backups)
        os.chmod(log_file, 0o600)  # a log created by an older version had the default mode
    except OSError:
        logging.basicConfig(level=logging.CRITICAL)
        return
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _fast_control(argv: List[str]) -> Optional[int]:
    """Run a plain control command without building the argument parser, or return None to use the parser.

    Importing argparse alone costs about as much as everything else a command does, and these run from key
    bindings and status bars. Only the simple valid forms are taken here (``pause``, ``volume 40``,
    ``seek -30``, ``status --json``); anything else, including every mistake, goes to the parser so its
    errors and help stay exactly as they are.
    """
    from mpf_core import control

    if not argv or argv[0] not in control.COMMANDS:
        return None
    as_json = "--json" in argv[1:]
    rest = [arg for arg in argv[1:] if arg != "--json"]
    if len(rest) > 1 or any(arg.startswith("--") for arg in rest):
        return None
    value = rest[0] if rest else None
    if control.check_usage(argv[0], value, as_json):
        return None
    try:
        return control.run(argv[0], value, as_json)
    except KeyboardInterrupt:
        return 130


def main(argv: Optional[List[str]] = None) -> None:
    fast = _fast_control(sys.argv[1:] if argv is None else argv)
    if fast is not None:
        raise SystemExit(fast)
    cli_args = parse_cli_args(argv)

    # Lazy loading. The control commands are one-shot clients of the running player and must start fast, so
    # they are dispatched first, before anything heavy is imported: no log file, no asyncio, no player.
    from mpf_core import control

    if cli_args.url in control.COMMANDS and not cli_args.daemon:
        try:
            exit_code = control.run(cli_args.url, cli_args.value, cli_args.json)
        except KeyboardInterrupt:
            exit_code = 130
        raise SystemExit(exit_code)

    import asyncio

    configure_logging()

    from mpf_core import daemon

    if cli_args.daemon:
        # The background player runs the engine alone: no terminal, numpy or blessed.
        from mpf_core.config import load_default_playlist
        from mpf_core.engine import PlaybackEngine

        engine = PlaybackEngine(
            playlist_url=cli_args.url or load_default_playlist(cli_args.config),
            auto_play=not cli_args.no_auto_play,
            config_file=cli_args.config,
            headless=True,
            force_refresh=cli_args.force_cache,
        )
        try:
            asyncio.run(engine.run_headless())
        except KeyboardInterrupt:
            pass
        return

    if cli_args.url == "quit":
        try:
            stopped = daemon.stop()
        except TimeoutError as err:
            print(f"Error: {err}.")
            raise SystemExit(1) from err
        print("Stopped the MPF background player." if stopped else "MPF is not running in the background.")
        return

    from mpf_core import MediaPlayerFluere
    from mpf_core.client import DaemonUnavailable

    # The background player owns playback. This only draws it, so a launch attaches to the running one
    # (starting it first when there is none) and leaving it just disconnects.
    player = MediaPlayerFluere(
        playlist_url=cli_args.url,
        auto_play=not cli_args.no_auto_play,
        config_file=cli_args.config,
        show_visualizer=False if cli_args.no_visualizer else None,
        show_preview=not cli_args.no_preview,
        vim_mode=cli_args.vim,
        force_refresh=cli_args.force_cache,
    )
    try:
        asyncio.run(player.run())
    except DaemonUnavailable as err:
        print(f"Error: {err}.")
        raise SystemExit(1) from err
    except KeyboardInterrupt:
        player.close()

    if player.music_continues:
        print(f"MPF is still playing in the background. Stop it with: {sys.argv[0]} quit")


if __name__ == "__main__":
    main()
