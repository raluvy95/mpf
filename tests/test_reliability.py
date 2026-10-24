"""Regression tests for reliability and long-running resource behavior."""

import asyncio
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from blessed import Terminal
from blessed.keyboard import Keystroke

from mpf_core.cache import (
    PlaybackState,
    load_cached_playlist,
    load_playback_state,
    save_cached_playlist,
    save_playback_state,
)
from mpf_core.fetcher import InvalidMediaURL, PlaylistFetchError, validate_media_url
from mpf_core.models import RepeatMode, Track, TrackQueue
from mpf_core.player import BlessedMusicPlayer
from mpf_core.previewer import KittyPreviewer

PLAYLIST_URL = "https://www.youtube.com/playlist?list=reliability"


class TestURLValidation(unittest.TestCase):
    def test_accepts_playlist_video_and_short_urls(self):
        urls = [
            PLAYLIST_URL,
            "https://www.youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://www.youtube.com/shorts/abc123",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(validate_media_url(url), url)

    def test_rejects_non_youtube_and_credential_urls(self):
        for url in [
            "file:///tmp/song",
            "https://example.com/watch?v=x",
            "https://user@youtube.com/watch?v=x",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/playlist?foo=bar",
        ]:
            with self.subTest(url=url), self.assertRaises(InvalidMediaURL):
                validate_media_url(url)


class TestReliableCaching(unittest.TestCase):
    def test_corrupt_caches_are_ignored_and_replaced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            playlist_file = os.path.join(tmpdir, "playlists.bin")
            playback_file = os.path.join(tmpdir, "playback.json")
            with open(playlist_file, "wb") as fh:
                fh.write(b"not-zstandard")
            with open(playback_file, "w", encoding="utf-8") as fh:
                fh.write("{broken")

            self.assertIsNone(load_cached_playlist(PLAYLIST_URL, playlist_file))
            self.assertIsNone(load_playback_state(PLAYLIST_URL, playback_file))
            self.assertTrue(save_cached_playlist(PLAYLIST_URL, [Track("a", "A")], playlist_file))
            self.assertTrue(save_playback_state(PlaybackState(PLAYLIST_URL, "a", 0, 1.0), playback_file))
            self.assertEqual(load_cached_playlist(PLAYLIST_URL, playlist_file)[0].id, "a")
            self.assertEqual(load_playback_state(PLAYLIST_URL, playback_file).track_id, "a")

    def test_concurrent_playback_updates_do_not_lose_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "playback.json")
            states = [PlaybackState(f"{PLAYLIST_URL}{i}", str(i), i, float(i)) for i in range(12)]
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(lambda state: save_playback_state(state, cache_file), states))
            self.assertTrue(all(results))
            with open(cache_file, "r", encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), len(states))

    def test_interrupted_replace_keeps_previous_playlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "playlists.bin")
            self.assertTrue(save_cached_playlist(PLAYLIST_URL, [Track("old", "Old")], cache_file))
            with patch("mpf_core.cache.os.replace", side_effect=OSError("interrupted")):
                self.assertFalse(save_cached_playlist(PLAYLIST_URL, [Track("new", "New")], cache_file))
            self.assertEqual(load_cached_playlist(PLAYLIST_URL, cache_file)[0].id, "old")


class TestPlaylistRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_stale_refresh_preserves_active_track_identity(self):
        old = [Track("a", "A"), Track("b", "B"), Track("c", "C")]
        fresh = [Track("c", "C2"), Track("a", "A2"), Track("b", "B2")]
        saved = PlaybackState(PLAYLIST_URL, "b", 1, 42.0)
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        with (
            patch("mpf_core.player.load_cached_playlist", return_value=old),
            patch("mpf_core.player.load_playback_state", return_value=saved),
            patch("mpf_core.player.is_playlist_cache_stale", return_value=True),
            patch("mpf_core.player.fetch_playlist_tracks", return_value=fresh),
            patch("mpf_core.player.save_cached_playlist", return_value=True),
            patch("mpf_core.player.save_default_playlist", return_value=True),
        ):
            await player.load_playlist(PLAYLIST_URL)
        self.assertEqual(player.queue.current_track.id, "b")
        self.assertEqual(player.queue.current_idx, 2)
        self.assertEqual(player._selected_list_idx, 2)

    async def test_refresh_outage_keeps_cached_queue(self):
        old = [Track("a", "A"), Track("b", "B")]
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        with (
            patch("mpf_core.player.load_cached_playlist", return_value=old),
            patch("mpf_core.player.load_playback_state", return_value=None),
            patch("mpf_core.player.is_playlist_cache_stale", return_value=True),
            patch("mpf_core.player.fetch_playlist_tracks", side_effect=PlaylistFetchError("network down")),
            patch("mpf_core.player.save_default_playlist", return_value=True),
        ):
            await player.load_playlist(PLAYLIST_URL)
        self.assertEqual([track.id for track in player.queue.tracks], ["a", "b"])
        self.assertIn("using cached playlist", player._status_msg)

    async def test_failed_initial_load_does_not_replace_default_playlist(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        with (
            patch("mpf_core.player.load_cached_playlist", return_value=None),
            patch("mpf_core.player.is_playlist_cache_stale", return_value=True),
            patch("mpf_core.player.fetch_playlist_tracks", side_effect=PlaylistFetchError("network down")),
            patch("mpf_core.player.save_default_playlist", return_value=True) as save_default,
        ):
            await player.load_playlist(PLAYLIST_URL)
        save_default.assert_not_called()


class TestPlayerFailureHandling(unittest.TestCase):
    def setUp(self):
        self.player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.player.queue = TrackQueue([Track("a", "A"), Track("b", "B"), Track("c", "C")])
        self.player.mpv = MagicMock()
        self.player.previewer.request = MagicMock()

    def tearDown(self):
        self.player.previewer.close()

    def test_unavailable_tracks_advance_then_stop(self):
        self.player._handle_mpv_end_file({"reason": "error", "error": "unavailable"})
        self.assertEqual(self.player.queue.current_track.id, "b")
        self.player._handle_mpv_end_file({"reason": "error"})
        self.assertEqual(self.player.queue.current_track.id, "c")
        self.player._handle_mpv_end_file({"reason": "error"})
        self.assertTrue(self.player._is_paused)
        self.assertEqual(self.player._failed_track_ids, {"a", "b", "c"})

    def test_repeat_off_stops_at_natural_playlist_end(self):
        self.player.queue.current_idx = 2
        self.player.queue.repeat_mode = RepeatMode.OFF
        self.player._handle_mpv_end_file({"reason": "eof"})
        self.assertTrue(self.player._is_paused)
        self.assertEqual(self.player._status_msg, "Playlist finished.")

    def test_next_previous_and_volume_controls_drive_player_state(self):
        self.player._handle_key("n")
        self.assertEqual(self.player.queue.current_track.id, "b")
        self.player._handle_key("p")
        self.assertEqual(self.player.queue.current_track.id, "a")
        self.player._handle_key("+")
        self.assertEqual(self.player._volume, 100)
        self.assertEqual(self.player.mpv.volume, 100)
        self.player._volume = 0
        self.player._handle_key("-")
        self.assertEqual(self.player._volume, 0)

    def test_unnamed_key_does_not_interrupt_track_skipping(self):
        self.player._handle_key(Keystroke("x"))
        self.player._handle_key(Keystroke("n"))

        self.assertEqual(self.player.queue.current_track.id, "b")

    def test_playback_state_volume_is_clamped_to_supported_range(self):
        self.assertEqual(PlaybackState.from_dict({"volume": 150}).volume, 100)
        self.assertEqual(PlaybackState.from_dict({"volume": -10}).volume, 0)

    def test_playing_without_mpv_shows_installation_error(self):
        self.player.mpv = None
        self.player._play_index(0)
        self.assertIn("install mpv", self.player._status_msg)

    def test_resume_seek_waits_for_file_loaded(self):
        self.player._play_index(0, start_pos=42.0)

        self.player.mpv.play.assert_called_once_with("https://youtu.be/a")
        self.player.mpv.command.assert_not_called()

        self.player._handle_mpv_file_loaded({})

        self.player.mpv.command.assert_called_once_with("seek", 42.0, "absolute")
        self.assertEqual(self.player._time_pos, 42.0)

    def test_resume_seek_failure_keeps_playback_active(self):
        self.player.mpv.command.side_effect = RuntimeError("not seekable")
        self.player._play_index(0, start_pos=42.0)

        self.player._handle_mpv_file_loaded({})

        self.assertTrue(self.player._is_buffering)
        self.assertIn("playing from start", self.player._status_msg)

    def test_fuzzy_search_cursor_selects_non_first_match(self):
        self.player.queue = TrackQueue([Track("a", "Song A"), Track("b", "Song B")])
        self.player._input_mode = "search"
        self.player._input_buffer = "song"
        self.player.queue.set_filter("song")
        self.player._list_height = 3
        down = type("Key", (), {"name": "KEY_DOWN", "is_sequence": True})()

        self.player._handle_key(down)
        self.player._handle_key("\n")

        self.assertEqual(self.player._selected_list_idx, 1)
        self.assertEqual(self.player.queue.current_track.id, "b")

    def test_navigation_requests_immediate_render(self):
        self.player._vim_mode = True
        self.player._render_requested.clear()

        self.player._handle_key("j")

        self.assertTrue(self.player._render_requested.is_set())


class TestPowerInhibition(unittest.TestCase):
    def setUp(self):
        self.player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.player.queue = TrackQueue([Track("a", "A")])
        self.player.mpv = MagicMock()
        self.player.mpv.pause = False
        self.player.previewer.request = MagicMock()

    def tearDown(self):
        self.player.previewer.close()

    @patch("subprocess.Popen")
    def test_playback_blocks_system_sleep(self, popen):
        process = popen.return_value

        self.player._play_index(0)

        popen.assert_called_once_with(
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
        self.assertIs(self.player._power_inhibitor, process)

    @patch("subprocess.Popen")
    def test_pause_releases_system_sleep(self, popen):
        process = popen.return_value
        process.stdin = io.BytesIO()
        self.player._play_index(0)

        self.player._handle_key(" ")

        self.assertTrue(process.stdin.closed)
        process.wait.assert_called_once_with(timeout=0.5)
        self.assertIsNone(self.player._power_inhibitor)

    @patch("subprocess.Popen", side_effect=FileNotFoundError("systemd-inhibit"))
    def test_missing_systemd_inhibit_does_not_interrupt_playback(self, _popen):
        self.player._play_index(0)

        self.player.mpv.play.assert_called_once_with("https://youtu.be/a")
        self.assertTrue(self.player._is_buffering)
        self.assertIsNone(self.player._power_inhibitor)


class TestMockedPlayerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_run_shuts_down_mpv_and_optional_processes(self):
        with patch("mpf_core.player.MPV") as mock_mpv:
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
            player.previewer.close()
            player.term = MagicMock()
            player.term.inkey.return_value = "q"
            player.spectrum = MagicMock()
            player.previewer = MagicMock()
            player.load_playlist = AsyncMock()
            player._render = MagicMock()

            await player.run()

        mock_mpv.return_value.terminate.assert_called_once()
        player.spectrum.start.assert_called_once()
        player.spectrum.stop.assert_called_once()
        player.previewer.clear.assert_called_once()
        player.previewer.close.assert_called_once()


class TestThumbnailPolicy(unittest.TestCase):
    def test_render_never_downloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback = os.path.join(tmpdir, "fallback.svg")
            with open(fallback, "w", encoding="utf-8") as fh:
                fh.write("<svg/>")
            previewer = KittyPreviewer(tmpdir, fallback)
            previewer.has_kitty = True
            with patch("mpf_core.previewer.urllib.request.urlopen") as urlopen, patch(
                "mpf_core.previewer.subprocess.run", return_value=MagicMock(returncode=0)
            ):
                previewer.render(Track("missing", "Missing"), 0, 0, 10, 5)
            previewer.close()
            urlopen.assert_not_called()

    def test_expiration_removes_oldest_files_to_size_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            previewer = KittyPreviewer(tmpdir, max_age_seconds=100, max_cache_bytes=6)
            old = os.path.join(tmpdir, "old.jpg")
            recent = os.path.join(tmpdir, "recent.jpg")
            expired = os.path.join(tmpdir, "expired.jpg")
            for path in [old, recent, expired]:
                with open(path, "wb") as fh:
                    fh.write(b"12345")
            now = time.time()
            os.utime(old, (now - 50, now - 50))
            os.utime(recent, (now - 10, now - 10))
            os.utime(expired, (now - 200, now - 200))
            previewer.expire_cache()
            previewer.close()
            self.assertFalse(os.path.exists(old))
            self.assertFalse(os.path.exists(expired))
            self.assertTrue(os.path.exists(recent))


class TestRenderOptimization(unittest.TestCase):
    def test_static_non_visualizer_signature_is_stable(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        player._show_visualizer = False
        player._is_buffering = False
        player._is_loading_playlist = False
        with patch("mpf_core.player.time.time", return_value=100.2):
            first = player._render_signature()
            second = player._render_signature()
        player.previewer.close()
        self.assertEqual(first, second)

    def test_visualizer_redraw_rate_is_capped(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)

        player._show_visualizer = True
        self.assertEqual(player._render_delay(), 1 / 15)
        player._show_visualizer = False
        self.assertEqual(player._render_delay(), 0.1)


class TestPlaybackStatePersistence(unittest.IsolatedAsyncioTestCase):
    def _player(self):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue = TrackQueue([Track("a", "Song A"), Track("b", "Song B")])
        return player

    def test_snapshot_requires_a_current_track(self):
        player = self._player()
        player.queue = TrackQueue()
        self.assertIsNone(player._snapshot_playback_state())

        player.queue = TrackQueue([Track("a", "Song A")])
        player._time_pos = 12.5
        state = player._snapshot_playback_state()
        self.assertEqual((state.track_id, state.time_pos), ("a", 12.5))

    async def test_periodic_state_save_runs_off_the_event_loop_thread(self):
        player = self._player()
        player.mpv = MagicMock(time_pos=12.0, duration=100.0, paused_for_cache=False, core_idle=False)
        loop_thread = threading.get_ident()
        saved = []

        def fake_save(state):
            saved.append((state, threading.get_ident()))
            return True

        with patch("mpf_core.player.save_playback_state", side_effect=fake_save):
            task = asyncio.create_task(player._mpv_poll_loop())
            await asyncio.sleep(0.05)
            player._running = False
            await task

        self.assertEqual(len(saved), 1)
        state, thread_id = saved[0]
        self.assertEqual((state.track_id, state.time_pos), ("a", 12.0))
        self.assertNotEqual(thread_id, loop_thread)

    async def test_cancelled_poll_loop_does_not_interrupt_in_flight_save(self):
        player = self._player()
        player.mpv = MagicMock(time_pos=12.0, duration=100.0, paused_for_cache=False, core_idle=False)
        started = threading.Event()
        release = threading.Event()
        finished = []

        def slow_save(_state):
            started.set()
            release.wait(timeout=5)
            finished.append(True)
            return True

        with patch("mpf_core.player.save_playback_state", side_effect=slow_save):
            task = asyncio.create_task(player._mpv_poll_loop())
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            self.assertFalse(player._state_save.cancelled())
            release.set()
            await asyncio.gather(player._state_save, return_exceptions=True)

        self.assertEqual(finished, [True])


class TestIdleAndBufferingState(unittest.IsolatedAsyncioTestCase):
    async def _poll_once(self, **mpv_props):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue = TrackQueue([Track("a", "Song A")])
        player.mpv = MagicMock(time_pos=0.0, duration=0.0, paused_for_cache=False, **mpv_props)
        task = asyncio.create_task(player._mpv_poll_loop())
        await asyncio.sleep(0.05)
        player._running = False
        with patch("mpf_core.player.save_playback_state"):
            await task
        return player

    async def test_nothing_loaded_is_idle_not_buffering(self):
        player = await self._poll_once(core_idle=True, idle_active=True)

        self.assertTrue(player._mpv_idle)
        self.assertFalse(player._is_buffering)

    async def test_loading_file_with_idle_core_is_buffering(self):
        player = await self._poll_once(core_idle=True, idle_active=False)

        self.assertFalse(player._mpv_idle)
        self.assertTrue(player._is_buffering)

    async def test_visualizer_stays_paused_while_nothing_is_loaded(self):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue = TrackQueue([Track("a", "Song A")])
        player._show_visualizer = True
        player.spectrum = MagicMock(enabled=False)
        player.mpv = MagicMock(
            time_pos=0.0, duration=0.0, paused_for_cache=False, core_idle=True, idle_active=True
        )
        task = asyncio.create_task(player._mpv_poll_loop())
        await asyncio.sleep(0.05)
        player._running = False
        with patch("mpf_core.player.save_playback_state"):
            await task

        player.spectrum.resume.assert_not_called()

    def test_playing_a_track_clears_idle_state(self):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue = TrackQueue([Track("a", "Song A")])
        player.mpv = MagicMock()
        player.previewer.request = MagicMock()
        self.assertTrue(player._mpv_idle)

        player._play_index(0)

        self.assertFalse(player._mpv_idle)
        self.assertTrue(player._is_buffering)


def _mouse(name, x, y):
    return type(
        "Mouse",
        (),
        {"name": name, "mouse_xy": (x, y), "is_mouse_left": lambda self: name == "MOUSE_LEFT"},
    )()


class TestVisualizerOnPause(unittest.IsolatedAsyncioTestCase):
    async def _mode_after_poll(self, paused, **mpv_props):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue = TrackQueue([Track("a", "Song A")])
        player._show_visualizer = True
        player._is_paused = paused
        player.spectrum = MagicMock(enabled=True)
        player.mpv = MagicMock(time_pos=5.0, duration=100.0, paused_for_cache=False, **mpv_props)
        task = asyncio.create_task(player._mpv_poll_loop())
        await asyncio.sleep(0.05)
        player._running = False
        with patch("mpf_core.player.save_playback_state"):
            await task
        return player.spectrum

    async def test_pausing_freezes_the_visualizer_instead_of_clearing_it(self):
        spectrum = await self._mode_after_poll(True, core_idle=True, idle_active=False)

        spectrum.freeze.assert_called_once()
        spectrum.pause.assert_not_called()

    async def test_nothing_loaded_clears_the_visualizer(self):
        spectrum = await self._mode_after_poll(False, core_idle=True, idle_active=True)

        spectrum.pause.assert_called_once()
        spectrum.freeze.assert_not_called()

    async def test_playing_runs_the_visualizer(self):
        spectrum = await self._mode_after_poll(False, core_idle=False, idle_active=False)

        spectrum.resume.assert_called_once()

    def test_frozen_waterfall_holds_its_rows_and_cleared_one_goes_blank(self):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player._spectrum_style = "waterfall"
        player.spectrum = MagicMock(enabled=False)
        rows = [[0.5] * 4, [0.9] * 4]
        player._spectrum_history = [list(row) for row in rows]

        player._spectrum_mode = "freeze"
        frozen = player._render_spectrum_lines([0.1] * 4, 2, 4)
        self.assertEqual(player._spectrum_history, rows)
        self.assertTrue("".join(frozen).strip())

        player._spectrum_mode = "clear"
        cleared = player._render_spectrum_lines([0.1] * 4, 2, 4)
        self.assertEqual(player._spectrum_history, [])
        self.assertFalse("".join(cleared).strip())

        player.spectrum.enabled = True
        player._render_spectrum_lines([0.1] * 4, 2, 4)
        self.assertEqual(len(player._spectrum_history), 1)


class TestMouseControls(unittest.TestCase):
    def setUp(self):
        with patch("mpf_core.player.MPV"):
            self.player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(self.player.previewer.close)
        self.player.queue = TrackQueue([Track("a", "A"), Track("b", "B"), Track("c", "C")])
        self.player.mpv = MagicMock()
        self.player._duration = 100.0
        self.player._timeline_hit = (2, 7, 85)  # row, first column, width

    def test_click_on_the_progress_bar_jumps_to_that_point(self):
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 7 + 42, 2))  # 42 of 84 cells: halfway

        self.player.mpv.command.assert_called_once_with("seek", 50.0, "absolute")
        self.assertEqual(self.player._time_pos, 50.0)

    def test_click_at_either_end_of_the_bar_seeks_to_start_and_end(self):
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 7, 2))
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 7 + 84, 2))

        self.assertEqual(
            [call.args for call in self.player.mpv.command.call_args_list],
            [("seek", 0.0, "absolute"), ("seek", 100.0, "absolute")],
        )

    def test_dragging_scrubs_until_release_and_clamps_outside_the_bar(self):
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 7 + 21, 2))
        self.player._handle_mouse(_mouse("MOUSE_LEFT_MOTION", 7 + 63, 3))  # may leave the row
        self.player._handle_mouse(_mouse("MOUSE_LEFT_MOTION", 200, 2))  # past the right edge
        self.player._handle_mouse(_mouse("MOUSE_LEFT_RELEASED", 200, 2))
        self.player._handle_mouse(_mouse("MOUSE_LEFT_MOTION", 7 + 10, 2))  # after release: ignored

        self.assertEqual(
            [call.args[1] for call in self.player.mpv.command.call_args_list], [25.0, 75.0, 100.0]
        )

    def test_clicks_that_miss_the_bar_or_have_no_duration_do_not_seek(self):
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 3, 2))  # on the elapsed-time label
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 7 + 90, 2))  # past the bar
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 20, 5))  # another row
        self.player._duration = 0.0
        self.player._handle_mouse(_mouse("MOUSE_LEFT", 20, 2))

        self.player.mpv.command.assert_not_called()

    def test_clicking_summary_icons_cycles_repeat_and_shuffles(self):
        self.player._summary_hits = [(80, 82, "repeat"), (85, 87, "shuffle")]
        before = self.player.queue.repeat_mode

        self.player._handle_mouse(_mouse("MOUSE_LEFT", 81, 0))
        self.assertEqual(self.player.queue.repeat_mode, before.next_mode())

        with patch.object(self.player.queue, "shuffle") as shuffle:
            self.player._handle_mouse(_mouse("MOUSE_LEFT", 83, 0))  # between icons
            shuffle.assert_not_called()
            self.player._handle_mouse(_mouse("MOUSE_LEFT", 86, 0))
            shuffle.assert_called_once()

    def test_render_publishes_click_targets(self):
        player = self.player
        player._show_visualizer = False
        out = io.StringIO()
        with patch.object(Terminal, "height", new_callable=PropertyMock, return_value=30), patch.object(
            Terminal, "width", new_callable=PropertyMock, return_value=100
        ), redirect_stdout(out):
            player.term = Terminal(force_styling=True, kind="xterm-256color")
            player._render()

        row, bar_x, bar_width = player._timeline_hit
        self.assertEqual((row, bar_x), (2, len("00:00") + 2))
        self.assertEqual({action for _s, _e, action in player._summary_hits}, {"repeat", "shuffle"})
        for start, end, _action in player._summary_hits:
            self.assertGreater(end, start)
            self.assertLessEqual(end, 100)


class TestRenderProtection(unittest.TestCase):
    def setUp(self):
        with patch("mpf_core.player.MPV"):
            self.player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(self.player.previewer.close)
        self.player.queue = TrackQueue([Track("a", "A"), Track("b", "B"), Track("c", "C")])
        self.player._vim_mode = True
        self.player._list_top_row = 4
        self.player._list_height = 3

    def test_scrolling_past_the_last_track_requests_no_redraw(self):
        self.player._selected_list_idx = 2
        wheel = _mouse("MOUSE_SCROLL_DOWN", 5, 5)
        for event in ("j", wheel, wheel, "j", "x"):
            self.player._render_requested.clear()
            self.player._handle_key(event)
            self.assertFalse(self.player._render_requested.is_set(), event)

    def test_scrolling_past_the_first_track_requests_no_redraw(self):
        for event in ("k", _mouse("MOUSE_SCROLL_UP", 5, 5)):
            self.player._render_requested.clear()
            self.player._handle_key(event)
            self.assertFalse(self.player._render_requested.is_set())

    def test_real_changes_still_request_a_redraw(self):
        for event, expected in (("j", 1), (_mouse("MOUSE_SCROLL_DOWN", 5, 5), 2), ("k", 1), ("s", None)):
            self.player._render_requested.clear()
            self.player._handle_key(event)
            self.assertTrue(self.player._render_requested.is_set(), event)
            if expected is not None:
                self.assertEqual(self.player._selected_list_idx, expected)

    def test_static_screens_are_not_redrawn_but_a_live_spectrum_is(self):
        player = self.player
        player.spectrum = MagicMock(enabled=False)
        player._show_visualizer = True
        self.assertFalse(player._visualizer_animating())
        player.spectrum.enabled = True
        self.assertTrue(player._visualizer_animating())
        player._show_help = True
        self.assertFalse(player._visualizer_animating())

    def test_signature_changes_with_style_visibility_and_queue_order(self):
        player = self.player
        base = player._render_signature()
        player._spectrum_style = "bars"
        self.assertNotEqual(player._render_signature(), base)
        base = player._render_signature()
        player._show_visualizer = not player._show_visualizer
        self.assertNotEqual(player._render_signature(), base)
        base = player._render_signature()
        player.queue.shuffle()
        self.assertNotEqual(player._render_signature(), base)

    def test_visualizer_toggle_requests_a_redraw(self):
        self.player._render_requested.clear()
        self.player._handle_key("v")
        self.assertTrue(self.player._render_requested.is_set())


class TestRenderOutput(unittest.TestCase):
    def _player(self):
        with patch("mpf_core.player.MPV"):
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue = TrackQueue([Track("a", "A"), Track("b", "B")])
        player.previewer = MagicMock()
        player._show_visualizer = False
        return player

    def _render(self, player):
        out = io.StringIO()
        with patch.object(Terminal, "height", new_callable=PropertyMock, return_value=30), patch.object(
            Terminal, "width", new_callable=PropertyMock, return_value=100
        ), redirect_stdout(out):
            player.term = Terminal(force_styling=True, kind="xterm-256color")
            player._render()
        return out.getvalue(), player.term

    def test_closing_help_wipes_the_screen_and_stale_thumbnail(self):
        player = self._player()
        player._show_help = True
        self._render(player)
        player.previewer.clear.assert_called()  # entering help drops the graphic

        player.previewer.clear.reset_mock()
        player._show_help = False
        output, term = self._render(player)

        player.previewer.clear.assert_called()
        self.assertTrue(output.startswith(term.clear + term.home))

        output, term = self._render(player)  # an ordinary frame must not wipe the screen
        self.assertFalse(output.startswith(term.clear))

    def test_every_frame_ends_with_the_cursor_hidden(self):
        player = self._player()
        output, term = self._render(player)

        self.assertTrue(output.endswith(term.hide_cursor))


class TestSearchIsolation(unittest.TestCase):
    ESC = type("Key", (), {"name": "KEY_ESCAPE", "is_sequence": True})()
    DOWN = type("Key", (), {"name": "KEY_DOWN", "is_sequence": True})()

    def setUp(self):
        with patch("mpf_core.player.MPV"):
            self.player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(self.player.previewer.close)
        tracks = [Track(str(i), f"Song {i:02d}") for i in range(30)]
        tracks[20] = Track("20", "Needle Track")
        self.player.queue = TrackQueue(tracks)
        self.player.mpv = MagicMock()
        self.player.previewer.request = MagicMock()
        self.player._list_height = 5
        self.player._selected_list_idx = 12
        self.player._scroll_offset = 10

    def _search(self, text):
        self.player._handle_key("/")
        for char in text:
            self.player._handle_key(char)

    def _assert_back_on_full_list_where_left_off(self):
        player = self.player
        self.assertEqual(player._input_mode, "none")
        self.assertEqual(len(player.queue.filtered_tracks), 30)
        self.assertEqual((player._selected_list_idx, player._scroll_offset), (12, 10))

    def test_escape_returns_to_where_you_left_off(self):
        self._search("needle")
        self.assertEqual(len(self.player.queue.filtered_tracks), 1)
        self.assertEqual(self.player._selected_list_idx, 0)

        self.player._handle_key(self.ESC)

        self._assert_back_on_full_list_where_left_off()
        self.assertNotEqual(self.player.queue.current_track.title, "Needle Track")

    def test_escape_after_browsing_matches_or_erasing_the_query_still_restores(self):
        self._search("song")
        for _ in range(3):
            self.player._handle_key(self.DOWN)
        self.player._handle_key(self.ESC)
        self._assert_back_on_full_list_where_left_off()

        self._search("ab")
        self.player._handle_key("\x7f")
        self.player._handle_key("\x7f")
        self.player._handle_key(self.ESC)
        self._assert_back_on_full_list_where_left_off()

    def test_enter_plays_the_match_and_selects_it_in_the_full_list(self):
        self._search("needle")

        self.player._handle_key("\n")

        player = self.player
        self.assertEqual(player.queue.current_track.title, "Needle Track")
        self.assertEqual(player._input_mode, "none")
        self.assertEqual(len(player.queue.filtered_tracks), 30)
        self.assertEqual(player._selected_list_idx, 20)
        self.assertLessEqual(player._scroll_offset, 20)
        self.assertLess(20, player._scroll_offset + player._list_height)

    def test_enter_without_matches_plays_nothing_and_restores_the_selection(self):
        playing_before = self.player.queue.current_idx
        self._search("zzzz")

        self.player._handle_key("\n")

        self._assert_back_on_full_list_where_left_off()
        self.assertEqual(self.player.queue.current_idx, playing_before)
        self.player.mpv.play.assert_not_called()

    def test_restored_selection_is_clamped_if_the_playlist_shrank_meanwhile(self):
        self.player._selected_list_idx = 25
        self.player._scroll_offset = 22
        self._search("song")
        self.player.queue.set_tracks(self.player.queue.tracks[:10])

        self.player._handle_key(self.ESC)

        self.assertEqual(self.player._selected_list_idx, 9)
        self.assertLessEqual(self.player._scroll_offset, 9)


if __name__ == "__main__":
    unittest.main()
