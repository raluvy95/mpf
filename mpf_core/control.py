"""Control subcommands: ``mpf.py pause``, ``seek +30``, ``status`` and the rest.

Each one is a short-lived client of the running background player: it attaches, sends one command (or reads
the state it was given on attaching) and leaves. It never starts a player, so a status bar or a key binding
cannot bring one up by accident. Imports neither numpy nor blessed.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, Optional, Tuple

from mpf_core.client import DaemonUnavailable, RemoteEngine
from mpf_core.models import format_time

# Commands that act on the running player. ``quit`` is handled by mpf.py itself, through the daemon's
# plain-text control line, so it also works against a player of any version.
COMMANDS = ("play", "pause", "toggle", "next", "prev", "volume", "seek", "status")
ALL_COMMANDS = COMMANDS + ("quit",)

# The help section for these commands, as rows of (kind, text) pieces so it can be coloured with the same
# theme argparse uses for the rest of --help. The kinds name theme entries: "cmd" is how argparse shows a
# positional, "label" a placeholder such as PATH, "opt" a long option.
_HELP_TITLE = "control commands (act on the running background player):"
_HELP_ROWS = (
    ([("cmd", "play"), ("", " | "), ("cmd", "pause"), ("", " | "), ("cmd", "toggle"), ("", " | "),
      ("cmd", "next"), ("", " | "), ("cmd", "prev")], ""),
    ([("cmd", "volume"), ("", " ["), ("label", "N"), ("", " | "), ("label", "+N"), ("", " | "),
      ("label", "-N"), ("", "]")], "show or set the volume, 0-100"),
    ([("cmd", "seek"), ("", " [+|-]"), ("label", "SECONDS")],
     "jump to, or by, a time; M:SS and H:MM:SS work too"),
    ([("cmd", "status"), ("", " ["), ("opt", "--json"), ("", "]")], "what is playing"),
    ([("cmd", "quit")], "stop the background player"),
)
_THEME_ATTRS = {"cmd": "action", "label": "label", "opt": "long_option"}


def usage_help(theme: Any = None) -> str:
    """The control-commands section of --help; ``theme`` is argparse's theme (empty strings when not coloured)."""

    def paint(kind: str, text: str) -> str:
        if theme is None or not kind:
            return text
        return f"{getattr(theme, _THEME_ATTRS[kind], '')}{text}{getattr(theme, 'reset', '')}"

    heading = f"{getattr(theme, 'heading', '')}{_HELP_TITLE}{getattr(theme, 'reset', '')}" if theme else _HELP_TITLE
    column = max(sum(len(text) for _kind, text in pieces) for pieces, description in _HELP_ROWS if description) + 4
    lines = [heading]
    for pieces, description in _HELP_ROWS:
        plain_width = sum(len(text) for _kind, text in pieces)
        line = "  " + "".join(paint(kind, text) for kind, text in pieces)
        lines.append(line + (" " * (column - plain_width) + description if description else ""))
    return "\n".join(lines)


USAGE_HELP = usage_help()  # uncoloured

_TIME = re.compile(r"^(?P<sign>[+-]?)(?P<parts>\d+(?:\.\d+)?(?::\d{1,2}(?:\.\d+)?){0,2})$")
_VOLUME = re.compile(r"^(?P<sign>[+-]?)(?P<amount>\d{1,3})$")


class ControlError(ValueError):
    """A control command used wrongly."""


def check_usage(command: str, value: Optional[str], as_json: bool) -> Optional[str]:
    """The complaint about a command's arguments, or None when they are fine."""
    if as_json and command != "status":
        return "--json only applies to the status command"
    if command == "seek":
        if value is None:
            return "seek needs a time, for example: seek +30, seek -10 or seek 1:30"
        parser = parse_seek
    elif command == "volume":
        if value is None:
            return None  # shows the current volume
        parser = parse_volume
    else:
        return f"{command} takes no argument" if value is not None else None
    try:
        parser(value)
    except ControlError as err:
        return str(err)
    return None


def parse_volume(text: str) -> Tuple[bool, int]:
    """(relative, amount): ``40`` sets the volume, ``+5`` and ``-5`` change it."""
    match = _VOLUME.match(text.strip())
    if not match:
        raise ControlError(f"volume must be a number from 0 to 100, or +N or -N (got {text!r})")
    amount = int(match["amount"])
    if match["sign"] == "-":
        return True, -amount
    if match["sign"] == "+":
        return True, amount
    if amount > 100:
        raise ControlError(f"volume must be from 0 to 100 (got {amount})")
    return False, amount


def parse_seek(text: str) -> Tuple[bool, float]:
    """(relative, seconds): ``90`` and ``1:30`` jump to a time, ``+30`` and ``-1:05`` move by one."""
    match = _TIME.match(text.strip())
    if not match:
        raise ControlError(f"seek takes seconds or M:SS, with an optional + or - (got {text!r})")
    seconds = 0.0
    for part in match["parts"].split(":"):
        seconds = seconds * 60 + float(part)
    if match["sign"] == "-":
        return True, -seconds
    return match["sign"] == "+", seconds


def state_name(client: RemoteEngine) -> str:
    """playing, paused or stopped, by the same rule as the desktop media widget."""
    if not client.queue.current_track or (client._mpv_idle and not client._is_buffering):
        return "stopped"
    return "paused" if client._is_paused else "playing"


def status_info(client: RemoteEngine) -> Dict[str, Any]:
    track = client.queue.current_track
    return {
        "state": state_name(client),
        "title": track.title if track else "",
        "uploader": track.uploader if track else "",
        "id": track.id if track else "",
        "url": track.url if track else "",
        "position": round(client._time_pos, 1),
        "duration": round(client._duration, 1),
        "volume": client._volume,
        "muted": client._is_muted,
        "repeat": client.queue.repeat_mode.value,
        "shuffle": client.queue.shuffle_enabled,
        "index": client.queue.current_idx if track else None,
        "count": len(client.queue.tracks),
        "playlist_url": client.playlist_url,
    }


def format_status(info: Dict[str, Any]) -> str:
    if info["state"] == "stopped":
        return "Stopped: nothing playing"
    detail = [info["uploader"]] if info["uploader"] else []
    detail.append(f"{format_time(info['position'])} / {format_time(info['duration'])}")
    detail.append(f"volume {info['volume']}%" + (" (muted)" if info["muted"] else ""))
    detail.append(f"repeat {info['repeat']}")
    detail.append("shuffle " + ("on" if info["shuffle"] else "off"))
    detail.append(f"track {info['index'] + 1} of {info['count']}")
    return f"{info['state'].capitalize()}: {info['title']}\n  " + " · ".join(detail)


async def run(command: str, value: Optional[str], as_json: bool, config_file: str) -> int:
    """Run one control command against the background player; returns the process exit code."""
    problem = check_usage(command, value, as_json)
    if problem:
        print(f"Error: {problem}.", file=sys.stderr)
        return 2
    client = RemoteEngine(config_file=config_file)
    try:
        await client.attach(start_daemon=False)
    except DaemonUnavailable as err:
        print(f"Error: {err}.", file=sys.stderr)
        return 1
    try:
        if command == "status":
            info = status_info(client)
            print(json.dumps(info, ensure_ascii=False) if as_json else format_status(info))
        elif command == "volume" and value is None:
            print(client._volume)
        elif command == "volume":
            relative, amount = parse_volume(value or "")
            client.set_volume(client._volume + amount if relative else amount)
        elif command == "seek":
            relative, seconds = parse_seek(value or "")
            if relative:
                client.seek_relative(seconds)
            else:
                client.seek_absolute(seconds)
        elif command == "play":
            client.set_paused(False)
        elif command == "pause":
            client.set_paused(True)
        elif command == "toggle":
            client.set_paused(not client._is_paused)
        elif command == "next":
            client.next_track()
        elif command == "prev":
            client.previous_track()
        await client.flush()
    finally:
        client.close()
    return 0
