"""Client side of the playback daemon: a local mirror of its state, and the commands that drive it.

``RemoteEngine`` offers the names the TUI already reads from a playback engine (``queue``, ``_is_paused``,
``_time_pos`` and so on), filled from the daemon's frames, with command methods in place of the engine's
controls. Nothing here draws anything, so it imports neither numpy nor blessed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from mpf_core import daemon
from mpf_core.config import load_ytdl_fallback_clients
from mpf_core.metadata import clean_text
from mpf_core.models import RepeatMode, Track, TrackQueue
from mpf_core.paths import CONFIG_FILE
from mpf_core.protocol import MAX_SERVER_FRAME, PROTOCOL_VERSION, ProtocolError, decode, encode

logger = logging.getLogger(__name__)

STARTUP_TIMEOUT_SECONDS = 15.0  # how long to wait for a daemon this client has just started
HANDSHAKE_TIMEOUT_SECONDS = 5.0
RECONNECT_ATTEMPTS = 3


class DaemonUnavailable(RuntimeError):
    """The background player is not running and could not be started."""


class VersionMismatch(DaemonUnavailable):
    """The running background player speaks a different protocol version."""


class RemoteEngine:
    def __init__(
        self,
        playlist_url: str = "",
        auto_play: bool = True,
        config_file: str = CONFIG_FILE,
        force_refresh: bool = False,
    ) -> None:
        self._requested_url = playlist_url  # what this launch asked for; empty means "whatever is playing"
        self.playlist_url = playlist_url  # replaced by the daemon's own once attached
        self.auto_play = auto_play
        self.config_file = config_file
        self._force_refresh = force_refresh
        self.queue = TrackQueue()
        self.started_daemon = False  # this client had to start the background player

        self._running = True
        self._connected = False
        self._is_paused = False
        self._is_muted = False
        self._is_buffering = False
        self._mpv_idle = True
        self._is_loading_playlist = False
        self._volume = 100
        self._time_pos = 0.0
        self._duration = 0.0
        self._status_msg = "Connecting to the background player..."
        self._ytdl_fallback_clients = load_ytdl_fallback_clients(config_file)

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._supervisor: Optional[asyncio.Task[None]] = None
        self._queue_parts: Dict[int, Dict[int, List[Any]]] = {}
        self._queue_seen = False
        self._started_idx: Optional[int] = None  # the track whose start the screen has been told about
        self.last_error: Tuple[str, str] = ("", "")  # (code, message) of the latest error frame
        self._left_playing: Optional[bool] = None  # set by close(): was audio playing as this client left

    # ── Hooks the screen overrides ───────────────────────────────────────────

    def _notify(self) -> None:
        """Something the screen shows has changed."""

    def _on_track_started(self, track: Track) -> None:
        """The daemon moved to another track."""

    def _after_poll(self) -> None:
        """New state has just been applied."""

    def _on_queue_loaded(self, index: int) -> None:
        """The first queue arrived; the current track is at ``index``."""

    def _on_starting_daemon(self) -> None:
        """No background player is running, so this client is about to start one."""

    def _selection_anchor(self) -> Any:
        """Whatever the screen needs to keep its selection when the queue is replaced."""
        return None

    def _restore_selection(self, anchor: Any) -> None:
        """Put the selection back after the queue was replaced."""

    # ── Connecting ───────────────────────────────────────────────────────────

    async def attach(self) -> None:
        """Connect to the background player, starting it first if none is running, and load its state."""
        await self._connect()
        if self._requested_url and not self.started_daemon:
            # An already running daemon: load what was asked for, unless it is playing exactly that.
            if self._requested_url != self.playlist_url or self._force_refresh:
                self.load_url(self._requested_url, auto_play=self.auto_play, force=self._force_refresh)
        self._supervisor = asyncio.create_task(self._supervise())

    async def _open(self) -> Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        try:
            return await asyncio.open_unix_connection(daemon.socket_path(), limit=MAX_SERVER_FRAME + 1024)
        except OSError:
            return None

    def _spawn_daemon(self) -> bool:
        extra: List[str] = []
        if not self.auto_play:
            extra.append("--no-auto-play")
        if self._force_refresh:
            extra.append("--force-cache")
        return daemon.spawn(self._requested_url, self.config_file, extra_args=extra)

    async def _connect(self) -> None:
        connection = await self._open()
        if connection is None:
            await asyncio.to_thread(daemon.is_running)  # removes a stale socket left by a crashed daemon
            self.started_daemon = True
            self._on_starting_daemon()
            if not await asyncio.to_thread(self._spawn_daemon):
                raise DaemonUnavailable("could not start the background player")
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while connection is None:
                if time.monotonic() > deadline:
                    raise DaemonUnavailable("the background player did not start in time")
                await asyncio.sleep(0.05)
                connection = await self._open()
        reader, writer = connection
        try:
            await self._handshake(reader, writer)
        except BaseException:
            writer.close()
            raise
        self._reader, self._writer = reader, writer
        self._connected = True

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(encode({"t": "hello", "version": PROTOCOL_VERSION}))
        await writer.drain()

        async def frame() -> Dict[str, Any]:
            line = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT_SECONDS)
            if not line:
                raise DaemonUnavailable("the background player closed the connection")
            return decode(line, MAX_SERVER_FRAME)

        try:
            first = await frame()
            if first["t"] == "error" and first.get("code") == "version":
                raise VersionMismatch(
                    "the background player is an older or newer version of MPF; run `mpf.py quit` and start MPF again"
                )
            if first["t"] != "welcome":
                raise DaemonUnavailable("the background player did not greet this client")
            self._queue_parts.clear()
            self._queue_seen = False
            while True:  # the snapshot, then the whole queue, so the screen starts with a full picture
                message = await frame()
                self._apply(message)
                if message["t"] == "queue" and message["chunk"] == message["chunks"] - 1:
                    return
        except (asyncio.TimeoutError, ProtocolError, KeyError, TypeError) as err:
            raise DaemonUnavailable(f"the background player did not answer properly: {err}") from err

    # ── Frames ───────────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        """Apply frames until the connection drops, then try to reconnect, starting the daemon if needed."""
        while self._running:
            await self._read_frames()
            if not self._running:
                return
            self._connected = False
            self._status_msg = "Lost connection to the background player; reconnecting..."
            self._notify()
            for attempt in range(RECONNECT_ATTEMPTS):
                try:
                    await self._connect()
                    self._notify()
                    break
                except DaemonUnavailable as err:
                    logger.warning("Reconnect attempt %d failed: %s", attempt + 1, err)
                    await asyncio.sleep(0.5)
            else:
                self._status_msg = "The background player is gone. Press q to quit."
                self._notify()
                return

    async def _read_frames(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                self._apply(decode(line, MAX_SERVER_FRAME))
        except (ConnectionError, ProtocolError, ValueError, asyncio.LimitOverrunError):
            return

    def _apply(self, message: Dict[str, Any]) -> None:
        kind = message.get("t")
        if kind in ("state", "snapshot"):
            self._apply_state(message)
        elif kind == "queue":
            self._apply_queue(message)
        elif kind == "error":
            self.last_error = (str(message.get("code", "")), clean_text(message.get("message"), 200))
            if self.last_error[1]:
                self._status_msg = self.last_error[1]
            self._notify()

    def _apply_state(self, fields: Dict[str, Any]) -> None:
        try:
            if "playlist_url" in fields:
                self.playlist_url = str(fields["playlist_url"])
            if "repeat" in fields:
                self.queue.repeat_mode = RepeatMode(fields["repeat"])
            if "shuffle" in fields:
                self.queue.shuffle_enabled = bool(fields["shuffle"])
            if "paused" in fields:
                self._is_paused = bool(fields["paused"])
            if "muted" in fields:
                self._is_muted = bool(fields["muted"])
            if "buffering" in fields:
                self._is_buffering = bool(fields["buffering"])
            if "idle" in fields:
                self._mpv_idle = bool(fields["idle"])
            if "loading" in fields:
                self._is_loading_playlist = bool(fields["loading"])
            if "volume" in fields:
                self._volume = int(fields["volume"])
            if "position" in fields:
                self._time_pos = float(fields["position"])
            if "duration" in fields:
                self._duration = float(fields["duration"])
            if "status" in fields:
                self._status_msg = clean_text(fields["status"], 300)
            if "current_idx" in fields:
                self.queue.current_idx = int(fields["current_idx"])
        except (TypeError, ValueError):
            logger.warning("Ignoring a malformed state frame")
            return
        track = self.queue.current_track
        if track and self.queue.current_idx != self._started_idx:
            self._started_idx = self.queue.current_idx
            self._on_track_started(track)
        self._after_poll()
        self._notify()

    def _apply_queue(self, message: Dict[str, Any]) -> None:
        try:
            revision, chunk, chunks = int(message["revision"]), int(message["chunk"]), int(message["chunks"])
            parts = self._queue_parts.setdefault(revision, {})
            parts[chunk] = message["tracks"]
            if len(parts) < chunks:
                return
            rows = [row for index in range(chunks) for row in parts[index]]
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring a malformed queue frame")
            return
        self._queue_parts.clear()
        tracks: List[Track] = []
        for row in rows:
            try:
                tracks.append(Track.from_cache_tuple(tuple(row)))
            except (TypeError, ValueError, IndexError):
                continue
        anchor = self._selection_anchor()
        self.queue.replace_tracks(tracks)
        if not self._queue_seen:
            self._queue_seen = True
            self._on_queue_loaded(self.queue.current_idx)
        else:
            self._restore_selection(anchor)
        self._notify()

    # ── Commands ─────────────────────────────────────────────────────────────

    def _send(self, name: str, **args: Any) -> bool:
        writer = self._writer
        if not self._connected or writer is None or writer.is_closing():
            return False
        try:
            writer.write(encode({"t": "cmd", "name": name, **args}))
        except (ProtocolError, OSError):
            return False
        return True

    def _play_index(self, index: int) -> None:
        self._send("play_index", index=index)

    def next_track(self) -> None:
        self._send("next")

    def previous_track(self) -> None:
        self._send("prev")

    def set_paused(self, paused: bool) -> None:
        if self._send("set_paused", paused=paused):
            self._is_paused = paused  # shown at once; the daemon confirms with its own state
            self._notify()

    def seek_relative(self, seconds: float) -> None:
        self._send("seek_rel", seconds=seconds)

    def seek_absolute(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self._send("seek_abs", seconds=seconds):
            self._time_pos = seconds
            self._notify()

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        if self._send("set_volume", volume=volume):
            self._volume = volume
            self._notify()

    def set_mute(self, muted: bool) -> None:
        if self._send("set_mute", muted=muted):
            self._is_muted = muted
            self._notify()

    def set_shuffle(self, enabled: bool) -> None:
        self._send("set_shuffle", enabled=enabled)  # the new order arrives as a queue frame

    def _cycle_repeat(self) -> None:
        if self._send("cycle_repeat"):
            self.queue.repeat_mode = self.queue.repeat_mode.next_mode()
            self._notify()

    def load_url(self, url: str, auto_play: Optional[bool] = None, force: bool = False) -> None:
        args: Dict[str, Any] = {"url": url, "force": force}
        if auto_play is not None:
            args["auto_play"] = auto_play
        self._send("load_url", **args)

    def set_playback_config(
        self, default_playlist: Optional[str] = None, fallback_clients: Optional[List[str]] = None
    ) -> None:
        args: Dict[str, Any] = {}
        if default_playlist is not None:
            args["default_playlist"] = default_playlist
        if fallback_clients is not None:
            args["fallback_clients"] = fallback_clients
        self._send("set_playback_config", **args)

    # ── Leaving ──────────────────────────────────────────────────────────────

    @property
    def music_continues(self) -> bool:
        """Whether leaving this client leaves audio playing in the background."""
        if self._left_playing is not None:
            return self._left_playing
        return self._connected and self.queue.current_track is not None and not self._is_paused

    def close(self) -> None:
        """Disconnect. The daemon, and the music, carry on."""
        if self._left_playing is None:
            self._left_playing = self.music_continues
        self._running = False
        self._connected = False
        if self._supervisor:
            self._supervisor.cancel()
            self._supervisor = None
        if self._writer:
            self._writer.close()
            self._writer = None
