"""Regression tests for reliability and long-running resource behavior."""

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.player._render_requested.clear()

        self.player._handle_key("j")

        self.assertTrue(self.player._render_requested.is_set())


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

        self.assertEqual(player._render_delay(), 1 / 15)
        player._show_visualizer = False
        self.assertEqual(player._render_delay(), 0.1)


if __name__ == "__main__":
    unittest.main()
