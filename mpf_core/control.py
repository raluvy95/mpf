"""Control subcommands: ``mpf.py pause``, ``seek +30``, ``status`` and the rest.

Each one is a short-lived client of the running background player: it connects, says hello, sends one
command (or reads the state it is given) and leaves. It never starts a player, so a status bar or a key
binding cannot bring one up by accident.

These run from key bindings and status bars, so they have to start fast. That is why this module talks to
the socket with plain blocking calls and imports almost nothing: no asyncio, logging, numpy or blessed,
which together cost more than everything else a command does.
"""

from __future__ import annotations

import json
import re
import socket
import sys
from typing import Any, Dict, List, Optional, Tuple

from mpf_core.paths import socket_path
from mpf_core.protocol import MAX_SERVER_FRAME, PROTOCOL_VERSION, ProtocolError, decode, encode
from mpf_core.text import (
    ID_LIMIT,
    NAME_LIMIT,
    TITLE_LIMIT,
    URL_LIMIT,
    clean_text,
    format_time,
    watch_url,
)

# Commands that act on the running player. ``quit`` is handled by mpf.py itself, through the daemon's
# plain-text control line, so it also works against a player of any version.
COMMANDS = ("play", "pause", "toggle", "next", "prev", "volume", "seek", "status")
ALL_COMMANDS = COMMANDS + ("quit",)

TIMEOUT_SECONDS = 5.0

# The help section for these commands, as rows of (kind, text) pieces so it can be coloured with the same
# theme argparse uses for the rest of --help. The kinds name theme entries: "cmd" is how argparse shows a
# positional, "label" a placeholder such as PATH, "opt" a long option.
_HELP_TITLE = "control commands (act on the running background player):"
_HELP_ROWS = (
    ([("cmd", "play"), ("", " | "), ("cmd", "pause"), ("", " | "), ("cmd", "toggle"), ("", " | "),
      ("cmd", "next"), ("", " | "), ("cmd", "prev")], ""),
    ([("cmd", "volume"), ("", " ["), ("label", "N"), ("", " | "), ("label", "+N"), ("", " | "),
      ("label", "-N"), ("", "]")], "show or set the volume, 0-100"),
    ([("cmd", "seek"), ("", " [+ | -]"), ("label", "SECONDS")],
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


class PlayerUnavailable(RuntimeError):
    """The background player is not running, or could not be understood."""


# ── Arguments ────────────────────────────────────────────────────────────────


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


# ── Talking to the player ────────────────────────────────────────────────────


class _Session:
    """One blocking connection to the background player."""

    def __init__(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_SECONDS)
        try:
            sock.connect(socket_path())
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as err:
            sock.close()
            raise PlayerUnavailable("MPF is not running in the background") from err
        except OSError as err:
            sock.close()
            raise PlayerUnavailable(f"could not reach the background player: {err}") from err
        self._sock = sock
        self._file = sock.makefile("rb")
        try:
            self._handshake()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._file.close()
        self._sock.close()

    def _line(self) -> bytes:
        try:
            return self._file.readline(MAX_SERVER_FRAME + 1)
        except OSError as err:  # includes the timeout
            raise PlayerUnavailable(f"the background player did not answer: {err}") from err

    def _frame(self) -> Dict[str, Any]:
        line = self._line()
        if not line:
            raise PlayerUnavailable("the background player closed the connection")
        try:
            return decode(line, MAX_SERVER_FRAME)
        except ProtocolError as err:
            raise PlayerUnavailable(f"the background player sent something unreadable: {err}") from err

    def _handshake(self) -> None:
        mismatch = PlayerUnavailable(
            "the background player is an older or newer version of MPF; run `mpf.py quit` and start MPF again"
        )
        self._sock.sendall(encode({"t": "hello", "version": PROTOCOL_VERSION}))
        line = self._line()
        if not line:
            raise PlayerUnavailable("the background player closed the connection")
        try:
            first = decode(line, MAX_SERVER_FRAME)
        except ProtocolError as err:
            # A background player from before the JSON protocol answers with a plain-text line.
            raise mismatch from err
        if first.get("t") == "error" and first.get("code") == "version":
            raise mismatch
        if first.get("t") != "welcome":
            raise PlayerUnavailable("the background player did not greet this client")

    def read_state(self) -> Tuple[Dict[str, Any], List[Any]]:
        """The snapshot and the whole queue, which the player sends right after the welcome."""
        snapshot: Dict[str, Any] = {}
        rows: List[Any] = []
        while True:
            message = self._frame()
            if message.get("t") == "snapshot":
                snapshot = message
            elif message.get("t") == "queue":
                tracks = message.get("tracks")
                if isinstance(tracks, list):
                    rows.extend(tracks)
                if message.get("chunk") == message.get("chunks", 0) - 1:
                    return snapshot, rows

    def send_and_finish(self, commands: List[Dict[str, Any]]) -> Optional[str]:
        """Send commands, then wait for the player to have handled them; returns an error it reported."""
        for command in commands:
            self._sock.sendall(encode({"t": "cmd", **command}))
        self._sock.shutdown(socket.SHUT_WR)  # the player reads our commands, then sees the end and closes
        problem = None
        while True:
            try:
                line = self._file.readline(MAX_SERVER_FRAME + 1)
            except OSError:
                return problem
            if not line:
                return problem
            try:
                message = decode(line, MAX_SERVER_FRAME)
            except ProtocolError:
                continue
            if message.get("t") == "error" and problem is None:
                problem = clean_text(message.get("message"), 200) or "the player refused the command"


# ── Status ───────────────────────────────────────────────────────────────────


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def status_info(snapshot: Dict[str, Any], rows: List[Any]) -> Dict[str, Any]:
    index = snapshot.get("current_idx")
    row = rows[index] if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(rows) else None
    track = row if isinstance(row, (list, tuple)) and len(row) >= 2 else None
    if track is None or (snapshot.get("idle") and not snapshot.get("buffering")):
        state = "stopped"
    else:
        state = "paused" if snapshot.get("paused") else "playing"
    video_id = clean_text(track[0], ID_LIMIT) if track else ""
    return {
        "state": state,
        "title": clean_text(track[1], TITLE_LIMIT) if track else "",
        "uploader": clean_text(track[3], NAME_LIMIT) if track and len(track) > 3 else "",
        "id": video_id,
        "url": watch_url(video_id) if video_id else "",
        "position": round(_number(snapshot.get("position")), 1),
        "duration": round(_number(snapshot.get("duration")), 1),
        "volume": int(_number(snapshot.get("volume"), 100)),
        "muted": bool(snapshot.get("muted")),
        "repeat": clean_text(snapshot.get("repeat"), 8),
        "shuffle": bool(snapshot.get("shuffle")),
        "index": index if track else None,
        "count": len(rows),
        "playlist_url": clean_text(snapshot.get("playlist_url"), URL_LIMIT),
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


# ── Entry point ──────────────────────────────────────────────────────────────


def _command_for(command: str, value: Optional[str], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """The protocol command a control command stands for; ``snapshot`` is only read when it is needed."""
    if command == "play":
        return {"name": "set_paused", "paused": False}
    if command == "pause":
        return {"name": "set_paused", "paused": True}
    if command == "toggle":
        return {"name": "set_paused", "paused": not snapshot.get("paused")}
    if command == "next":
        return {"name": "next"}
    if command == "prev":
        return {"name": "prev"}
    if command == "volume":
        relative, amount = parse_volume(value or "")
        current = int(_number(snapshot.get("volume"), 100))
        return {"name": "set_volume", "volume": max(0, min(100, current + amount if relative else amount))}
    relative, seconds = parse_seek(value or "")
    if relative:
        return {"name": "seek_rel", "seconds": seconds}
    return {"name": "seek_abs", "seconds": max(0.0, seconds)}


def _needs_state(command: str, value: Optional[str]) -> bool:
    if command in ("status", "toggle"):
        return True
    return command == "volume" and (value is None or parse_volume(value)[0])


def run(command: str, value: Optional[str], as_json: bool) -> int:
    """Run one control command against the background player; returns the process exit code."""
    problem = check_usage(command, value, as_json)
    if problem:
        print(f"Error: {problem}.", file=sys.stderr)
        return 2
    try:
        session = _Session()
    except PlayerUnavailable as err:
        print(f"Error: {err}.", file=sys.stderr)
        return 1
    try:
        snapshot: Dict[str, Any] = {}
        rows: List[Any] = []
        if _needs_state(command, value):
            snapshot, rows = session.read_state()
        if command == "status":
            info = status_info(snapshot, rows)
            print(json.dumps(info, ensure_ascii=False) if as_json else format_status(info))
            return 0
        if command == "volume" and value is None:
            print(status_info(snapshot, rows)["volume"])
            return 0
        refused = session.send_and_finish([_command_for(command, value, snapshot)])
        if refused:
            print(f"Error: {refused}.", file=sys.stderr)
            return 1
        return 0
    except PlayerUnavailable as err:
        print(f"Error: {err}.", file=sys.stderr)
        return 1
    finally:
        session.close()
