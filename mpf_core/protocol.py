"""Wire protocol between the playback daemon and its clients.

One UTF-8 JSON object per line, each with a string type ``t``. Both sides distrust what they read: lines
are size-capped, must decode to an object, and commands are checked against a fixed table of argument
names and types before anything acts on them.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

PROTOCOL_VERSION = 1

MAX_CLIENT_LINE = 64 * 1024  # a command is tiny; anything longer is not a client speaking the protocol
MAX_SERVER_FRAME = 1024 * 1024  # queue frames are chunked well below this
MAX_BACKLOG = 4 * 1024 * 1024  # unsent bytes a client may fall behind by before it is dropped
QUEUE_CHUNK = 500  # tracks per queue frame
POSITION_INTERVAL = 0.1  # seconds between position updates

# Argument name -> (allowed types, required). A bool is never accepted where an int is wanted.
_NUMBER = (int, float)
_OPTIONAL_STR = (str, type(None))
_OPTIONAL_LIST = (list, type(None))
COMMANDS: Dict[str, Dict[str, Tuple[Tuple[type, ...], bool]]] = {
    "play_index": {"index": ((int,), True)},
    "set_paused": {"paused": ((bool,), True)},
    "next": {},
    "prev": {},
    "seek_rel": {"seconds": (_NUMBER, True)},
    "seek_abs": {"seconds": (_NUMBER, True)},
    "set_volume": {"volume": ((int,), True)},
    "set_mute": {"muted": ((bool,), True)},
    "set_shuffle": {"enabled": ((bool,), True)},
    "cycle_repeat": {},
    "load_url": {"url": ((str,), True), "auto_play": ((bool,), False), "force": ((bool,), False)},
    "force_refresh": {},
    "set_playback_config": {"default_playlist": (_OPTIONAL_STR, False), "fallback_clients": (_OPTIONAL_LIST, False)},
    "shutdown": {},
}


class ProtocolError(ValueError):
    """A line that is not a valid protocol message."""


def encode(message: Dict[str, Any], limit: int = MAX_SERVER_FRAME) -> bytes:
    """Serialise one message as a compact JSON line."""
    if not isinstance(message.get("t"), str):
        raise ProtocolError("message has no type")
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > limit:
        raise ProtocolError(f"message is larger than {limit} bytes")
    return data


def decode(line: bytes, limit: int = MAX_CLIENT_LINE) -> Dict[str, Any]:
    """Parse one line into a message, or raise ProtocolError."""
    if len(line) > limit:
        raise ProtocolError(f"line is larger than {limit} bytes")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise ProtocolError(f"not a JSON line: {err}") from err
    if not isinstance(message, dict) or not isinstance(message.get("t"), str):
        raise ProtocolError("not a message object with a type")
    return message


def _type_ok(value: Any, types: Tuple[type, ...]) -> bool:
    if isinstance(value, bool) and bool not in types:
        return False  # bool is a subclass of int; True must not pass as an index or a volume
    return isinstance(value, types)


def validate_command(message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Return (command, arguments) for a valid ``cmd`` message, or raise ProtocolError."""
    name = message.get("name")
    if message.get("t") != "cmd" or not isinstance(name, str) or name not in COMMANDS:
        raise ProtocolError(f"unknown command {name!r}")
    spec = COMMANDS[name]
    args = {key: value for key, value in message.items() if key not in ("t", "name")}
    extra = set(args) - set(spec)
    if extra:
        raise ProtocolError(f"{name}: unexpected arguments {sorted(extra)}")
    for key, (types, required) in spec.items():
        if key not in args:
            if required:
                raise ProtocolError(f"{name}: missing {key}")
            continue
        if not _type_ok(args[key], types):
            raise ProtocolError(f"{name}: {key} has the wrong type")
    fallback = args.get("fallback_clients")
    if fallback is not None and not all(isinstance(item, str) for item in fallback):
        raise ProtocolError(f"{name}: fallback_clients must be strings")
    return name, args
