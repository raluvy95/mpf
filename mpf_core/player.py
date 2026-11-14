"""TUI client: draws the playback engine's state with blessed and turns keys into its controls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from blessed import Terminal

from mpf_core.cache import (
    SavedPlaylist,
    list_saved_playlists,
    set_playlist_title,
)
from mpf_core.client import RemoteEngine
from mpf_core.config import (
    BAR_POSITIONS,
    COLOR_MODES,
    OPEN_VIEWS,
    VISUALIZER_STYLES,
    load_bar_position,
    load_color_mode,
    load_default_playlist,
    load_open_view,
    load_rainbow_colors,
    load_vim_mode,
    load_visualizer_preferences,
    parse_rainbow_colors,
    parse_ytdl_fallback_clients,
    save_bar_position,
    save_color_mode,
    save_open_view,
    save_rainbow_colors,
    save_vim_mode,
    save_visualizer_preferences,
)
from mpf_core.fetcher import (
    InvalidMediaURL,
    PlaylistFetchError,
    fetch_playlist_title,
    validate_media_url,
)
from mpf_core.models import Track, format_time
from mpf_core.paths import CONFIG_FILE
from mpf_core.previewer import KittyPreviewer
from mpf_core.visualizer import PipeWireSpectrumAnalyzer

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

logger = logging.getLogger(__name__)


class MediaPlayerFluere(RemoteEngine):
    """Asyncio-driven TUI player using Blessed for true transparent terminal rendering."""

    SPECTRUM_CHARS = " .:-=+*#%@"
    SPECTRUM_STYLES = VISUALIZER_STYLES
    BRAILLE_LEFT = (0x1, 0x2, 0x4, 0x40)
    BRAILLE_RIGHT = (0x8, 0x10, 0x20, 0x80)
    BRAILLE_TABLE = np.array([chr(0x2800 + mask) for mask in range(256)], dtype="<U1")
    VISUALIZER_RENDER_INTERVAL = 1 / 15
    IDLE_RENDER_INTERVAL = 0.1
    MIN_RENDER_INTERVAL = 1 / 30  # coalesces bursts of input into one redraw
    INPUT_POLL_INTERVAL = 0.25  # inkey returns at once on a keypress; this only bounds shutdown latency
    SHUFFLE_ICONS = {True: "\U000f049f", False: "\U000f049e"}
    SUMMARY_SEPARATOR = "  ·  "
    FOOTER_HINTS = [("?", "help"), ("Space", "pause"), ("/", "search"), ("o", "open"), ("q", "quit"), (",", "settings")]

    def __init__(
        self,
        playlist_url: str = "",
        auto_play: bool = True,
        config_file: str = CONFIG_FILE,
        show_visualizer: Optional[bool] = None,
        show_preview: bool = True,
        vim_mode: Optional[bool] = None,
        force_refresh: bool = False,
    ) -> None:
        super().__init__(
            playlist_url=playlist_url, auto_play=auto_play, config_file=config_file, force_refresh=force_refresh
        )
        self.term = Terminal()
        self.spectrum = PipeWireSpectrumAnalyzer(num_bands=32, target_node="mpv")
        self.previewer = KittyPreviewer()

        self._spectrum_style, configured_visualizer = load_visualizer_preferences(config_file)
        self._show_visualizer = configured_visualizer if show_visualizer is None else show_visualizer
        self._show_preview = show_preview
        self._rainbow_colors = load_rainbow_colors(config_file)
        self._bar_position = load_bar_position(config_file)
        self._open_view = load_open_view(config_file)
        self._color_mode = load_color_mode(config_file)
        # Per-cell intensity (0..1) of the last frame for styles that are not bars; None for bar styles.
        self._cell_levels: Optional[np.ndarray] = None
        self._vim_mode = load_vim_mode(config_file) if vim_mode is None else vim_mode
        self._show_help = False
        self._show_settings = False
        self._settings_msg = ""
        self._settings_idx = 0  # highlighted row on the settings screen
        self._help_scroll = 0  # first visible body line of help or settings when the terminal is too short
        self._settings_focus = 0  # line of the highlighted settings row
        self._settings_footer = 0  # trailing settings lines (prompt or message, hint) kept on screen

        # UI state
        self._selected_list_idx = 0
        self._scroll_offset = 0
        self._input_mode = "none"  # "search" | "url" | "playlist" | "clients" | "rainbow" | "none"
        self._input_text = ""
        self._input_cursor = 0  # insertion point within the prompt text
        self._input_error = ""  # why the open prompt rejected its URL; shown under the prompt
        self._open_saved: List[SavedPlaylist] = []  # playlists the open dialog offers, newest first
        self._open_idx = 0  # highlighted saved playlist
        self._open_scroll = 0  # first visible saved playlist
        self._open_titles = 0  # bumped whenever a missing playlist title arrives, to redraw the dialog
        self._title_task: Optional[asyncio.Task[None]] = None
        self._last_render_signature: Optional[Tuple[Any, ...]] = None
        self._render_requested = asyncio.Event()
        # Track list layout — updated each render so mouse clicks map to rows
        self._list_top_row = 0
        self._list_height = 0
        self._last_render_size = (0, 0)
        self._spectrum_history: List[List[float]] = []
        self._spectrum_mode: Optional[str] = None  # "run" | "freeze" | "clear"
        self._help_visible = False
        self._timeline_hit: Optional[Tuple[int, int, int]] = None  # row, first column, width
        self._summary_hits: List[Tuple[int, int, str]] = []  # first column, end column, action
        self._scrubbing = False
        self._move_cache: Dict[Tuple[int, int], str] = {}
        self._move_cache_term: Optional[Terminal] = None
        self._search_origin: Tuple[int, int] = (0, 0)  # selection and scroll before searching

    # ── Hooks: what the screen does when the daemon reports a change ────────

    def _notify(self) -> None:
        super()._notify()
        self._render_requested.set()

    def _on_starting_daemon(self) -> None:
        print("Starting the background player...", flush=True)

    def _on_track_started(self, track: Track) -> None:
        self._spectrum_mode = None
        if self._connected and self._show_visualizer:
            self.spectrum.resume()  # enable the visualizer once playback begins
        self.previewer.request(track)

    def _after_poll(self) -> None:
        # Run the visualizer while playing, freeze it on pause, clear it otherwise
        if self._show_visualizer:
            if self._is_buffering or self._mpv_idle:
                self._set_spectrum_mode("clear")
            elif self._is_paused:
                self._set_spectrum_mode("freeze")
            else:
                self._set_spectrum_mode("run")

    def _on_queue_loaded(self, index: int) -> None:
        self._selected_list_idx = index

    def _selection_anchor(self) -> Any:
        filtered = self.queue.filtered_tracks
        if 0 <= self._selected_list_idx < len(filtered):
            return filtered[self._selected_list_idx][1]
        return None

    def _restore_selection(self, anchor: Any) -> None:
        if anchor:
            self._selected_list_idx = next(
                (i for i, (_orig, track) in enumerate(self.queue.filtered_tracks) if track.id == anchor.id),
                min(self._selected_list_idx, max(0, len(self.queue.filtered_tracks) - 1)),
            )

    async def run(self) -> None:
        """Attach to the background player, then run the screen and keys until quit."""
        await self.attach()  # raises DaemonUnavailable or VersionMismatch before the screen is taken over
        self.spectrum.start()

        with contextlib.ExitStack() as screen:
            screen.enter_context(self.term.fullscreen())
            screen.enter_context(self.term.cbreak())
            screen.enter_context(self.term.hidden_cursor())
            screen.enter_context(self.term.mouse_enabled(report_drag=True))
            tasks = [asyncio.create_task(self._render_loop()), asyncio.create_task(self._input_loop())]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass
            finally:
                self._running = False
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.previewer.clear()
                self.previewer.close()
                self.spectrum.stop()
                self.close()  # only disconnects: the music carries on in the background player

    def _move(self, x: int, y: int) -> str:
        """Return the cursor-positioning sequence for (x, y).

        Cached because blessed re-parses the terminfo template on every call (~250 us),
        which dominated the cost of a frame; the sequence for a position never changes.
        """
        term = self.term
        if term is not self._move_cache_term or len(self._move_cache) > 8192:
            self._move_cache = {}
            self._move_cache_term = term
        sequence = self._move_cache.get((x, y))
        if sequence is None:
            sequence = self._move_cache[(x, y)] = term.move_xy(x, y)
        return sequence









    def _set_spectrum_mode(self, mode: str) -> None:
        """Run, freeze, or clear the visualizer, applying changes only on transitions."""
        if mode == self._spectrum_mode:
            return
        self._spectrum_mode = mode
        if mode == "run":
            self.spectrum.resume()
        elif mode == "freeze":
            self.spectrum.freeze()
        else:
            self.spectrum.pause()

    # ── Playback controls shared by the keyboard and MPRIS ──────────────────















    def _activate_summary(self, action: str) -> None:
        """Run the control behind a clicked dashboard summary icon."""
        if action == "shuffle":
            self.set_shuffle(not self.queue.shuffle_enabled)
        elif action == "repeat":
            self._cycle_repeat()

    def _seek_to_column(self, column: int) -> None:
        """Jump to the point of the track under a mouse column on the progress bar."""
        if not self._timeline_hit or not self._connected or self._duration <= 0:
            return
        _row, bar_x, bar_width = self._timeline_hit
        fraction = max(0.0, min(1.0, (column - bar_x) / max(1, bar_width - 1)))
        target = fraction * self._duration
        self.seek_absolute(target)

    async def _input_loop(self) -> None:
        """Feed keys to the handler from a daemon thread, so quitting never waits for a pending poll.

        The pool thread this replaces had to finish its poll before the interpreter could exit, which put
        up to ``INPUT_POLL_INTERVAL`` on every quit.
        """
        loop = asyncio.get_running_loop()
        keys: asyncio.Queue[Any] = asyncio.Queue()

        def read_keys() -> None:
            while self._running:
                key = self.term.inkey(timeout=self.INPUT_POLL_INTERVAL)
                if key:
                    loop.call_soon_threadsafe(keys.put_nowait, key)

        threading.Thread(target=read_keys, name="mpf-input", daemon=True).start()
        while self._running:
            self._handle_key(await keys.get())

    def _handle_key(self, key: Any) -> None:
        """Dispatch a key and request a redraw only if it changed what is on screen."""
        before = self._render_signature()
        self._dispatch_key(key)
        if self._render_signature() != before:
            self._render_requested.set()

    def _dispatch_key(self, key: Any) -> None:
        key_name = getattr(key, "name", None) or ""
        # Input mode active (Live Fuzzy Search / URL prompt)
        if self._input_mode != "none":
            if key_name == "KEY_ENTER" or key == "\n" or key == "\r":
                if self._input_mode == "search":
                    # Play the highlighted fuzzy match, or dismiss the search if nothing matches.
                    filtered = self.queue.filtered_tracks
                    if 0 <= self._selected_list_idx < len(filtered):
                        orig_idx = filtered[self._selected_list_idx][0]
                        self._play_index(orig_idx)
                        self._exit_search(played_idx=orig_idx)
                    else:
                        self._exit_search()
                elif self._input_mode == "url":
                    candidate = self._input_buffer.strip()
                    if not candidate and 0 <= self._open_idx < len(self._open_saved):
                        candidate = self._open_saved[self._open_idx].url  # nothing typed: the chosen playlist
                    try:
                        if not candidate:
                            raise InvalidMediaURL("Enter a YouTube playlist or video URL")
                        validate_media_url(candidate)
                    except InvalidMediaURL as err:
                        # Stay in the prompt with the text intact so the URL can be corrected; the status
                        # line is hidden while a track plays, so the error is drawn under the prompt.
                        self._input_error = str(err)
                        return
                    self.load_url(candidate)
                elif self._input_mode in ("playlist", "clients", "rainbow"):
                    self._apply_setting_input(self._input_mode, self._input_buffer)
                self._input_mode = "none"
                self._input_buffer = ""
            elif key_name == "KEY_ESCAPE":
                if self._input_mode == "search":
                    self._exit_search()
                self._input_mode = "none"
                self._input_buffer = ""
                self._input_error = ""
            elif self._input_mode == "search" and key_name == "KEY_UP":
                self._move_list_selection(-1)
            elif self._input_mode == "search" and key_name == "KEY_DOWN":
                self._move_list_selection(1)
            elif self._input_mode == "url" and key_name in ("KEY_UP", "KEY_DOWN", "KEY_TAB", "KEY_BTAB"):
                step = {"KEY_UP": -self._open_columns(), "KEY_DOWN": self._open_columns(), "KEY_TAB": 1, "KEY_BTAB": -1}
                self._move_open_selection(step[key_name])
            elif self._input_mode == "url" and key in ("o", "O") and not self._input_text:
                self._input_mode = "none"  # the key that opened the dialog also hides it while nothing is typed
                self._input_error = ""
            elif key_name == "KEY_LEFT":
                self._input_cursor = max(0, self._input_cursor - 1)
            elif key_name == "KEY_RIGHT":
                self._input_cursor = min(len(self._input_text), self._input_cursor + 1)
            elif key_name == "KEY_HOME" or key == "\x01":
                self._input_cursor = 0
            elif key_name == "KEY_END" or key == "\x05":
                self._input_cursor = len(self._input_text)
            elif key_name == "KEY_DELETE":
                text, cursor = self._input_text, self._input_cursor
                self._edit_input(text[:cursor] + text[cursor + 1 :], cursor)
            elif key_name == "KEY_BACKSPACE" or key == "\x7f" or key == "\x08":
                text, cursor = self._input_text, self._input_cursor
                if cursor:
                    self._edit_input(text[: cursor - 1] + text[cursor:], cursor - 1)
            elif not getattr(key, "is_sequence", False) and len(key) == 1 and 32 <= ord(key) <= 126:
                text, cursor = self._input_text, self._input_cursor
                self._edit_input(text[:cursor] + str(key) + text[cursor:], cursor + 1)
            return

        if self._show_help:
            if key in ("?", "\x1b") or key_name == "KEY_ESCAPE":
                self._show_help = False
            elif key in ("q", "Q"):
                self._running = False
            elif key == "V":
                # The help lists this key and shows its state, and the movement keys it documents change
                # with it, so it must work here and the screen must update.
                self._toggle_vim_mode()
            else:
                self._scroll_help(key, key_name)
            return

        if self._show_settings:
            self._dispatch_settings_key(key, key_name)
            return

        # Player navigation shortcuts
        if key in ("q", "Q"):
            self._running = False
        elif key == "?":
            self._show_help = True
            self._help_scroll = 0
        elif key == ",":
            self._show_settings = True
            self._help_scroll = 0
            self._settings_msg = ""
        elif key == " ":
            self.set_paused(not self._is_paused)
        elif key in ("n", "N"):
            self.next_track()
        elif key in ("p", "P"):
            self.previous_track()
        elif key in ("s", "S"):
            self.set_shuffle(not self.queue.shuffle_enabled)
        elif key in ("r", "R"):
            self._cycle_repeat()
        elif (self._vim_mode and key == "h") or (
            not self._vim_mode and key_name == "KEY_LEFT"
        ):
            self.seek_relative(-5)
        elif (self._vim_mode and key == "l") or (
            not self._vim_mode and key_name == "KEY_RIGHT"
        ):
            self.seek_relative(5)
        elif key == "[":
            self.seek_relative(-30)
        elif key == "]":
            self.seek_relative(30)
        elif key in ("+", "="):
            self.set_volume(self._volume + 5)
        elif key in ("-", "_"):
            self.set_volume(self._volume - 5)
        elif key in ("m", "M"):
            self.set_mute(not self._is_muted)
        elif key == "v":
            self._toggle_visualizer()
        elif key == "V":
            self._toggle_vim_mode()
        elif key in ("a", "A"):
            self._cycle_visualizer_style()
        elif key in ("t", "T"):
            self._show_preview = not self._show_preview
            if not self._show_preview:
                self.previewer.clear()
            print(self.term.clear, end="", flush=True)
        elif key in ("/", "f", "F"):
            self._input_mode = "search"
            self._input_buffer = ""
            self._search_origin = (self._selected_list_idx, self._scroll_offset)
        elif key in ("o", "O"):
            self._input_mode = "url"
            self._input_buffer = ""
            self._input_error = ""
            self._open_saved = list_saved_playlists()
            self._open_idx = self._open_scroll = 0
            if any(not item.titled for item in self._open_saved) and not (self._title_task and not self._title_task.done()):
                self._title_task = asyncio.create_task(self._fill_missing_titles(list(self._open_saved)))
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

    @property
    def _input_buffer(self) -> str:
        return self._input_text

    @_input_buffer.setter
    def _input_buffer(self, value: str) -> None:
        """Replace the prompt text; the cursor goes to the end, where typing continues."""
        self._input_text = value
        self._input_cursor = len(value)

    def _edit_input(self, text: str, cursor: int) -> None:
        """Change the prompt text in place, keeping the live search results in step."""
        self._input_text = text
        self._input_cursor = cursor
        self._input_error = ""
        if self._input_mode == "search":
            self.queue.set_filter(text)
            self._selected_list_idx = 0
            self._scroll_offset = 0

    def _prompt_segments(self, label: str, suffix: str = "", width: Optional[int] = None) -> List[Tuple[Any, str]]:
        """The prompt line with a reverse-video cursor at the insertion point.

        When the text is wider than ``width`` it scrolls so the cursor stays visible.
        """
        term = self.term
        prefix = f" {label}"
        text, cursor = self._input_text, self._input_cursor
        start = 0
        if width is not None:
            room = max(1, width - len(prefix))  # cells for the text and the cursor
            start = max(0, cursor - room + 1)
            text = text[start : start + room]
        local = cursor - start
        return [
            (term.bold_yellow, prefix + text[:local]),
            (term.bold_yellow_reverse, text[local : local + 1] or " "),
            (term.bold_yellow, text[local + 1 :] + suffix),
        ]

    async def _fill_missing_titles(self, saved: List[SavedPlaylist]) -> None:
        """Look up the title of every saved playlist that has none, one at a time, and keep it for good."""
        loop = asyncio.get_running_loop()
        for item in saved:
            if item.titled:
                continue
            try:
                title = await loop.run_in_executor(None, fetch_playlist_title, item.url)
            except PlaylistFetchError as err:
                logger.warning("Unable to look up the title of %s: %s", item.url, err)
                continue
            except Exception:
                logger.exception("Unexpected failure looking up the title of %s", item.url)
                continue
            if title and set_playlist_title(item.url, title):
                item.name, item.titled = title, True
                self._open_titles += 1
                self._render_requested.set()

    TILE_WIDTH = 22  # a thumbnail tile: the image is this many cells wide...
    TILE_IMAGE_ROWS = 6  # ...and this many tall (about 16:9), with the name on the row below and a blank one after
    TILE_GAP = 2

    def _open_grid(self) -> Optional[Tuple[int, int]]:
        """(columns, visible rows) of the thumbnail grid, or None when the list view is used.

        The list is the fallback whenever thumbnails cannot be drawn (no Kitty, preview turned off) or
        the terminal is too small to show at least two columns and two rows of tiles.
        """
        if self._open_view != "thumbnails" or not self.previewer.has_kitty or not self._show_preview:
            return None
        columns = min(4, (self.term.width - 3) // (self.TILE_WIDTH + self.TILE_GAP))
        rows = (self.term.height - self.OPEN_HEADER_ROWS - 1) // (self.TILE_IMAGE_ROWS + 2)
        return (columns, rows) if columns >= 2 and rows >= 2 else None

    def _open_columns(self) -> int:
        """Saved playlists per row: one for the list view."""
        grid = self._open_grid()
        return grid[0] if grid else 1

    def _open_visible_rows(self, height: int) -> int:
        """Saved-playlist rows that fit under the dialog header and above its hint."""
        grid = self._open_grid()
        return grid[1] if grid else max(1, height - self.OPEN_HEADER_ROWS - 1)

    def _move_open_selection(self, step: int) -> None:
        """Move the highlighted saved playlist by ``step`` items, stopping at either end."""
        if not self._open_saved:
            return
        self._open_idx = max(0, min(len(self._open_saved) - 1, self._open_idx + step))
        columns = self._open_columns()
        row = self._open_idx // columns
        visible = self._open_visible_rows(self.term.height)
        first = self._open_scroll // columns
        if row < first:
            first = row
        elif row >= first + visible:
            first = row - visible + 1
        self._open_scroll = first * columns

    OPEN_HEADER_ROWS = 6  # prompt, hint, error or blank, blank, "Saved playlists:", blank

    def _open_lines(self, width: int, height: int) -> Tuple[List[str], List[Tuple[Any, int, int, int, int]]]:
        """The open dialog: the URL prompt, then the saved playlists as a list or a grid of thumbnails.

        Returns the text lines and the thumbnails to draw over them as (track, x, y, width, height).
        """
        term = self.term
        typing = bool(self._input_text)
        hide = [(term.dim, " press ")] + self._keycap_segments("Esc" if typing else "o", "yellow") + [(term.dim, " to hide")]
        lines = [
            self._paint(self._prompt_segments("open: ", "_" * width, width), width),
            self._paint(hide, width),
            self._paint([(term.bold_red, f" ✗ {self._input_error}")], width) if self._input_error else "",
            "",
            self._paint([(term.bold_white, " Saved playlists:")], width),
            "",
        ]
        if not self._open_saved:
            lines.append(self._paint([(term.dim, " Nothing saved yet: playlists you open are kept here.")], width))
            return lines, []
        visible = self._open_visible_rows(height)
        grid = self._open_grid()
        if grid:
            tiles = self._open_tile_lines(width, grid[0], visible, typing)
            lines += tiles[0]
            lines += [""] * max(0, height - 1 - len(lines))
            lines.append(self._paint([(term.dim, " arrows or Tab choose · Enter opens · Esc closes")], width))
            return lines, tiles[1]
        for index in range(self._open_scroll, min(len(self._open_saved), self._open_scroll + visible)):
            item = self._open_saved[index]
            selected = index == self._open_idx and not typing  # typed text is what Enter opens
            lines.append(
                self._paint(
                    [
                        (term.bold_yellow, " ➤ " if selected else "   "),
                        (term.bold_reverse if selected else term.bold, item.name),
                        (term.dim, f" - {item.url}"),
                    ],
                    width,
                )
            )
        lines += [""] * max(0, height - 1 - len(lines))
        lines.append(self._paint([(term.dim, " ↑↓ choose · Enter opens · Esc closes")], width))
        return lines, []

    def _open_tile_lines(
        self, width: int, columns: int, visible_rows: int, typing: bool
    ) -> Tuple[List[str], List[Tuple[Any, int, int, int, int]]]:
        """Grid rows: blank lines for each thumbnail to be drawn over, then the names; plus the placements."""
        term = self.term
        lines: List[str] = []
        placements: List[Tuple[Any, int, int, int, int]] = []
        pitch = self.TILE_WIDTH + self.TILE_GAP
        first = self._open_scroll
        for row in range(visible_rows):
            names: List[Tuple[Any, str]] = []
            for column in range(columns):
                index = first + row * columns + column
                if index >= len(self._open_saved):
                    break
                item = self._open_saved[index]
                selected = index == self._open_idx and not typing
                self.previewer.request(item.cover)
                placements.append(
                    (item.cover, 2 + column * pitch, self.OPEN_HEADER_ROWS + row * (self.TILE_IMAGE_ROWS + 2),
                     self.TILE_WIDTH, self.TILE_IMAGE_ROWS)
                )
                name = ("➤ " if selected else "  ") + item.name
                names.append((term.bold_yellow_reverse if selected else term.bold, name[: self.TILE_WIDTH].ljust(pitch)))
            if not names:
                break
            lines += [""] * self.TILE_IMAGE_ROWS
            lines.append(self._paint(names, width))
            lines.append("")
        return lines, placements

    def _handle_mouse(self, key: Any) -> None:
        """Handle progress-bar seeking, summary icons, track-list clicks, and wheel navigation."""
        try:
            mouse_x, mouse_y = key.mouse_xy  # (x, y) — 0-indexed
        except (AttributeError, TypeError, ValueError):
            logger.debug("Ignoring malformed mouse event", exc_info=True)
            return

        mouse_name = getattr(key, "name", "")
        if mouse_name == "MOUSE_LEFT_RELEASED":
            self._scrubbing = False
            return
        if mouse_name == "MOUSE_LEFT_MOTION":
            if self._scrubbing:
                self._seek_to_column(mouse_x)
            return

        is_left_click = mouse_name == "MOUSE_LEFT" or bool(getattr(key, "is_mouse_left", lambda: False)())
        if is_left_click:
            self._scrubbing = False
            if self._timeline_hit:
                bar_row, bar_x, bar_width = self._timeline_hit
                if mouse_y == bar_row and bar_x <= mouse_x < bar_x + bar_width:
                    self._scrubbing = True
                    self._seek_to_column(mouse_x)
                    return
            if mouse_y == 0:
                for start, end, action in self._summary_hits:
                    if start <= mouse_x < end:
                        self._activate_summary(action)
                        return

        # Check if the event landed in the track list area
        if self._list_height <= 0:
            return
        list_end_row = self._list_top_row + self._list_height
        if not (self._list_top_row <= mouse_y < list_end_row):
            return

        if mouse_name in {"MOUSE_SCROLL_UP", "MOUSE_WHEEL_UP"}:
            self._move_list_selection(-1)
            return
        if mouse_name in {"MOUSE_SCROLL_DOWN", "MOUSE_WHEEL_DOWN"}:
            self._move_list_selection(1)
            return
        if not is_left_click:
            return

        # Map screen row to filtered list index
        clicked_item_idx = self._scroll_offset + (mouse_y - self._list_top_row)
        filtered = self.queue.filtered_tracks
        if 0 <= clicked_item_idx < len(filtered):
            orig_idx = filtered[clicked_item_idx][0]
            self._selected_list_idx = clicked_item_idx
            self._play_index(orig_idx)

    def _exit_search(self, played_idx: Optional[int] = None) -> None:
        """Leave search and return to the full playlist.

        Search never changes the main list: cancelling restores the selection and scroll
        position from before searching, and playing a match selects that track in the full list.
        """
        self.queue.clear_filter()
        if played_idx is None:
            self._selected_list_idx, self._scroll_offset = self._search_origin
        else:
            self._selected_list_idx = played_idx
            self._scroll_offset = max(0, played_idx - max(1, self._list_height) // 2)
        self._selected_list_idx = max(0, min(self._selected_list_idx, len(self.queue.tracks) - 1))
        self._move_list_selection(0)

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

    def _render_braille_lines(self, bands: List[float], height: int, width: int) -> List[str]:
        """Braille spectrum, vectorized: the per-dot Python loop was the slowest style by far."""
        all_bands = np.asarray(bands, dtype=np.float64)
        columns = np.arange(width) * 2
        left = all_bands[np.minimum(len(all_bands) - 1, columns)]
        right = all_bands[np.minimum(len(all_bands) - 1, columns + 1)]
        dot_levels = height * 4
        # thresholds[row, dot]: a dot is lit when the band reaches its level.
        thresholds = dot_levels - np.arange(dot_levels).reshape(height, 4)
        weights_left = np.asarray(self.BRAILLE_LEFT).reshape(1, 4, 1)
        weights_right = np.asarray(self.BRAILLE_RIGHT).reshape(1, 4, 1)
        lit_left = left[None, None, :] * dot_levels >= thresholds[:, :, None]
        lit_right = right[None, None, :] * dot_levels >= thresholds[:, :, None]
        masks = (lit_left * weights_left).sum(axis=1) + (lit_right * weights_right).sum(axis=1)
        grid = np.ascontiguousarray(self.BRAILLE_TABLE[masks])
        return [row.tobytes().decode("utf-32-le") for row in grid]

    def _render_spectrum_lines(self, bands: List[float], height: int, width: int) -> List[str]:
        self._cell_levels = None
        if self._spectrum_style == "waterfall":
            if self.spectrum.enabled:
                self._spectrum_history.append(bands)
            elif self._spectrum_mode != "freeze":
                self._spectrum_history.clear()
            self._spectrum_history = self._spectrum_history[-height:]
            padding = [[0.0] * width] * max(0, height - len(self._spectrum_history))
            rows = padding + self._spectrum_history
            self._cell_levels = np.zeros((height, width))
            for index, values in enumerate(rows):
                self._cell_levels[index, : len(values)] = values[:width]
            return [
                "".join(self.SPECTRUM_CHARS[min(len(self.SPECTRUM_CHARS) - 1, int(value * (len(self.SPECTRUM_CHARS) - 1)))] for value in row)
                for row in rows
            ]

        self._spectrum_history.clear()
        if self._spectrum_style == "braille":
            return self._render_braille_lines(bands, height, width)

        if self._spectrum_style == "galaxy":
            return self._render_galaxy_lines(bands, height, width)
        top = self._bar_position == "top"
        if self._spectrum_style == "thin":
            return self._render_bar_lines(bands, height, width, top, 2, "┃", "╻" if top else "╹")
        if self._spectrum_style == "fewbars":
            count = max(1, (width + 1) // 6)
            groups = [float(np.mean(group)) for group in np.array_split(np.asarray(bands, dtype=np.float64), count)]
            expanded = [value for group in groups for value in [group] * 5 + [-1.0]]
            return self._render_bar_lines(expanded, height, width, top, 1, "█", "▀" if top else "▄")
        if self._spectrum_style == "rainbow":
            left_width = width // 2
            bands = list(bands[:left_width][::-1]) + list(bands[: width - left_width])  # bass meets in the middle

        if top and self._spectrum_style in ("bars", "rainbow"):
            return self._render_bar_lines(bands, height, width, True, 1, "█", "▀")

        lines = [[" "] * width for _ in range(height)]
        if self._spectrum_style == "waveform":
            self._cell_levels = np.tile(np.pad(np.asarray(bands[:width], dtype=np.float64), (0, max(0, width - len(bands)))), (height, 1))
            for col, value in enumerate(bands[:width]):
                row = height - 1 - min(height - 1, int(value * height))
                lines[row][col] = "•"
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

    @staticmethod
    def _render_bar_lines(
        bands: List[float], height: int, width: int, top: bool, spacing: int, full: str, cap: str
    ) -> List[str]:
        """Bars in half-cell steps, growing from the top or bottom edge; a negative value is a gap column."""
        lines = [[" "] * width for _ in range(height)]
        for col in range(0, min(width, len(bands) * spacing), spacing):
            value = bands[col // spacing]
            if value < 0:
                continue
            level = int(min(1.0, value) * height * 2)
            for step in range(min(height, level // 2 + level % 2)):
                char = full if step < level // 2 else cap
                lines[step if top else height - 1 - step][col] = char
        return ["".join(line) for line in lines]

    def _render_galaxy_lines(self, bands: List[float], height: int, width: int) -> List[str]:
        """A glowing ring whose radius follows the spectrum, drawn in braille dots and slowly turning."""
        pixels_w, pixels_h = width * 2, height * 4
        values = np.asarray(bands, dtype=np.float64)
        if values.size == 0:
            values = np.zeros(1)
        dots = np.zeros((pixels_h, pixels_w), dtype=bool)
        center_x, center_y = (pixels_w - 1) / 2, (pixels_h - 1) / 2
        # An ellipse matched to the box: at full level the ring touches its edges and cannot leave it.
        theta = np.linspace(0, 2 * np.pi, max(720, 4 * (pixels_w + pixels_h)), endpoint=False)
        turn = (time.time() * 0.15) % 1.0  # fraction of a revolution
        # Mirror the spectrum around the circle so the ring closes without a seam.
        position = (np.abs(((theta / (2 * np.pi) + turn) % 1.0) * 2 - 1) * (values.size - 1)).astype(int)
        level = values[np.clip(position, 0, values.size - 1)]
        for scale, spin in ((1.0, 0.0), (0.55, 0.5)):
            radius = scale * (0.3 + 0.7 * level)
            xs = np.rint(center_x + center_x * radius * np.cos(theta + spin)).astype(int)
            ys = np.rint(center_y + center_y * radius * np.sin(theta + spin)).astype(int)
            keep = (xs >= 0) & (xs < pixels_w) & (ys >= 0) & (ys < pixels_h)
            dots[ys[keep], xs[keep]] = True
        dots[int(center_y) : int(center_y) + 2, int(center_x) : int(center_x) + 2] = True  # the core
        # Intensity of a cell is how far out the ring was pushed, i.e. how loud that part of the spectrum is.
        offset_x = (np.arange(pixels_w)[None, :] - center_x) / max(1.0, center_x)
        offset_y = (np.arange(pixels_h)[:, None] - center_y) / max(1.0, center_y)
        distance = np.minimum(1.0, np.hypot(offset_x, offset_y))
        self._cell_levels = distance.reshape(height, 4, width, 2).mean(axis=(1, 3))
        codes = np.zeros((height, width), dtype=np.int64)
        for dy in range(4):
            codes += dots[dy::4, 0::2] * self.BRAILLE_LEFT[dy]
            codes += dots[dy::4, 1::2] * self.BRAILLE_RIGHT[dy]
        return ["".join(row) for row in self.BRAILLE_TABLE[codes]]

    def _band_count(self, width: int) -> int:
        """How many spectrum bands the current style wants for ``width`` columns."""
        if self._spectrum_style == "braille":
            return width * 2
        if self._spectrum_style == "galaxy":
            return 64
        if self._spectrum_style == "thin":
            return (width + 1) // 2
        return width

    def _palette_style(self, fraction: float) -> Any:
        """Style for a 0..1 position along the rainbow colours."""
        count = len(self._rainbow_colors)
        name = self._rainbow_colors[min(count - 1, max(0, int(fraction * count)))]
        return getattr(self.term, f"bold_{name}")

    def _styled_row(self, row: int, height: int, line: str) -> str:
        """Colour one visualizer row: bold cyan, or in rainbow mode by position along the colours.

        Bar styles colour by the row's height from the edge the bars grow from, so quiet parts stay in
        the first colour and only loud peaks reach the last. The other styles colour each cell by its
        own intensity (waterfall brightness, the waveform's level, how far the galaxy ring is pushed out);
        braille colours by row like the bars.
        """
        if self._spectrum_style != "rainbow" and self._color_mode != "rainbow":
            return str(self.term.bold_cyan(line))
        levels = self._cell_levels
        if levels is None or row >= len(levels):
            anchored_top = self._bar_position == "top"
            return str(self._palette_style((row if anchored_top else height - 1 - row) / max(1, height - 1))(line))
        segments: List[Tuple[Any, str]] = []
        for col, char in enumerate(line):
            blank = char in (" ", "\u2800")
            style = None if blank else self._palette_style(float(levels[row, col]) if col < levels.shape[1] else 0.0)
            if segments and segments[-1][0] is style:
                segments[-1] = (style, segments[-1][1] + char)
            else:
                segments.append((style, char))
        return self._paint(segments)

    def _visualizer_animating(self) -> bool:
        """True while the spectrum is live; a frozen or cleared one is static."""
        return self._show_visualizer and not self._show_help and not self._show_settings and bool(self.spectrum.enabled)

    async def _render_loop(self) -> None:
        while self._running:
            signature = self._render_signature()
            if signature != self._last_render_signature or self._visualizer_animating():
                self._render()
                self._last_render_signature = signature
            self._render_requested.clear()
            # Coalesce bursts of input (e.g. a scroll-wheel flood) into one redraw per interval.
            await asyncio.sleep(self.MIN_RENDER_INTERVAL)
            try:
                await asyncio.wait_for(
                    self._render_requested.wait(),
                    timeout=max(0.0, self._render_delay() - self.MIN_RENDER_INTERVAL),
                )
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
            self.queue.revision,
            self._selected_list_idx,
            self._scroll_offset,
            self._input_mode,
            self._input_buffer,
            self._input_cursor,
            self._input_error,
            self._open_idx,
            self._open_scroll,
            self._open_titles,
            self._is_paused,
            self._is_muted,
            self._is_loading_playlist,
            self._is_buffering,
            self._mpv_idle,
            self._show_visualizer,
            self._spectrum_style,
            self._bar_position,
            self._color_mode,
            self._rainbow_colors,
            self._show_preview,
            self._vim_mode,
            self._show_help,
            self._show_settings,
            self._settings_msg,
            self._settings_idx,
            self._help_scroll,
            self._volume,
            self.queue.repeat_mode,
            self.queue.shuffle_enabled,
            self._status_msg,
            int(self._time_pos),
            int(self._duration),
            self.previewer.generation,
            animated_frame,
        )

    def _toggle_visualizer(self) -> None:
        self._show_visualizer = not self._show_visualizer
        self._spectrum_mode = None
        save_visualizer_preferences(self._spectrum_style, self._show_visualizer, self.config_file)
        if self._show_visualizer:
            self.spectrum.resume()
        else:
            self.spectrum.pause()
            self.previewer.clear()
        print(self.term.clear, end="", flush=True)

    def _cycle_visualizer_style(self) -> None:
        style_index = self.SPECTRUM_STYLES.index(self._spectrum_style)
        self._spectrum_style = self.SPECTRUM_STYLES[(style_index + 1) % len(self.SPECTRUM_STYLES)]
        save_visualizer_preferences(self._spectrum_style, self._show_visualizer, self.config_file)
        self._spectrum_history.clear()

    def _dispatch_settings_key(self, key: Any, key_name: str) -> None:
        """Settings screen: arrows move, Enter or Space changes the row; v, a and V work as on the dashboard."""
        if key in (",", "\x1b") or key_name == "KEY_ESCAPE":
            self._show_settings = False
        elif key in ("q", "Q"):
            self._running = False
        elif key_name in ("KEY_UP", "KEY_DOWN"):
            step = -1 if key_name == "KEY_UP" else 1
            self._settings_idx = (self._settings_idx + step) % len(self.SETTINGS_ROWS)
            self._settings_msg = ""
        elif key_name == "KEY_ENTER" or key in ("\n", "\r", " "):
            self._change_setting(self.SETTINGS_ROWS[self._settings_idx][3])
        elif key == "v":
            self._toggle_visualizer()
        elif key in ("a", "A"):
            self._cycle_visualizer_style()
        elif key == "V":
            self._toggle_vim_mode()

    def _change_setting(self, action: str) -> None:
        """Toggle or cycle a setting at once and save it, or open the prompt for a typed one."""
        self._settings_msg = ""
        if action == "visualizer":
            self._toggle_visualizer()
        elif action == "style":
            self._cycle_visualizer_style()
        elif action == "vim":
            self._toggle_vim_mode()
        elif action == "bar":
            index = BAR_POSITIONS.index(self._bar_position)
            self._bar_position = BAR_POSITIONS[(index + 1) % len(BAR_POSITIONS)]
            save_bar_position(self._bar_position, self.config_file)
            self._spectrum_history.clear()
        elif action == "open_view":
            self._open_view = OPEN_VIEWS[(OPEN_VIEWS.index(self._open_view) + 1) % len(OPEN_VIEWS)]
            save_open_view(self._open_view, self.config_file)
        elif action == "rainbow":
            self._color_mode = COLOR_MODES[(COLOR_MODES.index(self._color_mode) + 1) % len(COLOR_MODES)]
            save_color_mode(self._color_mode, self.config_file)
        elif action == "rainbow_colors":
            self._input_mode = "rainbow"
            self._input_buffer = ", ".join(self._rainbow_colors)
        elif action == "playlist":
            self._input_mode = "playlist"
            self._input_buffer = load_default_playlist(self.config_file)
        elif action == "clients":
            self._input_mode = "clients"
            self._input_buffer = ", ".join(self._ytdl_fallback_clients)

    def _apply_setting_input(self, mode: str, text: str) -> None:
        """Validate and save a typed setting, reporting the outcome on the settings screen."""
        text = text.strip()
        if mode == "playlist":
            try:
                url = validate_media_url(text)
            except InvalidMediaURL as err:
                self._settings_msg = str(err)
                return
            self.set_playback_config(default_playlist=url)  # the background player saves it and reports a failure
            self._settings_msg = "Default playlist saved." if self._connected else "Not connected to the player."
        elif mode == "rainbow":
            colors = parse_rainbow_colors(text)
            if colors is None:
                self._settings_msg = "Give at least two terminal colours, e.g. blue, cyan, red or bright_red."
                return
            if save_rainbow_colors(colors, self.config_file):
                self._rainbow_colors = colors
                self._settings_msg = "Rainbow colours saved."
            else:
                self._settings_msg = "Could not save the configuration."
        else:
            clients = parse_ytdl_fallback_clients(text)
            if clients is None:
                self._settings_msg = "Client names may only use letters, digits and underscores."
                return
            self.set_playback_config(fallback_clients=list(clients))
            self._ytdl_fallback_clients = clients
            self._settings_msg = "Fallback clients saved." if self._connected else "Not connected to the player."

    def _toggle_vim_mode(self) -> None:
        self._vim_mode = not self._vim_mode
        save_vim_mode(self._vim_mode, self.config_file)

    def _keycap(self, key: str, color: Optional[str] = "cyan") -> str:
        """A key drawn as its own box: dim brackets around a bold coloured key, e.g. [Space] or [n]."""
        return self._paint(self._keycap_segments(key, color))

    def _keycap_segments(self, key: str, color: Optional[str] = "cyan") -> List[Tuple[Any, str]]:
        """Segments of a keycap; ``color`` None means bold in the terminal's own default colour."""
        term = self.term
        return [(term.dim, "["), (getattr(term, f"bold_{color}") if color else term.bold, key), (term.dim, "]")]

    @staticmethod
    def _paint(segments: List[Tuple[Any, str]], limit: Optional[int] = None) -> str:
        """Join (style, text) segments, cutting to ``limit`` visible characters *before* styling.

        blessed's Terminal.truncate() must not be used on styled text: it collapses the line into
        its first style and drops every later colour. Cutting the plain text first keeps each
        segment's own style, and never leaves half an escape sequence.
        """
        out: List[str] = []
        for style, text in segments:
            if limit is not None:
                text = text[: max(0, limit)]
                limit -= len(text)
            if text:
                out.append(style(text) if style else text)
            if limit is not None and limit <= 0:
                break
        return "".join(out)

    # Settings rows as (section, label, dashboard key or "", action). Only keys that also work on the
    # dashboard are shown; every other row is reached with the arrows and changed with Enter or Space.
    SETTINGS_ROWS = (
        ("Display", "Show visualizer", "v", "visualizer"),
        ("Display", "Visualizer style", "a", "style"),
        ("Display", "Bar position", "", "bar"),
        ("Display", "Rainbow colouring", "", "rainbow"),
        ("Display", "Rainbow colours", "", "rainbow_colors"),
        ("General", "Vim keys", "V", "vim"),
        ("Library", "Open dialog view", "", "open_view"),
        ("Library", "Default playlist", "", "playlist"),
        ("Playback", "YouTube fallback clients", "", "clients"),
    )
    HELP_COLORS = {"Playback": "cyan", "Library": "green", "Display": "magenta", "General": "yellow"}

    def _help_sections(self) -> List[Tuple[str, List[Tuple[Tuple[str, ...], str, Optional[Tuple[str, str]]]]]]:
        """Help content as (title, [(keys, description, note)]); a note is (text, colour) shown as "(now text)".

        Vim mode swaps the movement keys.
        """
        vim = self._vim_mode
        return [
            (
                "Playback",
                [
                    (("Space",), "Play or pause", None),
                    (("n", "p"), "Next or previous track", None),
                    (("s",), "Toggle shuffle", None),
                    (("r",), "Cycle repeat mode", None),
                    (("m",), "Mute", None),
                    (("+", "-"), "Change volume", None),
                    ((("h", "l") if vim else ("←", "→")), "Seek 5 seconds", None),
                    (("[", "]"), "Seek 30 seconds", None),
                ],
            ),
            (
                "Library",
                [
                    ((("j", "k") if vim else ("↑", "↓")), "Browse tracks", None),
                    (("Enter",), "Play selected track", None),
                    (("/",), "Search tracks", None),
                    (("o",), "Open a YouTube URL", None),
                    (("Click",), "Progress bar seeks, repeat/shuffle icons toggle", None),
                ],
            ),
            (
                "Display",
                [
                    (("v",), "Toggle visualizer", None),
                    (("a",), "Change visualizer style", None),
                    (("t",), "Toggle thumbnail preview", None),
                    (("V",), "Toggle Vim keys", ("on", "green") if vim else ("off", "red")),
                    ((",",), "Open settings", None),
                ],
            ),
            (
                "General",
                [
                    (("?", "Esc"), "Close help", None),
                    (("q",), "Close (music keeps playing)", None),
                ],
            ),
        ]

    @staticmethod
    def _on_off(value: bool) -> Tuple[str, str]:
        return ("on", "green") if value else ("off", "red")

    def _setting_value(self, action: str) -> Tuple[str, str]:
        """Current value of a settings row and the colour to show it in."""
        if action == "playlist":
            playlist = load_default_playlist(self.config_file)
            return (playlist[:40] + "…" if len(playlist) > 40 else playlist or "none"), "cyan"
        return {
            "visualizer": self._on_off(self._show_visualizer),
            "style": (self._spectrum_style, "cyan"),
            "bar": (self._bar_position, "cyan"),
            "open_view": (self._open_view, "cyan"),
            "rainbow": self._on_off(self._color_mode == "rainbow"),
            "rainbow_colors": (", ".join(self._rainbow_colors), "cyan"),
            "vim": self._on_off(self._vim_mode),
            "clients": (", ".join(self._ytdl_fallback_clients) or "none", "cyan"),
        }[action]

    def _help_lines(self, width: Optional[int] = None) -> List[str]:
        """Styled help screen lines, each cut to ``width`` visible characters when given."""
        return self._screen_lines(" MPF HELP ", self._help_sections(), "?", width)

    def _settings_lines(self, width: Optional[int] = None) -> List[str]:
        """Settings screen: one row per option with a cursor on the selected one, then the prompt or last result."""
        term = self.term
        label_width = max(len(label) for _section, label, _key, _action in self.SETTINGS_ROWS)
        lines = [
            self._paint(
                [(None, " "), (term.bold_reverse, " MPF SETTINGS "), (term.dim, "  press ")]
                + self._keycap_segments(",", None)
                + [(term.dim, " to close")],
                width,
            )
        ]
        section = ""
        for index, (row_section, label, key, action) in enumerate(self.SETTINGS_ROWS):
            color = self.HELP_COLORS.get(row_section, "cyan")
            if row_section != section:
                section = row_section
                dashes = "─" * max(0, 12 - len(section))
                lines.append("")
                lines.append(
                    self._paint(
                        [(None, " "), (getattr(term, f"bold_{color}"), section), (None, " "), (term.dim, dashes)], width
                    )
                )
            selected = index == self._settings_idx
            if selected:
                self._settings_focus = len(lines)
            value, value_color = self._setting_value(action)
            segments: List[Tuple[Any, str]] = [(getattr(term, f"bold_{color}"), " ›  " if selected else "    ")]
            segments += self._keycap_segments(key, color) if key else [(None, "   ")]
            segments += [
                (None, "  "),
                (term.bold if selected else None, label.ljust(label_width)),
                (None, "  "),
                (getattr(term, f"bold_{value_color}"), value),
            ]
            lines.append(self._paint(segments, width))
        lines.append("")
        footer_start = len(lines) - 1
        if self._input_mode in ("playlist", "clients", "rainbow"):
            label = {
                "playlist": "default playlist ",
                "clients": "clients (comma separated) ",
                "rainbow": "colours, bottom to top (comma separated) ",
            }[self._input_mode]
            lines.append(self._paint(self._prompt_segments(label, width=width), width))
            lines.append(self._paint([(term.dim, " Enter saves · Esc cancels")], width))
        else:
            if self._settings_msg:
                lines.append(self._paint([(term.dim, f" {self._settings_msg}")], width))
            lines.append(self._paint([(term.dim, " ↑↓ move · Enter or Space change · Esc close")], width))
        self._settings_footer = len(lines) - footer_start
        return lines

    def _screen_lines(
        self,
        title_text: str,
        sections: List[Tuple[str, List[Tuple[Tuple[str, ...], str, Optional[Tuple[str, str]]]]]],
        close_key: str,
        width: Optional[int],
    ) -> List[str]:
        """Lines of a help-style screen, each cut to ``width`` visible characters when given.

        Keys sit in boxes, each section has its own colour for its title and keys, and descriptions
        share one aligned column.
        """
        term = self.term

        def plain_width(keys: Tuple[str, ...]) -> int:
            return sum(len(key) + 2 for key in keys) + len(keys) - 1  # "[k]" per key, one space between

        column = max(plain_width(keys) for _title, rows in sections for keys, _desc, _note in rows)
        rows_out: List[List[Tuple[Any, str]]] = [
            # Reverse video swaps the terminal's own foreground and background, so the title is
            # readable on any theme; fixed colour pairs (white on blue) are remapped by themes.
            [(None, " "), (term.bold_reverse, title_text), (term.dim, "  press ")]
            + self._keycap_segments(close_key, None)
            + [(term.dim, " to close")]
        ]
        for title, rows in sections:
            color = self.HELP_COLORS.get(title, "cyan")
            rows_out.append([])
            rows_out.append(
                [(None, " "), (getattr(term, f"bold_{color}"), title), (None, " "), (term.dim, "─" * max(0, 12 - len(title)))]
            )
            for keys, description, note in rows:
                line: List[Tuple[Any, str]] = [(None, "   ")]
                for index, key in enumerate(keys):
                    line += ([(None, " ")] if index else []) + self._keycap_segments(key, color)
                line += [(None, " " * (column - plain_width(keys) + 2)), (None, description)]  # default text colour
                if note:
                    line += [(term.dim, " (now "), (getattr(term, f"bold_{note[1]}"), note[0]), (term.dim, ")")]
                rows_out.append(line)
        return [self._paint(segments, width) for segments in rows_out]

    def _scroll_help(self, key: Any, key_name: str) -> None:
        """Scroll the help screen when it is taller than the terminal; the render clamps the offset."""
        page = max(1, self.term.height - 3)
        vim = self._vim_mode
        if key_name in ("KEY_UP", "MOUSE_SCROLL_UP", "MOUSE_WHEEL_UP") or (vim and key == "k"):
            self._help_scroll -= 1
        elif key_name in ("KEY_DOWN", "MOUSE_SCROLL_DOWN", "MOUSE_WHEEL_DOWN") or (vim and key == "j"):
            self._help_scroll += 1
        elif key_name == "KEY_PGUP":
            self._help_scroll -= page
        elif key_name == "KEY_PGDOWN" or key == " ":
            self._help_scroll += page
        elif key_name == "KEY_HOME":
            self._help_scroll = 0
        elif key_name == "KEY_END":
            self._help_scroll = 1 << 30

    def _fit_lines(
        self, lines: List[str], height: int, width: int, focus: Optional[int] = None, footer: int = 0
    ) -> List[str]:
        """Cut a screen taller than the terminal to a scrolling window.

        The title line stays on top and the last ``footer`` lines stay at the bottom; the lines between
        scroll, following ``focus`` (a line index that must stay visible) when given. A dim indicator
        shows that there is more.
        """
        if len(lines) <= height:
            self._help_scroll = 0
            return lines
        foot = lines[len(lines) - footer :] if footer else []
        body = lines[1 : len(lines) - footer]
        room = max(1, height - 1 - len(foot) - 1)  # minus the title, the footer and the indicator
        if focus is not None:
            focus -= 1  # body index
            if focus < self._help_scroll:
                self._help_scroll = focus
            elif focus >= self._help_scroll + room:
                self._help_scroll = focus - room + 1
        self._help_scroll = max(0, min(self._help_scroll, max(0, len(body) - room)))
        shown = body[self._help_scroll : self._help_scroll + room]
        more_up, more_down = self._help_scroll > 0, self._help_scroll + room < len(body)
        arrows = ("↑" if more_up else " ") + ("↓" if more_down else " ")
        text = f" {arrows} more" + (" · arrows or PgUp/PgDn scroll" if focus is None else "")
        indicator = self._paint([(self.term.dim, text)], width)
        return [lines[0]] + shown + [indicator] + foot

    def _render_help(self) -> None:
        """Render the help or settings screen without leaving the player."""
        term = self.term
        width = max(0, term.width - 3)
        output = [term.clear + term.home]
        placements: List[Tuple[Any, int, int, int, int]] = []
        if self._input_mode == "url":
            lines, placements = self._open_lines(width, term.height)
            # Repaint every row in place instead of clearing the screen, which could take the thumbnails
            # with it while the previewer still believes they are drawn.
            lines += [""] * (term.height - len(lines))
            output = [term.home]
        else:
            if self._show_settings:
                lines = self._fit_lines(
                    self._settings_lines(width), term.height, width, self._settings_focus, self._settings_footer
                )
            else:
                lines = self._fit_lines(self._help_lines(width), term.height, width)
        for row, line in enumerate(lines[: term.height]):
            # The dialog repaints over the dashboard, so its two margin columns are blanked too.
            margin = self._move(0, row) + "  " if self._input_mode == "url" else self._move(2, row)
            output.append(margin + line + term.clear_eol)
        print("".join(output), end="", flush=True)
        if placements:
            self.previewer.render_many(placements, fallback=False)

    @staticmethod
    def _progress_meter(width: int, progress: float) -> str:
        """Return a fixed-width dashboard progress meter."""
        width = max(1, width)
        marker = round(max(0.0, min(1.0, progress)) * (width - 1))
        return "━" * marker + "●" + "─" * (width - marker - 1)

    def _summary_segments(self) -> List[Tuple[str, str]]:
        """Dashboard summary pieces as (text, click action); an empty action is not clickable."""
        volume_icon = "\U000f075f" if self._is_muted else "\uf028"
        return [
            (f"{volume_icon} {self._volume}%", ""),
            (self.queue.repeat_mode.icon, "repeat"),
            (self.SHUFFLE_ICONS[self.queue.shuffle_enabled], "shuffle"),
            (f"\U000f0386 {len(self.queue.tracks)}", ""),
        ]

    def _dashboard_summary(self) -> str:
        return self.SUMMARY_SEPARATOR.join(text for text, _action in self._summary_segments())

    def _render(self) -> None:
        term = self.term
        h, w = term.height, term.width
        if (w, h) != self._last_render_size:
            self.previewer.clear()
            self._last_render_size = (w, h)
        if h < 10 or w < 30:
            self._list_height = 0
            self._timeline_hit = None
            self._summary_hits = []
            print(term.home + term.red("Terminal window too small!"), end="", flush=True)
            return

        if self._show_help or self._show_settings or self._input_mode == "url":
            if not self._help_visible:
                self._help_visible = True
                self.previewer.clear()
            self._render_help()
            return

        out: List[str] = [term.home]
        if self._help_visible:
            # Wipe leftover help text and any stale thumbnail before redrawing the dashboard.
            self._help_visible = False
            self.previewer.clear()
            out = [term.clear + term.home]
        row = 0

        # Animated Spinner
        spinner = SPINNER_FRAMES[int(time.time() * 10) % len(SPINNER_FRAMES)]

        # 1. Now playing: state icon and title, with the summary right-aligned
        curr_track = self.queue.current_track
        if self._is_loading_playlist or (self._is_buffering and curr_track):
            state = spinner
        elif self._is_paused:
            state = "Ⅱ"
        elif curr_track and not self._mpv_idle:
            state = "▶"
        else:
            state = "■"

        title = curr_track.title if curr_track else "Nothing playing"
        summary = self._dashboard_summary()
        left = f" {state}  {title}"
        summary_col = w - len(summary) - 1
        self._summary_hits = []
        if summary_col >= 24:
            header = term.bold_white(left[: summary_col - 2]) + self._move(summary_col, row) + term.cyan(summary)
            column = summary_col
            for text, action in self._summary_segments():
                if action:
                    self._summary_hits.append((column, column + len(text), action))
                column += len(text) + len(self.SUMMARY_SEPARATOR)
        else:
            header = term.bold_white(left[: w - 1])
        out.append(self._move(0, row) + header + term.clear_eol)
        row += 1

        detail = curr_track.uploader if curr_track and curr_track.uploader else self._status_msg
        out.append(self._move(0, row) + term.dim(f"    {detail}"[: w - 1]) + term.clear_eol)
        row += 1

        # 2. Timeline
        elapsed = format_time(self._time_pos)
        duration = format_time(self._duration)
        bar_width = max(5, w - len(elapsed) - len(duration) - 5)
        pct = (self._time_pos / self._duration) if self._duration > 0 else 0.0
        bar_str = self._progress_meter(bar_width, pct)
        self._timeline_hit = (row, len(elapsed) + 2, bar_width)
        out.append(
            self._move(0, row)
            + term.white(f" {elapsed} ")
            + term.green(bar_str)
            + term.white(f" {duration}")
            + term.clear_eol
        )
        row += 2

        # 3. Side-by-side artwork and spectrum
        preview_width = 16
        show_preview_box = self._show_preview and w >= 55 and h >= 16

        if self._show_visualizer and h >= 16:
            spec_height = min(8, h - row - 6)
            if spec_height >= 3:
                if show_preview_box:
                    for r_idx in range(spec_height):
                        out.append(self._move(2, row + r_idx) + " " * preview_width)
                    viz_start_col = preview_width + 4
                else:
                    viz_start_col = 2

                # Render Spectrum Analyzer
                viz_avail_width = w - viz_start_col - 2
                num_bands = self._band_count(viz_avail_width)
                bands, _peaks = self.spectrum.get_bands(num_bands)
                spectrum_lines = self._render_spectrum_lines(bands, spec_height, viz_avail_width)

                for r in range(spec_height):
                    current_screen_row = row + r
                    out.append(self._move(viz_start_col, current_screen_row) + self._styled_row(r, spec_height, spectrum_lines[r]) + term.clear_eol)

                # Render Kitty image overlay
                if show_preview_box:
                    self.previewer.render(curr_track, x=2, y=row, width=preview_width, height=spec_height)

                row += spec_height + 1
        else:
            self._spectrum_history.clear()
            self.previewer.clear()

        # 4. Input prompt
        if self._input_mode != "none":
            prompt_label = "/"
            suffix = ""
            if self._input_mode == "search" and self._input_buffer:
                suffix = f"   {len(self.queue.filtered_tracks)}/{len(self.queue.tracks)}"
            segments = self._prompt_segments(prompt_label, suffix, w - 1)
            out.append(self._move(0, row) + self._paint(segments, w - 1) + term.clear_eol)
            row += 1

        # 5. Queue (ranked by fuzzy score when query active), with a one-line footer
        filtered = self.queue.filtered_tracks
        list_height = max(1, h - row - 1)

        self._list_height = list_height
        self._move_list_selection(0)

        # Store for mouse hit-testing
        self._list_top_row = row

        for i in range(list_height):
            item_idx = self._scroll_offset + i
            current_screen_row = row + i
            if item_idx >= len(filtered):
                out.append(self._move(0, current_screen_row) + term.clear_eol)
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

            out.append(self._move(0, current_screen_row) + formatted + term.clear_eol)

        # Clear remaining unused rows below track list (above footer)
        footer_row = h - 1
        for r_clear in range(row + list_height, footer_row):
            out.append(self._move(0, r_clear) + term.clear_eol)

        footer = ""
        footer_width = 0
        for key, label in self.FOOTER_HINTS:
            plain = f"[{key}] {label}"
            separator = "  ·  " if footer else " "
            if footer_width + len(separator) + len(plain) > w - 1:
                break
            footer += term.dim(separator) + self._keycap(key) + term.dim(f" {label}")
            footer_width += len(separator) + len(plain)
        out.append(self._move(0, footer_row) + footer + term.clear_eol)

        out.append(term.hide_cursor)
        print("".join(out), end="", flush=True)
