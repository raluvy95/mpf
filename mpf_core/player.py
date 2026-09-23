"""Blessed TUI player driven by asyncio event loop."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from blessed import Terminal
from python_mpv_jsonipc import MPV

from mpf_core.cache import (
    PlaybackState,
    is_playlist_cache_stale,
    load_cached_playlist,
    load_playback_state,
    save_cached_playlist,
    save_playback_state,
)
from mpf_core.config import (
    VISUALIZER_STYLES,
    load_default_playlist,
    load_vim_mode,
    load_visualizer_preferences,
    save_default_playlist,
    save_vim_mode,
    save_visualizer_preferences,
)
from mpf_core.fetcher import (
    InvalidMediaURL,
    PlaylistFetchError,
    fetch_playlist_tracks,
    validate_media_url,
)
from mpf_core.models import RepeatMode, TrackQueue, format_time
from mpf_core.paths import CONFIG_FILE
from mpf_core.previewer import KittyPreviewer
from mpf_core.visualizer import PipeWireSpectrumAnalyzer

DEFAULT_PLAYLIST_URL = ""
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

logger = logging.getLogger(__name__)


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


class BlessedMusicPlayer:
    """Asyncio-driven TUI player using Blessed for true transparent terminal rendering."""

    SPECTRUM_CHARS = " .:-=+*#%@"
    SPECTRUM_STYLES = VISUALIZER_STYLES
    BRAILLE_LEFT = (0x1, 0x2, 0x4, 0x40)
    BRAILLE_RIGHT = (0x8, 0x10, 0x20, 0x80)
    VISUALIZER_RENDER_INTERVAL = 1 / 15
    IDLE_RENDER_INTERVAL = 0.1
    FOOTER_HINTS = [
        "? help", "space pause", "enter play", "↑/↓ browse", "/ search",
        "o open", "n/p track", "←/→ seek", "+/- volume", "q quit",
    ]

    def __init__(
        self,
        playlist_url: str = DEFAULT_PLAYLIST_URL,
        auto_play: bool = True,
        config_file: str = CONFIG_FILE,
        show_visualizer: Optional[bool] = None,
        show_preview: bool = True,
        vim_mode: Optional[bool] = None,
    ) -> None:
        self.term = Terminal()
        self.config_file = config_file
        self.playlist_url = playlist_url or load_default_playlist(config_file)
        self.auto_play = auto_play
        self.queue = TrackQueue()
        self.mpv: Optional[MPV] = None
        self.spectrum = PipeWireSpectrumAnalyzer(num_bands=32, target_node="mpv")
        self.previewer = KittyPreviewer()

        self._running = True
        self._is_paused = False
        self._is_muted = False
        self._is_buffering = False
        self._is_loading_playlist = False
        self._volume = 100
        self._spectrum_style, configured_visualizer = load_visualizer_preferences(config_file)
        self._show_visualizer = configured_visualizer if show_visualizer is None else show_visualizer
        self._show_preview = show_preview
        self._vim_mode = load_vim_mode(config_file) if vim_mode is None else vim_mode
        self._show_help = False
        self._status_msg = "Initializing..."

        # UI state
        self._selected_list_idx = 0
        self._scroll_offset = 0
        self._input_mode = "none"  # "search" | "url" | "none"
        self._input_buffer = ""
        self._time_pos = 0.0
        self._duration = 0.0
        self._last_state_save = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._playlist_task: Optional[asyncio.Task[None]] = None
        self._failed_track_ids: set[str] = set()
        self._last_render_signature: Optional[Tuple[Any, ...]] = None
        self._render_requested = asyncio.Event()
        self._mpv_poll_error_reported = False
        # Track list layout — updated each render so mouse clicks map to rows
        self._list_top_row = 0
        self._list_height = 0
        self._last_render_size = (0, 0)
        self._spectrum_history: List[List[float]] = []
        self._pending_seek: Optional[Tuple[str, float]] = None
        self._power_inhibitor: Optional[subprocess.Popen[bytes]] = None

    async def run(self) -> None:
        """Run the main async event loop with Blessed context managers."""
        self._loop = asyncio.get_running_loop()
        try:
            with _mpv_environment():
                self.mpv = MPV(
                    video=False,
                    ytdl=True,
                    ytdl_format="bestaudio/best",
                    cache="yes",
                    demuxer_max_bytes="25M",
                    demuxer_readahead_secs="30",
                    stop_screensaver="yes",
                )
            self.mpv.bind_event("end-file", self._on_mpv_end_file)
            self.mpv.bind_event("file-loaded", self._on_mpv_file_loaded)
        except Exception as err:
            self._status_msg = f"MPV startup failed; verify mpv is installed: {err}"
            logger.exception("MPV startup failed")

        self.spectrum.start()
        if self.playlist_url:
            self._playlist_task = asyncio.create_task(self.load_playlist(self.playlist_url))
        else:
            self._status_msg = "Load a YouTube playlist or video with [o]URL."

        # Enter blessed fullscreen mode with mouse click support
        with self.term.fullscreen(), self.term.cbreak(), self.term.hidden_cursor(), \
                self.term.mouse_enabled():
            render_task = asyncio.create_task(self._render_loop())
            input_task = asyncio.create_task(self._input_loop())
            mpv_task = asyncio.create_task(self._mpv_poll_loop())

            try:
                await asyncio.gather(render_task, input_task, mpv_task)
            except asyncio.CancelledError:
                pass
            finally:
                self._running = False
                if self._playlist_task and not self._playlist_task.done():
                    self._playlist_task.cancel()
                for task in (render_task, input_task, mpv_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(render_task, input_task, mpv_task, return_exceptions=True)
                if self._playlist_task:
                    await asyncio.gather(self._playlist_task, return_exceptions=True)
                self._save_current_state()
                self.previewer.clear()
                self.previewer.close()
                self.spectrum.stop()
                self._set_power_inhibit(False)
                if self.mpv:
                    try:
                        self.mpv.terminate()
                    except Exception:
                        logger.exception("MPV shutdown failed")
                    finally:
                        self.mpv = None

    def _save_current_state(self) -> None:
        """Persist current playback position, track, and settings to .cache_playback.json."""
        curr = self.queue.current_track
        if not curr:
            return
        state = PlaybackState(
            playlist_url=self.playlist_url,
            track_id=curr.id,
            track_index=self.queue.current_idx,
            time_pos=self._time_pos,
            duration=self._duration,
            repeat_mode=self.queue.repeat_mode.value,
            volume=self._volume,
            updated_at=time.time(),
        )
        save_playback_state(state)

    async def load_playlist(self, url: str) -> None:
        """Load playlist from cache first, then fetch updates via yt_dlp (once per week)."""
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

            self._selected_list_idx = target_idx
            self.queue.current_idx = target_idx
            if self.auto_play:
                self._play_index(target_idx, start_pos=start_pos)

        # 2. Fetch fresh tracks only when cache is missing or older than 7 days
        if not is_playlist_cache_stale(url):
            # Cache is fresh — nothing to do
            if cached:
                return
            # No cache yet but stale check failed (shouldn't happen, but guard)
        self._is_loading_playlist = True
        self._status_msg = "Fetching playlist..." if not cached else "Syncing playlist..."
        try:
            tracks = await loop.run_in_executor(None, fetch_playlist_tracks, url)
            if not tracks:
                if not cached:
                    self._status_msg = "No tracks found."
                return

            active_track = self.queue.current_track
            active_index = self.queue.current_idx
            selected_track = None
            filtered = self.queue.filtered_tracks
            if 0 <= self._selected_list_idx < len(filtered):
                selected_track = filtered[self._selected_list_idx][1]

            cache_saved = save_cached_playlist(url, tracks)
            if not save_default_playlist(url, self.config_file):
                logger.warning("Unable to persist loaded playlist %s", url)
            runtime_tracks = list(tracks)
            if active_track and all(track.id != active_track.id for track in runtime_tracks):
                runtime_tracks.insert(min(active_index, len(runtime_tracks)), active_track)
            self.queue.set_tracks(runtime_tracks, reset_index=not bool(cached))
            if selected_track:
                self._selected_list_idx = next(
                    (i for i, (_orig, track) in enumerate(self.queue.filtered_tracks) if track.id == selected_track.id),
                    min(self._selected_list_idx, max(0, len(self.queue.filtered_tracks) - 1)),
                )
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
                self._selected_list_idx = target_idx
                self.queue.current_idx = target_idx
                if self.auto_play:
                    self._play_index(target_idx, start_pos=start_pos)
        except PlaylistFetchError as err:
            self._status_msg = f"Offline; using cached playlist. {err}" if cached else f"Fetch Error: {err}"
            logger.warning("Playlist refresh failed for %s: %s", url, err)
        except Exception as err:
            self._status_msg = f"Playlist error: {err}"
            logger.exception("Unexpected playlist failure for %s", url)
        finally:
            self._is_loading_playlist = False

    def _play_index(self, index: int, start_pos: float = 0.0) -> None:
        if not self.queue.tracks or not (0 <= index < len(self.queue.tracks)):
            return
        self.queue.current_idx = index
        track = self.queue.tracks[index]
        self._is_paused = False
        self._is_buffering = True

        if self.mpv:
            try:
                self._pending_seek = (track.id, start_pos) if start_pos > 1.0 else None
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

        # Enable visualizer once playback begins
        if self.mpv and self._show_visualizer:
            self.spectrum.resume()

        self.previewer.request(track)

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
            self._failed_track_ids.add(current.id)
            error = event.get("file_error") or event.get("error") or "media unavailable"
            self._status_msg = f"Skipped {current.title}: {error}"
            logger.warning("Playback failed for %s: %s", current.id, error)

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
            self._play_index(nxt)
        else:
            self._is_buffering = False
            self._is_paused = True
            self._set_power_inhibit(False)
            if reason == "eof":
                self._status_msg = "Playlist finished."

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
                    self._is_buffering = bool(is_caching or (core_idle and not self._is_paused and self._time_pos == 0.0))
                    if self._time_pos > 0.0 and self.queue.current_track:
                        self._failed_track_ids.discard(self.queue.current_track.id)

                    # Auto-pause visualizer when source is gone/buffering, resume when playing
                    if self._show_visualizer:
                        source_active = not self._is_buffering and not self._is_paused
                        if source_active and not self.spectrum.enabled:
                            self.spectrum.resume()
                        elif not source_active and self.spectrum.enabled:
                            self.spectrum.pause()

                    # Periodically save playback state every 3 seconds
                    now = time.time()
                    if now - self._last_state_save >= 3.0:
                        self._save_current_state()
                        self._last_state_save = now
                    self._mpv_poll_error_reported = False
                except Exception as err:
                    if not self._mpv_poll_error_reported:
                        self._status_msg = f"MPV status error: {err}"
                        logger.warning("Unable to poll MPV status: %s", err)
                        self._mpv_poll_error_reported = True
            await asyncio.sleep(0.2)

    async def _input_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            key = await loop.run_in_executor(None, self.term.inkey, 0.05)
            if not key:
                await asyncio.sleep(0.01)
                continue
            self._handle_key(key)

    def _handle_key(self, key: Any) -> None:
        self._render_requested.set()
        key_name = getattr(key, "name", None) or ""
        # Input mode active (Live Fuzzy Search / URL prompt)
        if self._input_mode != "none":
            if key_name == "KEY_ENTER" or key == "\n" or key == "\r":
                if self._input_mode == "search":
                    # Play the highlighted fuzzy match or dismiss input.
                    filtered = self.queue.filtered_tracks
                    if 0 <= self._selected_list_idx < len(filtered):
                        orig_idx = filtered[self._selected_list_idx][0]
                        self._play_index(orig_idx)
                elif self._input_mode == "url" and self._input_buffer.strip():
                    candidate = self._input_buffer.strip()
                    try:
                        validate_media_url(candidate)
                    except InvalidMediaURL as err:
                        self._status_msg = str(err)
                    else:
                        if self._playlist_task and not self._playlist_task.done():
                            self._playlist_task.cancel()
                        self._playlist_task = asyncio.create_task(self.load_playlist(candidate))
                self._input_mode = "none"
                self._input_buffer = ""
            elif key_name == "KEY_ESCAPE":
                if self._input_mode == "search":
                    self.queue.clear_filter()
                self._input_mode = "none"
                self._input_buffer = ""
            elif self._input_mode == "search" and key_name == "KEY_UP":
                self._move_list_selection(-1)
            elif self._input_mode == "search" and key_name == "KEY_DOWN":
                self._move_list_selection(1)
            elif key_name in ("KEY_BACKSPACE", "KEY_DELETE") or key == "\x7f" or key == "\x08":
                self._input_buffer = self._input_buffer[:-1]
                if self._input_mode == "search":
                    self.queue.set_filter(self._input_buffer)
                    self._selected_list_idx = 0
                    self._scroll_offset = 0
            elif not getattr(key, "is_sequence", False) and len(key) == 1 and 32 <= ord(key) <= 126:
                self._input_buffer += str(key)
                if self._input_mode == "search":
                    self.queue.set_filter(self._input_buffer)
                    self._selected_list_idx = 0
                    self._scroll_offset = 0
            return

        if self._show_help:
            if key in ("?", "\x1b") or key_name == "KEY_ESCAPE":
                self._show_help = False
            elif key in ("q", "Q"):
                self._running = False
            return

        # Player navigation shortcuts
        if key in ("q", "Q"):
            self._running = False
        elif key == "?":
            self._show_help = True
        elif key == " ":
            if self.mpv:
                try:
                    self._is_paused = not getattr(self.mpv, "pause", False)
                    self.mpv.pause = self._is_paused
                    self._set_power_inhibit(not self._is_paused)
                except Exception as err:
                    self._status_msg = f"Pause failed: {err}"
                    logger.warning("MPV pause failed: %s", err)
        elif key in ("n", "N"):
            nxt = self.queue.get_next_index(natural_end=False)
            if nxt is not None:
                self._play_index(nxt)
        elif key in ("p", "P"):
            prev = self.queue.get_prev_index()
            if prev is not None:
                self._play_index(prev)
        elif key in ("s", "S"):
            self.queue.shuffle()
        elif key in ("r", "R"):
            self.queue.repeat_mode = self.queue.repeat_mode.next_mode()
        elif (self._vim_mode and key == "h") or (
            not self._vim_mode and key_name == "KEY_LEFT"
        ):
            if self.mpv:
                try:
                    self.mpv.command("seek", -5, "relative")
                except Exception as err:
                    self._status_msg = f"Seek failed: {err}"
                    logger.warning("MPV seek failed: %s", err)
        elif (self._vim_mode and key == "l") or (
            not self._vim_mode and key_name == "KEY_RIGHT"
        ):
            if self.mpv:
                try:
                    self.mpv.command("seek", 5, "relative")
                except Exception as err:
                    self._status_msg = f"Seek failed: {err}"
                    logger.warning("MPV seek failed: %s", err)
        elif key == "[":
            if self.mpv:
                try:
                    self.mpv.command("seek", -30, "relative")
                except Exception as err:
                    self._status_msg = f"Seek failed: {err}"
                    logger.warning("MPV seek failed: %s", err)
        elif key == "]":
            if self.mpv:
                try:
                    self.mpv.command("seek", 30, "relative")
                except Exception as err:
                    self._status_msg = f"Seek failed: {err}"
                    logger.warning("MPV seek failed: %s", err)
        elif key in ("+", "="):
            self._volume = min(100, self._volume + 5)
            if self.mpv:
                try:
                    self.mpv.volume = self._volume
                except Exception as err:
                    self._status_msg = f"Volume change failed: {err}"
                    logger.warning("MPV volume change failed: %s", err)
        elif key in ("-", "_"):
            self._volume = max(0, self._volume - 5)
            if self.mpv:
                try:
                    self.mpv.volume = self._volume
                except Exception as err:
                    self._status_msg = f"Volume change failed: {err}"
                    logger.warning("MPV volume change failed: %s", err)
        elif key in ("m", "M"):
            self._is_muted = not self._is_muted
            if self.mpv:
                try:
                    self.mpv.mute = self._is_muted
                except Exception as err:
                    self._status_msg = f"Mute failed: {err}"
                    logger.warning("MPV mute failed: %s", err)
        elif key == "v":
            self._show_visualizer = not self._show_visualizer
            save_visualizer_preferences(self._spectrum_style, self._show_visualizer, self.config_file)
            if self._show_visualizer:
                self.spectrum.resume()
            else:
                self.spectrum.pause()
                self.previewer.clear()
            print(self.term.clear, end="", flush=True)
        elif key == "V":
            self._vim_mode = not self._vim_mode
            save_vim_mode(self._vim_mode, self.config_file)
        elif key in ("a", "A"):
            style_index = self.SPECTRUM_STYLES.index(self._spectrum_style)
            self._spectrum_style = self.SPECTRUM_STYLES[(style_index + 1) % len(self.SPECTRUM_STYLES)]
            save_visualizer_preferences(self._spectrum_style, self._show_visualizer, self.config_file)
            self._spectrum_history.clear()
        elif key in ("t", "T"):
            self._show_preview = not self._show_preview
            if not self._show_preview:
                self.previewer.clear()
            print(self.term.clear, end="", flush=True)
        elif key in ("/", "f", "F"):
            self._input_mode = "search"
            self._input_buffer = ""
        elif key in ("o", "O"):
            self._input_mode = "url"
            self._input_buffer = ""
        elif (self._vim_mode and key == "k") or (
            not self._vim_mode and key_name == "KEY_UP"
        ):
            self._move_list_selection(-1)
        elif (self._vim_mode and key == "j") or (
            not self._vim_mode and key_name == "KEY_DOWN"
        ):
            self._move_list_selection(1)
        elif key_name == "KEY_ENTER" or key == "\n" or key == "\r":
            filtered = self.queue.filtered_tracks
            if 0 <= self._selected_list_idx < len(filtered):
                orig_idx = filtered[self._selected_list_idx][0]
                self._play_index(orig_idx)
        elif key_name.startswith("MOUSE_"):
            self._handle_mouse(key)

    def _handle_mouse(self, key: Any) -> None:
        """Handle track-list clicks and mouse wheel navigation."""
        try:
            _mouse_x, mouse_y = key.mouse_xy  # (x, y) — 0-indexed
        except (AttributeError, TypeError, ValueError):
            logger.debug("Ignoring malformed mouse event", exc_info=True)
            return

        # Check if click landed in the track list area
        if self._list_height <= 0:
            return
        list_end_row = self._list_top_row + self._list_height
        if not (self._list_top_row <= mouse_y < list_end_row):
            return

        mouse_name = getattr(key, "name", "")
        if mouse_name in {"MOUSE_SCROLL_UP", "MOUSE_WHEEL_UP"}:
            self._move_list_selection(-1)
            return
        if mouse_name in {"MOUSE_SCROLL_DOWN", "MOUSE_WHEEL_DOWN"}:
            self._move_list_selection(1)
            return
        if not (mouse_name == "MOUSE_LEFT" or key.is_mouse_left()):
            return

        # Map screen row to filtered list index
        clicked_item_idx = self._scroll_offset + (mouse_y - self._list_top_row)
        filtered = self.queue.filtered_tracks
        if 0 <= clicked_item_idx < len(filtered):
            orig_idx = filtered[clicked_item_idx][0]
            self._selected_list_idx = clicked_item_idx
            self._play_index(orig_idx)

    def _move_list_selection(self, delta: int) -> None:
        """Move list cursor and keep its visible viewport valid after resizes."""
        filtered_len = len(self.queue.filtered_tracks)
        if not filtered_len:
            self._selected_list_idx = 0
            self._scroll_offset = 0
            return
        self._selected_list_idx = max(0, min(filtered_len - 1, self._selected_list_idx + delta))
        list_height = max(1, self._list_height)
        if self._selected_list_idx < self._scroll_offset:
            self._scroll_offset = self._selected_list_idx
        elif self._selected_list_idx >= self._scroll_offset + list_height:
            self._scroll_offset = self._selected_list_idx - list_height + 1
        self._scroll_offset = min(self._scroll_offset, max(0, filtered_len - list_height))

    def _render_spectrum_lines(self, bands: List[float], height: int, width: int) -> List[str]:
        if self._spectrum_style == "waterfall":
            self._spectrum_history.append(bands)
            self._spectrum_history = self._spectrum_history[-height:]
            padding = [[0.0] * width] * max(0, height - len(self._spectrum_history))
            rows = padding + self._spectrum_history
            return [
                "".join(self.SPECTRUM_CHARS[min(len(self.SPECTRUM_CHARS) - 1, int(value * (len(self.SPECTRUM_CHARS) - 1)))] for value in row)
                for row in rows
            ]

        self._spectrum_history.clear()
        lines = [[" "] * width for _ in range(height)]
        if self._spectrum_style == "waveform":
            for col, value in enumerate(bands[:width]):
                row = height - 1 - min(height - 1, int(value * height))
                lines[row][col] = "•"
            return ["".join(line) for line in lines]

        if self._spectrum_style == "braille":
            levels = height * 4
            for col in range(width):
                left = bands[min(len(bands) - 1, col * 2)]
                right = bands[min(len(bands) - 1, col * 2 + 1)]
                for row in range(height):
                    mask = 0
                    for dot in range(4):
                        level = levels - (row * 4 + dot)
                        if left * levels >= level:
                            mask |= self.BRAILLE_LEFT[dot]
                        if right * levels >= level:
                            mask |= self.BRAILLE_RIGHT[dot]
                    lines[row][col] = chr(0x2800 + mask)
            return ["".join(line) for line in lines]

        for col, value in enumerate(bands[:width]):
            active_levels = int(value * height * 8)
            for row in range(height):
                row_level = active_levels - ((height - 1 - row) * 8)
                if row_level >= 8:
                    lines[row][col] = "█"
                elif row_level > 0:
                    lines[row][col] = "▁▂▃▄▅▆▇"[row_level - 1]
        return ["".join(line) for line in lines]

    async def _render_loop(self) -> None:
        while self._running:
            signature = self._render_signature()
            if self._show_visualizer or signature != self._last_render_signature:
                self._render()
                self._last_render_signature = signature
            delay = self._render_delay()
            self._render_requested.clear()
            try:
                await asyncio.wait_for(self._render_requested.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _render_delay(self) -> float:
        """Limit expensive full-screen spectrum redraws while keeping input immediate."""
        return self.VISUALIZER_RENDER_INTERVAL if self._show_visualizer else self.IDLE_RENDER_INTERVAL

    def _render_signature(self) -> Tuple[Any, ...]:
        """Capture cheap UI state so static screens are not repainted at 10 Hz."""
        animated_frame = int(time.time() * 10) if self._is_loading_playlist or self._is_buffering else 0
        return (
            self.term.width,
            self.term.height,
            self.queue.current_idx,
            len(self.queue.tracks),
            self._selected_list_idx,
            self._scroll_offset,
            self._input_mode,
            self._input_buffer,
            self._is_paused,
            self._is_muted,
            self._is_loading_playlist,
            self._is_buffering,
            self._show_preview,
            self._vim_mode,
            self._show_help,
            self._volume,
            self.queue.repeat_mode,
            self._status_msg,
            int(self._time_pos),
            int(self._duration),
            self.previewer.generation,
            animated_frame,
        )

    def _render_help(self) -> None:
        """Render a compact keyboard reference without leaving the player."""
        term = self.term
        width = term.width
        lines = [
            " MPF HELP",
            "",
            " Playback",
            "   Space       Play or pause",
            "   n / p       Next or previous track",
            "   s           Shuffle queue",
            "   r           Cycle repeat mode",
            "   m           Mute",
            "   + / -       Change volume",
            f"   {'h / l' if self._vim_mode else 'Left/Right':11} Seek 5 seconds",
            "   [ / ]       Seek 30 seconds",
            "",
            " Library",
            f"   {'j / k' if self._vim_mode else 'Up/Down':11} Browse tracks",
            "   Enter       Play selected track",
            "   /           Search tracks",
            "   o           Open a YouTube URL",
            "",
            f" Vim mode: {'enabled' if self._vim_mode else 'disabled'} (V toggles it)",
            "",
            "   v           Toggle visualizer",
            "   a           Change visualizer style",
            "   t           Toggle thumbnail preview",
            "   ? / Esc     Close help",
            "   q           Quit",
        ]
        output = [term.clear + term.home]
        for row, line in enumerate(lines[: term.height]):
            style = term.bold_cyan if row == 0 else term.white
            output.append(term.move_xy(2, row) + style(line[: max(0, width - 3)]) + term.clear_eol)
        print("".join(output), end="", flush=True)

    @staticmethod
    def _progress_meter(width: int, progress: float) -> str:
        """Return a fixed-width dashboard progress meter."""
        width = max(1, width)
        marker = round(max(0.0, min(1.0, progress)) * (width - 1))
        return "━" * marker + "●" + "─" * (width - marker - 1)

    def _dashboard_summary(self) -> str:
        volume_icon = "󰝟" if self._is_muted else ""
        return (
            f"{volume_icon} {self._volume}%  ·  {self.queue.repeat_mode.icon}"
            f"  ·  󰎆 {len(self.queue.tracks)}"
        )

    def _queue_heading(self, filtered_count: int) -> str:
        count = (
            f"{filtered_count}/{len(self.queue.tracks)}"
            if self._input_buffer
            else str(len(self.queue.tracks))
        )
        return f"  {count} "

    def _render(self) -> None:
        term = self.term
        h, w = term.height, term.width
        if (w, h) != self._last_render_size:
            self.previewer.clear()
            self._last_render_size = (w, h)
        if h < 10 or w < 30:
            self._list_height = 0
            print(term.home + term.red("Terminal window too small!"), end="", flush=True)
            return

        if self._show_help:
            self._render_help()
            return

        out: List[str] = [term.home]
        row = 0

        # Animated Spinner
        spinner = SPINNER_FRAMES[int(time.time() * 10) % len(SPINNER_FRAMES)]

        # 1. Dashboard header and now-playing details
        curr_track = self.queue.current_track
        if self._is_loading_playlist:
            state = f"{spinner} LOADING"
        elif self._is_buffering and curr_track:
            state = f"{spinner} BUFFERING"
        elif self._is_paused:
            state = "Ⅱ PAUSED"
        elif curr_track:
            state = "▶ PLAYING"
        else:
            state = "■ IDLE"

        summary = self._dashboard_summary()
        brand = f" MPF  {state}"
        gap = max(2, w - len(brand) - len(summary) - 1)
        header_text = brand + (" " * gap + summary if gap > 2 else "")
        out.append(term.move_xy(0, row) + term.bold_cyan(header_text[: w - 1]) + term.clear_eol)
        row += 1

        title = curr_track.title if curr_track else "Nothing playing"
        out.append(
            term.move_xy(0, row)
            + term.cyan(" 󰎈  ")
            + term.bold_white(title[: max(0, w - 15)])
            + term.clear_eol
        )
        row += 1

        detail = curr_track.uploader if curr_track and curr_track.uploader else self._status_msg
        out.append(term.move_xy(0, row) + term.white(f" {detail}"[: w - 1]) + term.clear_eol)
        row += 1

        # 2. Timeline
        elapsed = format_time(self._time_pos)
        duration = format_time(self._duration)
        bar_width = max(5, w - len(elapsed) - len(duration) - 5)
        pct = (self._time_pos / self._duration) if self._duration > 0 else 0.0
        bar_str = self._progress_meter(bar_width, pct)
        out.append(
            term.move_xy(0, row)
            + term.white(f" {elapsed} ")
            + term.green(bar_str)
            + term.white(f" {duration}")
            + term.clear_eol
        )
        row += 2

        # 3. Side-by-side artwork and spectrum
        preview_width = 16
        show_preview_box = self._show_preview and w >= 55 and h >= 18

        if self._show_visualizer and h >= 18:
            panel_label = " " if show_preview_box else ""
            viz_label_col = preview_width + 4 if show_preview_box else 2
            out.append(term.move_xy(0, row) + term.cyan(panel_label))
            out.append(
                term.move_xy(viz_label_col, row)
                + term.cyan(f"󰓃  {self._spectrum_style.upper()}")
                + term.clear_eol
            )
            row += 1
            spec_height = min(10, h - row - 8)
            if spec_height >= 3:
                if show_preview_box:
                    for r_idx in range(spec_height):
                        out.append(term.move_xy(2, row + r_idx) + " " * preview_width)
                    viz_start_col = preview_width + 4
                else:
                    viz_start_col = 2

                # Render Spectrum Analyzer
                viz_avail_width = w - viz_start_col - 2
                num_bands = viz_avail_width * 2 if self._spectrum_style == "braille" else viz_avail_width
                bands, _peaks = self.spectrum.get_bands(num_bands)
                spectrum_lines = self._render_spectrum_lines(bands, spec_height, viz_avail_width)

                for r in range(spec_height):
                    current_screen_row = row + r
                    out.append(term.move_xy(viz_start_col, current_screen_row) + term.bold_cyan(spectrum_lines[r]) + term.clear_eol)

                # Render Kitty image overlay
                if show_preview_box:
                    self.previewer.render(curr_track, x=2, y=row, width=preview_width, height=spec_height)

                row += spec_height + 1
        else:
            self._spectrum_history.clear()
            self.previewer.clear()

        # 4. Input prompt
        if self._input_mode != "none":
            prompt_label = "SEARCH" if self._input_mode == "search" else "OPEN URL"
            input_text = f" {prompt_label}  {self._input_buffer}█"
            out.append(term.move_xy(0, row) + term.bold_yellow(input_text[: w - 1]) + term.clear_eol)
            row += 1

        # 5. Queue (ranked by fuzzy score when query active)
        filtered = self.queue.filtered_tracks
        # How many footer rows do we need? Compute first, then shrink list_height.
        # Build footer lines that fit within terminal width
        footer_lines: List[str] = []
        current_line = ""
        footer_hints = list(self.FOOTER_HINTS)
        if self._vim_mode:
            footer_hints = [
                hint.replace("↑/↓ browse", "j/k browse").replace("←/→ seek", "h/l seek")
                for hint in footer_hints
            ]
        footer_hints.insert(1, "V key mode")
        for hint in footer_hints:
            decorated = f"[{hint.split(' ', 1)[0]}]" + (f" {hint.split(' ', 1)[1]}" if " " in hint else "")
            candidate = (current_line + "  " + decorated) if current_line else (" " + decorated)
            if current_line and len(candidate) > w - 1:
                footer_lines.append(current_line)
                current_line = " " + decorated
            else:
                current_line = candidate
        if current_line:
            footer_lines.append(current_line)
        num_footer_rows = len(footer_lines)

        list_height = max(1, h - row - 1 - num_footer_rows)

        self._list_height = list_height
        self._move_list_selection(0)

        list_header = self._queue_heading(len(filtered))
        out.append(
            term.move_xy(0, row)
            + term.magenta(list_header + "─" * max(0, w - len(list_header) - 1))
            + term.clear_eol
        )
        row += 1

        # Store for mouse hit-testing
        self._list_top_row = row
        self._list_height = list_height

        for i in range(list_height):
            item_idx = self._scroll_offset + i
            current_screen_row = row + i
            if item_idx >= len(filtered):
                out.append(term.move_xy(0, current_screen_row) + term.clear_eol)
                continue

            orig_idx, track = filtered[item_idx]
            is_active = orig_idx == self.queue.current_idx
            is_cursor = item_idx == self._selected_list_idx

            label = track.display_label(orig_idx, is_active=is_active, max_width=w - 2)
            if is_active and is_cursor:
                formatted = term.bold_green_reverse(f" {label}"[: w - 1])
            elif is_active:
                formatted = term.bold_green(f" {label}"[: w - 1])
            elif is_cursor:
                formatted = term.reverse(f" {label}"[: w - 1])
            else:
                formatted = term.white(f" {label}"[: w - 1])

            out.append(term.move_xy(0, current_screen_row) + formatted + term.clear_eol)

        # Clear remaining unused rows below track list (above footer)
        footer_start_row = h - num_footer_rows
        for r_clear in range(row + list_height, footer_start_row):
            out.append(term.move_xy(0, r_clear) + term.clear_eol)

        # 6. Compact command bar
        for fi, fline in enumerate(footer_lines):
            out.append(
                term.move_xy(0, footer_start_row + fi)
                + term.cyan(fline[: w - 1])
                + term.clear_eol
            )

        print("".join(out), end="", flush=True)
