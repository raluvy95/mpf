"""Serves the playback engine to any number of clients over the daemon's private socket.

A client says ``hello``, gets a ``snapshot`` of the engine's state and the queue in chunks, then a
``state`` frame with the changed fields whenever something changes (position at most every
``POSITION_INTERVAL``). It sends ``cmd`` messages, which are validated before the engine acts on them.
The plain-text lines ``ping`` and ``quit`` are also answered on the same socket (``mpf.py quit`` uses them).

Imports neither numpy nor blessed: this runs in the resident background player.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from mpf_core import daemon
from mpf_core.config import (
    parse_ytdl_fallback_clients,
    save_default_playlist,
    save_ytdl_fallback_clients,
)
from mpf_core.fetcher import InvalidMediaURL, validate_media_url
from mpf_core.protocol import (
    MAX_BACKLOG,
    MAX_CLIENT_LINE,
    POSITION_INTERVAL,
    PROTOCOL_VERSION,
    QUEUE_CHUNK,
    ProtocolError,
    decode,
    encode,
    validate_command,
)

if TYPE_CHECKING:
    from mpf_core.engine import PlaybackEngine

logger = logging.getLogger(__name__)

_LEGACY = {"ping", "quit"}


class PlayerServer:
    def __init__(self, engine: "PlaybackEngine") -> None:
        self._engine = engine
        self._clients: Set[asyncio.StreamWriter] = set()
        self._last: Dict[str, Any] = {}
        self._queue_revision: Optional[int] = None
        self._flush_scheduled = False
        self._ticker: Optional[asyncio.Task[None]] = None
        self._daemon_server: Optional[daemon.DaemonServer] = None

    async def start(self) -> None:
        """Listen on the daemon socket; raises daemon.DaemonAlreadyRunning if another daemon owns it."""
        engine = self._engine
        self._daemon_server = await daemon.serve(
            engine._request_quit, handler=self._handle, line_limit=MAX_CLIENT_LINE
        )
        engine.on_change = self._schedule_flush
        self._last = engine.state()
        self._queue_revision = engine.queue.revision
        self._ticker = asyncio.create_task(self._tick())

    def close(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        if self._daemon_server:
            self._daemon_server.close()
            self._daemon_server = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ── Broadcasting ─────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(POSITION_INTERVAL)
            self.flush()

    def _schedule_flush(self) -> None:
        """Send a change at once, but only once for a burst of changes in the same loop turn."""
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        asyncio.get_running_loop().call_soon(self.flush)

    def flush(self) -> None:
        """Send every client the fields that changed since the last flush, and the queue if it changed."""
        self._flush_scheduled = False
        state = self._engine.state()
        changed = {key: value for key, value in state.items() if self._last.get(key) != value}
        self._last = state
        if self._engine.queue.revision != self._queue_revision:
            self._queue_revision = self._engine.queue.revision
            frames = self._queue_frames()
            for writer in list(self._clients):
                for frame in frames:
                    self._send(writer, frame)
        if changed:
            frame = encode({"t": "state", **changed})
            for writer in list(self._clients):
                self._send(writer, frame)

    def _queue_frames(self) -> List[bytes]:
        tracks = [list(track.to_cache_tuple()) for track in self._engine.queue.tracks]
        chunks = [tracks[i : i + QUEUE_CHUNK] for i in range(0, len(tracks), QUEUE_CHUNK)] or [[]]
        revision = self._engine.queue.revision
        return [
            encode({"t": "queue", "revision": revision, "chunk": index, "chunks": len(chunks), "tracks": chunk})
            for index, chunk in enumerate(chunks)
        ]

    def _send(self, writer: asyncio.StreamWriter, frame: bytes) -> None:
        """Queue a frame for a client, dropping a client that has stopped reading."""
        if writer.is_closing():
            self._clients.discard(writer)
            return
        if writer.transport.get_write_buffer_size() + len(frame) > MAX_BACKLOG:
            logger.warning("Dropping a client that fell %d bytes behind", MAX_BACKLOG)
            self._clients.discard(writer)
            writer.close()
            return
        writer.write(frame)

    # ── Connections ──────────────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.readline(), 2.0)
            command = first.decode(errors="replace").strip()
            if command in _LEGACY:
                reply = daemon.legacy_reply(command, self._engine._request_quit)
                writer.write(reply.encode() + b"\n")
                await writer.drain()
                return
            hello = decode(first)
            if hello.get("t") != "hello" or hello.get("version") != PROTOCOL_VERSION:
                writer.write(encode({"t": "error", "code": "version", "version": PROTOCOL_VERSION}))
                await writer.drain()
                return
            writer.write(encode({"t": "welcome", "version": PROTOCOL_VERSION}))
            writer.write(encode({"t": "snapshot", **self._engine.state()}))
            for frame in self._queue_frames():
                writer.write(frame)
            await writer.drain()
            self._clients.add(writer)
            while True:
                line = await reader.readline()
                if not line:
                    return
                self._command(writer, line)
        except (asyncio.TimeoutError, ConnectionError, ProtocolError, ValueError, asyncio.IncompleteReadError):
            pass  # a garbled, oversized or silent client is simply disconnected
        finally:
            self._clients.discard(writer)
            writer.close()

    def _command(self, writer: asyncio.StreamWriter, line: bytes) -> None:
        try:
            name, args = validate_command(decode(line))
        except ProtocolError as err:
            self._send(writer, encode({"t": "error", "code": "command", "message": str(err)}))
            return
        engine = self._engine
        if name == "play_index":
            if 0 <= args["index"] < len(engine.queue.tracks):
                engine._play_index(args["index"])
        elif name == "set_paused":
            engine.set_paused(args["paused"])
        elif name == "next":
            engine.next_track()
        elif name == "prev":
            engine.previous_track()
        elif name == "seek_rel":
            engine.seek_relative(float(args["seconds"]))
        elif name == "seek_abs":
            engine.seek_absolute(float(args["seconds"]))
        elif name == "set_volume":
            engine.set_volume(args["volume"])
        elif name == "set_mute":
            engine.set_mute(args["muted"])
        elif name == "set_shuffle":
            engine.set_shuffle(args["enabled"])
        elif name == "cycle_repeat":
            engine._cycle_repeat()
        elif name == "load_url":
            self._load_url(writer, args)
        elif name == "force_refresh":
            if engine.playlist_url:
                engine.load_url(engine.playlist_url, force=True)
        elif name == "set_playback_config":
            self._set_playback_config(writer, args)
        elif name == "shutdown":
            engine._request_quit()
        engine._notify()

    def _load_url(self, writer: asyncio.StreamWriter, args: Dict[str, Any]) -> None:
        try:
            url = validate_media_url(args["url"])
        except InvalidMediaURL as err:
            self._send(writer, encode({"t": "error", "code": "load_url", "message": str(err)}))
            return
        self._engine.load_url(url, auto_play=args.get("auto_play"), force=bool(args.get("force")))
        self._send(writer, encode({"t": "ack", "name": "load_url"}))

    def _set_playback_config(self, writer: asyncio.StreamWriter, args: Dict[str, Any]) -> None:
        engine = self._engine
        problems = []
        playlist = args.get("default_playlist")
        if playlist is not None and not save_default_playlist(playlist, engine.config_file):
            problems.append("default playlist")
        clients = args.get("fallback_clients")
        if clients is not None:
            parsed = parse_ytdl_fallback_clients(" ".join(clients))
            if parsed is None or not save_ytdl_fallback_clients(parsed, engine.config_file):
                problems.append("fallback clients")
            else:
                engine._ytdl_fallback_clients = parsed
        reply = {"t": "error", "code": "config", "message": "could not save " + ", ".join(problems)} if problems else {
            "t": "ack",
            "name": "set_playback_config",
        }
        self._send(writer, encode(reply))

