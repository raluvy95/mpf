"""Playback engine: mpv, the queue, playlist loading, resume state and MPRIS, with no user interface.

The TUI client draws what this engine holds and calls its controls. It deliberately imports neither numpy
nor blessed, so it stays light enough to run as the resident background player.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

from python_mpv_jsonipc import MPV

from mpf_core import daemon
from mpf_core.buffering import AdaptiveReadahead
from mpf_core.cache import (
    PlaybackState,
    is_playlist_cache_stale,
    load_cached_playlist,
    load_playback_state,
    save_cached_playlist,
    save_playback_state,
)
from mpf_core.config import load_default_playlist, load_ytdl_fallback_clients, save_default_playlist
from mpf_core.fetcher import InvalidMediaURL, PlaylistFetchError, fetch_playlist, validate_media_url
from mpf_core.metadata import clean_text, redact_urls
from mpf_core.models import RepeatMode, Track, TrackQueue
from mpf_core.mpris import MprisService, build_metadata
from mpf_core.paths import CONFIG_FILE
from mpf_core.previewer import thumbnail_path
from mpf_core.server import PlayerServer

DEFAULT_PLAYLIST_URL = ""
HANDOFF_TIMEOUT_SECONDS = 10.0  # how long the TUI waits for the background player to take over mpv
PREFETCH_LEAD_SECONDS = 20.0  # queue the next track in mpv this close to the end of the current one

logger = logging.getLogger(__name__)


def _private_ipc_socket() -> Tuple[str, Optional[str]]:
    """A socket path only this user can reach, plus a directory to delete afterwards (or None).

    python-mpv-jsonipc defaults to a guessable name in /tmp, and mpv's IPC can run programs, so it
    must not be reachable by other users. $XDG_RUNTIME_DIR is 0700 and cleaned at logout; without a
    usable one, a fresh 0700 temporary directory is used.
    """
    name = f"mpf-mpv-{os.getpid()}-{secrets.token_hex(8)}.sock"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    try:
        info = os.stat(runtime_dir)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
            return os.path.join(runtime_dir, name), None
    except OSError:
        pass
    directory = tempfile.mkdtemp(prefix="mpf-")  # created 0700
    return os.path.join(directory, name), directory


@contextmanager
def _mpv_environment() -> Iterator[None]:
    """Let system mpv use host libraries instead of PyInstaller's copies."""
    if not getattr(sys, "frozen", False):
        yield
        return

    bundled_library_path = os.environ.get("LD_LIBRARY_PATH")
    host_library_path = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if host_library_path is None:
        os.environ.pop("LD_LIBRARY_PATH", None)
    else:
        os.environ["LD_LIBRARY_PATH"] = host_library_path

    try:
        yield
    finally:
        if bundled_library_path is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = bundled_library_path


class PlaybackEngine:
    """Owns mpv and the queue; a client reads its state and calls its controls."""

    def __init__(
        self,
        playlist_url: str = DEFAULT_PLAYLIST_URL,
        auto_play: bool = True,
        config_file: str = CONFIG_FILE,
        headless: bool = False,
        adopt_ipc: Optional[str] = None,
        force_refresh: bool = False,
    ) -> None:
        self.on_change: Optional[Callable[[], None]] = None  # called after any change a client may show
        self._force_refresh = force_refresh  # fetch the first playlist again however fresh its cache is
        self.headless = headless  # background player: no terminal, visualizer or thumbnails
        self.detach_on_exit = False  # set by run() when the caller should keep playing in the background
        self.handed_off = False  # the background player took over this player's mpv, which is still playing
        self._adopt_ipc = adopt_ipc  # IPC socket of an mpv that is already playing and should be taken over
        self._adopting = False  # connected to that mpv and not yet resumed from the saved state
        self._handing_over = False  # a returning TUI asked this background player to give up its mpv
        self._ipc_socket: Optional[str] = None
        self._server: Optional[PlayerServer] = None  # the background player's socket, when headless
        self.config_file = config_file
        self.playlist_url = playlist_url or load_default_playlist(config_file)
        self.auto_play = auto_play
        self.queue = TrackQueue()
        self.mpv: Optional[MPV] = None
        self.mpris = MprisService(self)

        self._running = True
        self._is_paused = False
        self._is_muted = False
        self._is_buffering = False
        self._mpv_idle = True  # no file loaded or loading in mpv
        self._is_loading_playlist = False
        self._volume = 100
        self._status_msg = "Initializing..."
        self._time_pos = 0.0
        self._duration = 0.0
        self._last_state_save = 0.0
        self._state_save: Optional[asyncio.Future[bool]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._playlist_task: Optional[asyncio.Task[None]] = None
        self._failed_track_ids: set[str] = set()
        self._readahead = AdaptiveReadahead()
        self._was_stalled = False
        self._prefetched_id: Optional[str] = None  # next track already appended to mpv's playlist
        self._mpv_poll_error_reported = False
        self._ipc_dir: Optional[str] = None  # temporary directory holding mpv's IPC socket, if any
        # yt-dlp chooses the YouTube client. When mpv fails a track it is retried first with yt-dlp's own
        # choice again (None, a fresh URL), then with each fallback client in order, then skipped.
        self._ytdl_fallback_clients = load_ytdl_fallback_clients(config_file)
        self._ytdl_plain_retries = 1
        self._fallbacks_left: List[Optional[str]] = []
        self._ytdl_client: Optional[str] = None  # client forced for the current attempt, if any
        self._mpv_errors: Deque[str] = deque(maxlen=6)  # mpv's own error lines for the current track
        self._pending_seek: Optional[Tuple[str, float]] = None
        self._power_inhibitor: Optional[subprocess.Popen[bytes]] = None

    # ── Read-only state for MPRIS and clients ───────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def position(self) -> float:
        return self._time_pos

    @property
    def volume(self) -> int:
        return self._volume

    def state(self) -> Dict[str, Any]:
        """Everything a client needs to draw the player, as plain values (the queue is sent separately)."""
        return {
            "playlist_url": self.playlist_url,
            "current_idx": self.queue.current_idx,
            "paused": self._is_paused,
            "muted": self._is_muted,
            "buffering": self._is_buffering,
            "idle": self._mpv_idle,
            "loading": self._is_loading_playlist,
            "volume": self._volume,
            "position": round(self._time_pos, 2),
            "duration": round(self._duration, 2),
            "repeat": self.queue.repeat_mode.value,
            "shuffle": self.queue.shuffle_enabled,
            "status": self._status_msg,
            "queue_revision": self.queue.revision,
        }

    def load_url(self, url: str, auto_play: Optional[bool] = None, force: bool = False) -> None:
        """Load a validated playlist or video URL in the background, replacing any load in progress."""
        if auto_play is not None:
            self.auto_play = auto_play
        if self._playlist_task and not self._playlist_task.done():
            self._playlist_task.cancel()
        self._playlist_task = asyncio.create_task(self.load_playlist(url, force=force))

    # ── Hooks a client overrides; the engine itself has no screen ───────────

    def _notify(self) -> None:
        """Something a client shows has changed."""
        if self.on_change:
            self.on_change()

    def _on_track_started(self, track: Track) -> None:
        """A track was just started."""

    def _after_poll(self) -> None:
        """mpv's state was just read."""

    def _on_queue_loaded(self, index: int) -> None:
        """A playlist was loaded and will start at ``index``."""

    def _selection_anchor(self) -> Any:
        """Whatever a client needs to keep its selection across a playlist re-sync."""
        return None

    def _restore_selection(self, anchor: Any) -> None:
        """Put the selection back after a re-sync, from ``_selection_anchor``'s value."""

    def _art_path(self, track: Track) -> str:
        return thumbnail_path(track)

    def set_mute(self, muted: bool) -> None:
        self._is_muted = muted
        if self.mpv:
            try:
                self.mpv.mute = muted
            except Exception as err:
                self._status_msg = f"Mute failed: {err}"
                logger.warning("MPV mute failed: %s", err)
        self._notify()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def _start_engine(self) -> bool:
        """Start mpv and MPRIS and begin loading the playlist; False if this daemon must not run."""
        self._loop = asyncio.get_running_loop()
        if self.headless:
            server = PlayerServer(self)
            try:
                await server.start()
            except daemon.DaemonAlreadyRunning as err:
                logger.error("%s", err)
                return False
            self._server = server
            try:
                self._loop.add_signal_handler(signal.SIGTERM, self._request_quit)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            if self._adopt_ipc:
                self._adopt_running_mpv(self._adopt_ipc)
            if self.mpv is None:
                self._start_own_mpv()
            if self.mpv:
                self.mpv.bind_event("end-file", self._on_mpv_end_file)
                self.mpv.bind_event("file-loaded", self._on_mpv_file_loaded)
        except Exception as err:
            self._status_msg = f"MPV startup failed; verify mpv is installed: {err}"
            logger.exception("MPV startup failed")

        await self.mpris.start()
        if self.playlist_url:
            self._playlist_task = asyncio.create_task(
                self.load_playlist(self.playlist_url, force=self._force_refresh)
            )
        else:
            self._status_msg = "Load a YouTube playlist or video with [o]URL."
        return True

    async def run_headless(self) -> None:
        """Run as the background player: serve clients and follow mpv until told to quit."""
        if not await self._start_engine():
            return
        try:
            await self._mpv_poll_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if self._playlist_task and not self._playlist_task.done():
                self._playlist_task.cancel()
            await self._stop_engine()

    async def _stop_engine(self) -> None:
        """Save state and release mpv, MPRIS and the daemon socket; run after the poll loop has stopped."""
        if self._playlist_task:
            await asyncio.gather(self._playlist_task, return_exceptions=True)
        if self._state_save:
            # Let an in-flight periodic save finish so it cannot overwrite the final one.
            await asyncio.gather(self._state_save, return_exceptions=True)
        self._save_current_state()
        self.mpris.stop()
        self._set_power_inhibit(False)
        if self.detach_on_exit and self.mpv:
            # Start the background player while mpv is still playing here, so there is no gap.
            self.handed_off = await asyncio.to_thread(self._hand_off_to_daemon)
        keep_mpv = self.handed_off or self._handing_over
        if self.mpv:
            try:
                if keep_mpv:
                    self.mpv.mpv_process = None  # disconnect only: mpv keeps playing for the next owner
                elif self.headless and self._adopt_ipc and self._ipc_socket == self._adopt_ipc:
                    self.mpv.command("quit")  # we did not start this mpv, so terminate() would leave it
                self.mpv.terminate()
            except Exception:
                logger.exception("MPV shutdown failed")
            finally:
                self.mpv = None
        if self._ipc_dir and not keep_mpv:
            shutil.rmtree(self._ipc_dir, ignore_errors=True)
        if self._server:
            self._server.close()
            self._server = None

    # ── Playback ────────────────────────────────────────────────────────────

    def _mpv_options(self) -> Dict[str, Any]:
        return {
            "log_handler": self._on_mpv_log,
            "loglevel": "error",
        }

    def _start_own_mpv(self) -> None:
        ipc_socket, self._ipc_dir = _private_ipc_socket()
        self._ipc_socket = ipc_socket
        with _mpv_environment():
            self.mpv = MPV(
                ipc_socket=ipc_socket,
                video=False,
                ytdl=True,
                ytdl_format="bestaudio/best",
                cache="yes",
                demuxer_max_bytes="25M",
                demuxer_readahead_secs=str(self._readahead.secs),
                prefetch_playlist="yes",
                stop_screensaver="yes",
                **self._mpv_options(),
            )

    def _adopt_running_mpv(self, socket_path: str) -> None:
        """Connect to the mpv the TUI left playing. On any problem, fall back to starting a fresh one."""
        try:
            info = os.stat(socket_path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError(f"{socket_path} is not a socket owned by this user")
            mpv = MPV(start_mpv=False, ipc_socket=socket_path, **self._mpv_options())
            mpv.command("playlist-clear")  # drops a prefetched next track; the playing one stays
        except Exception:
            logger.exception("Unable to take over the running mpv; starting a new one")
            return
        self.mpv = mpv
        self._ipc_socket = socket_path
        # The socket sits in a private mpf-* directory when there was no $XDG_RUNTIME_DIR; it is ours to remove.
        parent = os.path.dirname(socket_path)
        self._ipc_dir = parent if os.path.basename(parent).startswith("mpf-") else None
        self._adopting = True

    def _hand_off_to_daemon(self) -> bool:
        """Start the background player on this player's mpv and wait until it answers, so playback never stops."""
        if not self._ipc_socket or not daemon.spawn(self.playlist_url, self.config_file, adopt_ipc=self._ipc_socket):
            return False
        deadline = time.monotonic() + HANDOFF_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if daemon.is_running():
                return True
            time.sleep(0.1)
        with contextlib.suppress(Exception):
            daemon.stop(timeout=2.0)  # it may still come up late; it must not play after we stop the music
        return False

    def _resume_index(self, index: int, start_pos: float) -> None:
        """Start the saved track, or, when an already playing mpv was adopted, carry on with it untouched."""
        adopted, self._adopting = self._adopting, False
        track = self.queue.tracks[index] if 0 <= index < len(self.queue.tracks) else None
        if adopted and track and self.mpv:
            try:
                adopted = self.mpv.path == track.url
            except Exception:
                adopted = False
        if adopted:
            self._play_index(index, loaded=True)  # reloading it would be the audible gap this avoids
            try:
                self._is_paused = bool(self.mpv.pause) if self.mpv else False  # it may have been paused meanwhile
            except Exception:
                self._is_paused = False
        else:
            self._play_index(index, start_pos=start_pos)

    def _should_detach(self) -> bool:
        """Whether leaving the TUI should keep the music playing in a background process.

        Only audio that is playing is kept: a paused player, or one with nothing loaded, just exits.
        """
        return (
            not self.headless
            and self.mpv is not None
            and self.queue.current_track is not None
            and not self._is_paused
        )

    def _request_quit(self) -> None:
        self._running = False

    def _request_handover(self) -> Optional[str]:
        """A returning TUI wants this player's mpv: stop controlling it, leave it playing, and exit."""
        if not self.mpv or not self._ipc_socket:
            return None
        self._handing_over = True
        self._running = False
        return self._ipc_socket

    def _snapshot_playback_state(self) -> Optional[PlaybackState]:
        """Capture the current playback position, track, and settings."""
        curr = self.queue.current_track
        if not curr:
            return None
        return PlaybackState(
            playlist_url=self.playlist_url,
            track_id=curr.id,
            track_index=self.queue.current_idx,
            time_pos=self._time_pos,
            duration=self._duration,
            repeat_mode=self.queue.repeat_mode.value,
            volume=self._volume,
            updated_at=time.time(),
        )

    def _save_current_state(self) -> None:
        """Persist current playback state to the playback cache."""
        state = self._snapshot_playback_state()
        if state:
            save_playback_state(state)

    async def load_playlist(self, url: str, force: bool = False) -> None:
        """Load playlist from cache first, then fetch updates via yt_dlp (once per week).

        ``force`` fetches it again however fresh the cache is, replacing the cached copy on success.
        """
        loop = asyncio.get_running_loop()
        try:
            url = validate_media_url(url)
        except InvalidMediaURL as err:
            self._status_msg = str(err)
            return
        self.playlist_url = url

        # 1. Check local JSON cache for instant load
        cached = load_cached_playlist(url)
        saved_state = load_playback_state(url)

        if cached:
            self.queue.set_tracks(cached)
            self._status_msg = f"Loaded {len(cached)} tracks (cache)."
            if not save_default_playlist(url, self.config_file):
                logger.warning("Unable to persist loaded playlist %s", url)

            target_idx = 0
            start_pos = 0.0

            if saved_state:
                # Find track by ID or fallback to index
                matched_indices = [i for i, t in enumerate(cached) if t.id == saved_state.track_id]
                target_idx = matched_indices[0] if matched_indices else min(saved_state.track_index, len(cached) - 1)
                start_pos = saved_state.time_pos
                self._volume = saved_state.volume
                try:
                    self.queue.repeat_mode = RepeatMode(saved_state.repeat_mode)
                except ValueError:
                    logger.warning("Ignoring invalid saved repeat mode %r", saved_state.repeat_mode)

            self._on_queue_loaded(target_idx)
            self.queue.current_idx = target_idx
            if self.auto_play:
                self._resume_index(target_idx, start_pos)

        # 2. Fetch fresh tracks only when cache is missing or older than 7 days
        if not force and not is_playlist_cache_stale(url):
            # Cache is fresh — nothing to do
            if cached:
                return
            # No cache yet but stale check failed (shouldn't happen, but guard)
        self._is_loading_playlist = True
        self._status_msg = "Fetching playlist..." if not cached else "Syncing playlist..."
        try:
            playlist = await loop.run_in_executor(None, fetch_playlist, url)
            tracks = playlist.tracks
            if not tracks:
                if not cached:
                    self._status_msg = "No tracks found."
                return

            active_track = self.queue.current_track
            active_index = self.queue.current_idx
            selection = self._selection_anchor()

            cache_saved = save_cached_playlist(url, tracks, title=playlist.title)
            if not save_default_playlist(url, self.config_file):
                logger.warning("Unable to persist loaded playlist %s", url)
            runtime_tracks = list(tracks)
            if active_track and all(track.id != active_track.id for track in runtime_tracks):
                runtime_tracks.insert(min(active_index, len(runtime_tracks)), active_track)
            self.queue.set_tracks(runtime_tracks, reset_index=not bool(cached))
            self._restore_selection(selection)
            self._status_msg = (
                f"Loaded {len(tracks)} tracks."
                if cache_saved
                else f"Loaded {len(tracks)} tracks; cache write failed."
            )

            if not cached:
                target_idx = 0
                start_pos = 0.0
                if saved_state:
                    matched_indices = [i for i, t in enumerate(tracks) if t.id == saved_state.track_id]
                    target_idx = matched_indices[0] if matched_indices else min(saved_state.track_index, len(tracks) - 1)
                    start_pos = saved_state.time_pos
                    self._volume = saved_state.volume
                    try:
                        self.queue.repeat_mode = RepeatMode(saved_state.repeat_mode)
                    except ValueError:
                        logger.warning("Ignoring invalid saved repeat mode %r", saved_state.repeat_mode)
                self._on_queue_loaded(target_idx)
                self.queue.current_idx = target_idx
                if self.auto_play:
                    self._resume_index(target_idx, start_pos)
        except PlaylistFetchError as err:
            self._status_msg = f"Offline; using cached playlist. {err}" if cached else f"Fetch Error: {err}"
            logger.warning("Playlist refresh failed for %s: %s", url, err)
        except Exception as err:
            self._status_msg = f"Playlist error: {err}"
            logger.exception("Unexpected playlist failure for %s", url)
        finally:
            self._is_loading_playlist = False

    def _on_mpv_log(self, level: str, prefix: str, text: str) -> None:
        """Keep mpv's real error lines (e.g. "HTTP error 403 Forbidden") for the skip message and log."""
        self._mpv_errors.append(clean_text(redact_urls(f"{prefix}: {text}"), 300))

    def _set_ytdl_client(self, client: Optional[str]) -> None:
        """Force a YouTube client for the next load, or None to let yt-dlp choose."""
        if client == self._ytdl_client or not self.mpv:
            return
        self.mpv.ytdl_raw_options = f"extractor-args=youtube:player_client={client}" if client else ""
        self._ytdl_client = client

    def _retry_with_fallback_client(self, track: Any) -> bool:
        """Reload the failed track with the next fallback client. False once none are left."""
        if not self.mpv or not self._fallbacks_left:
            return False
        client = self._fallbacks_left.pop(0)
        label = f"the {client} client" if client else "yt-dlp's default client"
        logger.warning("Retrying %s with %s after: %s", track.id, label, self._mpv_errors_text())
        self._mpv_errors.clear()  # the next attempt reports its own errors
        self._prefetched_id = None  # mpv.play() below replaces the playlist
        try:
            self._set_ytdl_client(client)
            self.mpv.play(track.url)
        except Exception:
            logger.exception("Unable to retry track %s with client %s", track.id, client)
            return False
        self._status_msg = f"Retrying {track.title} with {label}..."
        self._is_buffering = True
        return True

    def _mpv_errors_text(self) -> str:
        return " | ".join(self._mpv_errors) or "no error from mpv"

    def _play_index(self, index: int, start_pos: float = 0.0, loaded: bool = False) -> None:
        if not self.queue.tracks or not (0 <= index < len(self.queue.tracks)):
            return
        self.queue.current_idx = index
        track = self.queue.tracks[index]
        self._is_paused = False
        self._is_buffering = True
        self._mpv_idle = False
        self._prefetched_id = None  # mpv.play() replaces its playlist; a loaded track already consumed it

        if self.mpv:
            try:
                self._pending_seek = (track.id, start_pos) if start_pos > 1.0 else None
                self._fallbacks_left = [None] * self._ytdl_plain_retries + list(self._ytdl_fallback_clients)
                self._mpv_errors.clear()
                self._set_ytdl_client(None)
                if not loaded:  # mpv already moved on to the prefetched track by itself
                    self.mpv.play(track.url)
                self._set_power_inhibit(True)
            except Exception as err:
                self._pending_seek = None
                self._status_msg = f"Play Error: {err}"
                self._is_buffering = False
                self._set_power_inhibit(False)
                logger.exception("Unable to play track %s", track.id)
        else:
            self._is_buffering = False
            self._status_msg = "MPV is unavailable; install mpv and restart MPF."

        self._on_track_started(track)

    def set_paused(self, paused: bool) -> None:
        if not self.mpv:
            return
        try:
            self.mpv.pause = paused
            self._is_paused = paused
            self._set_power_inhibit(not paused)
        except Exception as err:
            self._status_msg = f"Pause failed: {err}"
            logger.warning("MPV pause failed: %s", err)
        self._notify()

    def next_track(self) -> None:
        nxt = self.queue.get_next_index(natural_end=False)
        if nxt is not None:
            self._play_index(nxt)
            self._notify()

    def previous_track(self) -> None:
        prev = self.queue.get_prev_index()
        if prev is not None:
            self._play_index(prev)
            self._notify()

    def seek_relative(self, seconds: float) -> None:
        self._seek("seek", seconds, "relative")

    def seek_absolute(self, seconds: float) -> None:
        self._seek("seek", max(0.0, seconds), "absolute")

    def _seek(self, *command: Any) -> None:
        if not self.mpv:
            return
        try:
            self.mpv.command(*command)
        except Exception as err:
            self._status_msg = f"Seek failed: {err}"
            logger.warning("MPV seek failed: %s", err)

    def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, volume))
        if self.mpv:
            try:
                self.mpv.volume = self._volume
            except Exception as err:
                self._status_msg = f"Volume change failed: {err}"
                logger.warning("MPV volume change failed: %s", err)
        self._notify()

    def set_loop_status(self, status: str) -> None:
        mode = {"None": RepeatMode.OFF, "Playlist": RepeatMode.ALL, "Track": RepeatMode.ONE}.get(status)
        if mode:
            self.queue.repeat_mode = mode
            self._notify()


    def mpris_playback_status(self) -> str:
        if not self.queue.current_track or self._mpv_idle and not self._is_buffering:
            return "Stopped"
        return "Paused" if self._is_paused else "Playing"

    def mpris_loop_status(self) -> str:
        return {RepeatMode.OFF: "None", RepeatMode.ALL: "Playlist", RepeatMode.ONE: "Track"}[
            self.queue.repeat_mode
        ]

    def mpris_art_path(self) -> str:
        """Local thumbnail of the current track, or "" until it has downloaded."""
        track = self.queue.current_track
        if not track:
            return ""
        path = self._art_path(track)
        return path if os.path.exists(path) and os.path.getsize(path) > 0 else ""

    def mpris_metadata(self) -> Dict[str, Any]:
        return build_metadata(self.queue.current_track, self._duration, self.mpris_art_path())

    def set_shuffle(self, enabled: bool) -> None:
        self.queue.set_shuffle(enabled)
        self._notify()

    def _cycle_repeat(self) -> None:
        self.queue.repeat_mode = self.queue.repeat_mode.next_mode()

    def _set_power_inhibit(self, active: bool) -> None:
        if active:
            if self._power_inhibitor is not None:
                return
            try:
                self._power_inhibitor = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=sleep",
                        "--who=MPF",
                        "--why=Music playback",
                        "--mode=block",
                        "cat",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as err:
                logger.warning("Unable to inhibit system sleep: %s", err)
            return

        process = self._power_inhibitor
        self._power_inhibitor = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
        except OSError as err:
            logger.debug("Unable to stop system sleep inhibitor: %s", err)

    def _on_mpv_end_file(self, event: Dict[str, Any]) -> None:
        """Marshal MPV's IPC callback onto the asyncio/UI thread."""
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(self._handle_mpv_end_file, dict(event))

    def _on_mpv_file_loaded(self, event: Dict[str, Any]) -> None:
        """Seek only after MPV has made the newly loaded stream seekable."""
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(self._handle_mpv_file_loaded, dict(event))

    def _handle_mpv_file_loaded(self, _event: Dict[str, Any]) -> None:
        pending_seek = self._pending_seek
        self._pending_seek = None
        current = self.queue.current_track
        if not pending_seek or not current or pending_seek[0] != current.id or not self.mpv:
            return
        try:
            self.mpv.command("seek", pending_seek[1], "absolute")
            self._time_pos = pending_seek[1]
        except Exception as err:
            self._status_msg = f"Resume seek failed: {err}; playing from start."
            logger.warning("Unable to resume track %s: %s", current.id, err)

    def _handle_mpv_end_file(self, event: Dict[str, Any]) -> None:
        reason = event.get("reason")
        if reason not in {"eof", "error"}:
            return
        current = self.queue.current_track
        if reason == "error" and current:
            if self._retry_with_fallback_client(current):
                return
            self._failed_track_ids.add(current.id)
            error = clean_text(event.get("file_error") or event.get("error"), 200) or "media unavailable"
            detail = self._mpv_errors[-1] if self._mpv_errors else ""
            self._status_msg = f"Skipped {current.title}: {error}" + (f" ({detail})" if detail else "")
            logger.warning("Playback failed for %s: %s [%s]", current.id, error, self._mpv_errors_text())

        nxt = self.queue.get_next_index(natural_end=reason == "eof")
        if reason == "error":
            checked = 0
            while nxt is not None and self.queue.tracks[nxt].id in self._failed_track_ids:
                checked += 1
                if checked >= len(self.queue.tracks):
                    nxt = None
                    break
                nxt = (nxt + 1) % len(self.queue.tracks)
        if nxt is not None:
            # mpv advances to a prefetched track on its own at EOF; reloading it would throw the buffer away.
            prefetched = reason == "eof" and self.queue.tracks[nxt].id == self._prefetched_id
            self._play_index(nxt, loaded=prefetched)
        else:
            self._is_buffering = False
            self._is_paused = True
            self._set_power_inhibit(False)
            if reason == "eof":
                self._status_msg = "Playlist finished."

    def _tune_buffering(self, stalled: bool) -> None:
        """Raise mpv's read-ahead after a mid-track stall, lower it again after a long stall-free stretch."""
        if not self.mpv:
            return
        now = time.monotonic()
        changed = (stalled and not self._was_stalled and self._readahead.on_stall(now)) or (
            not stalled and not self._is_paused and self._readahead.tick(now)
        )
        self._was_stalled = stalled
        if changed:
            logger.info("Read-ahead is now %ss", self._readahead.secs)
            try:
                self.mpv.demuxer_readahead_secs = self._readahead.secs
            except Exception:
                logger.exception("Unable to change mpv's read-ahead")

    def _prefetch_target(self) -> Optional[Any]:
        """The track that will play after the current one ends by itself, if it is worth loading early."""
        current = self.queue.current_track
        if not current or self._duration <= 0 or self._duration - self._time_pos > PREFETCH_LEAD_SECONDS:
            return None
        nxt = self.queue.get_next_index(natural_end=True)
        if nxt is None:
            return None
        track = self.queue.tracks[nxt]
        if track.id == current.id or track.id in self._failed_track_ids:
            return None  # repeat-one, a one-track loop, or a track that already failed
        return track

    def _sync_prefetch(self) -> None:
        """Keep mpv's playlist holding exactly the next track, only near the end of the current one.

        Checked on every poll, so a shuffle or repeat change, a re-sync or a jump drops a stale prefetch
        without each of those having to know about it.
        """
        if not self.mpv:
            return
        target = self._prefetch_target()
        wanted = target.id if target else None
        if wanted == self._prefetched_id:
            return
        try:
            if self._prefetched_id is not None:
                self.mpv.command("playlist-clear")  # keeps the playing entry, drops the rest
                self._prefetched_id = None
            if target:
                self.mpv.command("loadfile", target.url, "append")
                self._prefetched_id = target.id
        except Exception:
            self._prefetched_id = None
            logger.exception("Unable to update the prefetched track")

    async def _mpv_poll_loop(self) -> None:
        while self._running:
            if self.mpv and self.queue.current_track:
                try:
                    self._time_pos = getattr(self.mpv, "time_pos", 0.0) or 0.0
                    self._duration = (
                        getattr(self.mpv, "duration", 0.0)
                        or (self.queue.current_track.duration or 0.0)
                    )
                    # Check real MPV cache/stall state
                    is_caching = getattr(self.mpv, "paused_for_cache", False)
                    core_idle = getattr(self.mpv, "core_idle", False)
                    self._mpv_idle = bool(getattr(self.mpv, "idle_active", False))
                    self._is_buffering = not self._mpv_idle and bool(
                        is_caching or (core_idle and not self._is_paused and self._time_pos == 0.0)
                    )
                    if self._time_pos > 0.0 and self.queue.current_track:
                        self._failed_track_ids.discard(self.queue.current_track.id)
                    self._tune_buffering(bool(is_caching) and self._time_pos > 0.0)
                    self._sync_prefetch()

                    self._after_poll()

                    # Periodically save playback state every 3 seconds. The write locks and
                    # fsyncs, so it runs in an executor to keep the UI loop responsive.
                    now = time.time()
                    if now - self._last_state_save >= 3.0:
                        self._last_state_save = now
                        state = self._snapshot_playback_state()
                        if state:
                            self._state_save = asyncio.get_running_loop().run_in_executor(
                                None, save_playback_state, state
                            )
                            await asyncio.shield(self._state_save)
                    self._mpv_poll_error_reported = False
                except Exception as err:
                    if not self._mpv_poll_error_reported:
                        self._status_msg = f"MPV status error: {err}"
                        logger.warning("Unable to poll MPV status: %s", err)
                        self._mpv_poll_error_reported = True
            self.mpris.sync()
            await asyncio.sleep(0.2)
