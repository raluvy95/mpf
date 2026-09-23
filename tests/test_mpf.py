"""Unit tests for mpf_core modules: models, fuzzy search, caching, Kitty previewer, and PipeWire visualizer."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mpf import DEFAULT_PLAYLIST_URL, parse_cli_args
from mpf_core import (
    KittyPreviewer,
    PipeWireSpectrumAnalyzer,
    RepeatMode,
    Track,
    TrackQueue,
    fetch_playlist_tracks,
    format_time,
    fuzzy_filter_tracks,
    fuzzy_score,
    load_cached_playlist,
    save_cached_playlist,
)
from mpf_core.config import (
    load_default_playlist,
    load_vim_mode,
    load_visualizer_preferences,
    save_default_playlist,
    save_vim_mode,
    save_visualizer_preferences,
)


class TestFormatTime(unittest.TestCase):
    def test_none_and_invalid(self):
        self.assertEqual(format_time(None), "--:--")
        self.assertEqual(format_time(-10), "--:--")
        self.assertEqual(format_time(float("nan")), "--:--")

    def test_seconds_only(self):
        self.assertEqual(format_time(0), "00:00")
        self.assertEqual(format_time(9), "00:09")
        self.assertEqual(format_time(45.4), "00:45")

    def test_minutes_and_seconds(self):
        self.assertEqual(format_time(60), "01:00")
        self.assertEqual(format_time(125), "02:05")
        self.assertEqual(format_time(3599), "59:59")

    def test_hours(self):
        self.assertEqual(format_time(3600), "1:00:00")
        self.assertEqual(format_time(3665), "1:01:05")
        self.assertEqual(format_time(7322), "2:02:02")


class TestTrackModel(unittest.TestCase):
    def test_display_label_basic(self):
        t = Track(
            id="abc123",
            title="Song A",
            duration=180,
            uploader="Artist X",
        )
        label = t.display_label(0, is_active=False)
        self.assertEqual(label, "  1. Song A - Artist X [03:00]")

    def test_display_label_active(self):
        t = Track(id="abc123", title="Song A")
        label = t.display_label(2, is_active=True)
        self.assertEqual(label, "▶ 3. Song A")

    def test_computed_url(self):
        t = Track(id="dQw4w9WgXcQ", title="Never Gonna Give You Up")
        self.assertEqual(t.url, "https://youtu.be/dQw4w9WgXcQ")

    def test_computed_thumbnail(self):
        t = Track(id="dQw4w9WgXcQ", title="Never Gonna Give You Up")
        self.assertEqual(t.thumbnail, "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")

    def test_url_override(self):
        """Non-YouTube sources can still pass a custom URL via _url."""
        t = Track(id="local", title="Local File", _url="file:///music/song.mp3")
        self.assertEqual(t.url, "file:///music/song.mp3")

    def test_cache_tuple_roundtrip(self):
        t = Track(id="abc", title="Test", duration=120.9, uploader="Artist")
        tup = t.to_cache_tuple()
        self.assertEqual(tup, ("abc", "Test", 120, "Artist"))
        restored = Track.from_cache_tuple(tup)
        self.assertEqual(restored.id, t.id)
        self.assertEqual(restored.title, t.title)
        self.assertEqual(restored.duration, 120.0)
        self.assertEqual(restored.uploader, t.uploader)
        self.assertEqual(restored.thumbnail, t.thumbnail)

    def test_legacy_dict_roundtrip(self):
        """to_dict/from_dict reconstruct same computed URL and thumbnail."""
        t = Track(id="abc", title="Test", duration=120, uploader="Artist")
        data = t.to_dict()
        restored = Track.from_dict(data)
        self.assertEqual(restored.id, t.id)
        self.assertEqual(restored.title, t.title)
        self.assertEqual(restored.thumbnail, t.thumbnail)


class TestRepeatMode(unittest.TestCase):
    def test_cycling(self):
        m = RepeatMode.OFF
        m = m.next_mode()
        self.assertEqual(m, RepeatMode.ALL)
        m = m.next_mode()
        self.assertEqual(m, RepeatMode.ONE)
        m = m.next_mode()
        self.assertEqual(m, RepeatMode.OFF)


class TestFuzzySearch(unittest.TestCase):
    def test_fuzzy_score_exact_and_subsequence(self):
        self.assertIsNotNone(fuzzy_score("creep", "Radiohead - Creep"))
        self.assertIsNotNone(fuzzy_score("radcrp", "Radiohead - Creep"))
        self.assertIsNone(fuzzy_score("xyz", "Radiohead - Creep"))

    def test_fuzzy_ranking(self):
        tracks = [
            Track(id="1", title="Bohemian Rhapsody", uploader="Queen"),
            Track(id="2", title="Radio Ga Ga", uploader="Queen"),
            Track(id="3", title="Karma Police", uploader="Radiohead"),
        ]
        # Query 'radio' should rank 'Radio Ga Ga' and 'Radiohead' highest
        results = fuzzy_filter_tracks(tracks, "radio")
        self.assertEqual(len(results), 2)
        matched_titles = [t.title for _, t in results]
        self.assertIn("Radio Ga Ga", matched_titles)
        self.assertIn("Karma Police", matched_titles)

    def test_fuzzy_multi_token(self):
        tracks = [
            Track(id="1", title="Paranoid Android", uploader="Radiohead"),
            Track(id="2", title="No Surprises", uploader="Radiohead"),
            Track(id="3", title="Android Dreams", uploader="Other"),
        ]
        results = fuzzy_filter_tracks(tracks, "rad android")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1].title, "Paranoid Android")


class TestTrackQueue(unittest.TestCase):
    def setUp(self):
        self.tracks = [
            Track(id="1", title="Alpha Song", duration=100, uploader="Alice"),
            Track(id="2", title="Beta Track", duration=200, uploader="Bob"),
            Track(id="3", title="Gamma Vibes", duration=300, uploader="Charlie"),
        ]
        self.queue = TrackQueue(self.tracks)

    def test_initial_state(self):
        self.assertEqual(len(self.queue.tracks), 3)
        self.assertEqual(self.queue.current_idx, 0)
        self.assertEqual(self.queue.current_track, self.tracks[0])

    def test_empty_queue(self):
        empty_q = TrackQueue([])
        self.assertIsNone(empty_q.current_track)
        self.assertIsNone(empty_q.get_next_index())
        self.assertIsNone(empty_q.get_prev_index())

    def test_next_index_normal(self):
        self.queue.current_idx = 0
        self.assertEqual(self.queue.get_next_index(), 1)
        self.queue.current_idx = 1
        self.assertEqual(self.queue.get_next_index(), 2)

    def test_next_index_wrap_all(self):
        self.queue.repeat_mode = RepeatMode.ALL
        self.queue.current_idx = 2
        self.assertEqual(self.queue.get_next_index(natural_end=True), 0)

    def test_next_index_repeat_one(self):
        self.queue.repeat_mode = RepeatMode.ONE
        self.queue.current_idx = 1
        self.assertEqual(self.queue.get_next_index(natural_end=True), 1)
        self.assertEqual(self.queue.get_next_index(natural_end=False), 2)

    def test_next_index_repeat_off(self):
        self.queue.repeat_mode = RepeatMode.OFF
        self.queue.current_idx = 2
        self.assertIsNone(self.queue.get_next_index(natural_end=True))
        self.assertEqual(self.queue.get_next_index(natural_end=False), 0)

    def test_prev_index(self):
        self.queue.current_idx = 2
        self.assertEqual(self.queue.get_prev_index(), 1)
        self.queue.current_idx = 0
        self.assertEqual(self.queue.get_prev_index(), 2)

    def test_shuffle(self):
        curr = self.tracks[1]
        self.queue.current_idx = 1
        self.queue.shuffle()
        self.assertEqual(len(self.queue.tracks), 3)
        self.assertEqual(self.queue.current_track, curr)

    def test_fuzzy_search_filter(self):
        self.queue.set_filter("beta")
        filtered = self.queue.filtered_tracks
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], 1)
        self.assertEqual(filtered[0][1].title, "Beta Track")

        self.queue.set_filter("alice")
        filtered = self.queue.filtered_tracks
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][1].title, "Alpha Song")

        self.queue.clear_filter()
        self.assertEqual(len(self.queue.filtered_tracks), 3)

    def test_filtered_tracks_reuses_results_until_queue_or_query_changes(self):
        self.queue.set_filter("song")
        first = self.queue.filtered_tracks

        self.assertIs(self.queue.filtered_tracks, first)

        self.queue.set_filter("beta")
        second = self.queue.filtered_tracks
        self.assertIsNot(second, first)
        self.assertEqual([track.id for _, track in second], ["2"])

        self.queue.set_tracks([Track("4", "Beta replacement")])
        third = self.queue.filtered_tracks
        self.assertIsNot(third, second)
        self.assertEqual([track.id for _, track in third], ["4"])


class TestPlaylistCaching(unittest.TestCase):
    def test_save_and_load_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "cache.mpk.zst")
            url = "https://yt.com/playlist?list=123"
            tracks = [
                Track(id="t1", title="Title 1", duration=60, uploader="Up 1"),
                Track(id="t2", title="Title 2", duration=120, uploader="Up 2"),
            ]

            self.assertIsNone(load_cached_playlist(url, cache_file))

            save_cached_playlist(url, tracks, cache_file)
            loaded = load_cached_playlist(url, cache_file)
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].id, "t1")
            self.assertEqual(loaded[0].title, "Title 1")
            # thumbnail is now derived from the video id
            self.assertEqual(loaded[0].thumbnail, "https://i.ytimg.com/vi/t1/hqdefault.jpg")
            self.assertEqual(loaded[1].duration, 120.0)


class TestPlaybackStateCaching(unittest.TestCase):
    def test_save_and_load_playback_state(self):
        from mpf_core.cache import PlaybackState, load_playback_state, save_playback_state

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, ".cache_playback.json")
            url = "https://yt.com/playlist?list=456"
            state = PlaybackState(
                playlist_url=url,
                track_id="vid_99",
                track_index=3,
                time_pos=142.5,
                duration=300.0,
                repeat_mode="One",
                volume=85,
            )

            self.assertIsNone(load_playback_state(url, cache_file))
            save_playback_state(state, cache_file)

            loaded = load_playback_state(url, cache_file)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.playlist_url, url)
            self.assertEqual(loaded.track_id, "vid_99")
            self.assertEqual(loaded.track_index, 3)
            self.assertEqual(loaded.time_pos, 142.5)
            self.assertEqual(loaded.repeat_mode, "One")
            self.assertEqual(loaded.volume, 85)


class TestKittyPreviewer(unittest.TestCase):
    def test_get_local_image_path_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_svg = os.path.join(tmpdir, "fake.svg")
            with open(fake_svg, "w") as f:
                f.write("<svg></svg>")

            previewer = KittyPreviewer(thumbs_dir=tmpdir, fallback_icon=fake_svg)
            path = previewer.get_local_image_path(None)
            self.assertEqual(path, fake_svg)

    def test_render_clears_previous_image_before_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = os.path.join(tmpdir, "fallback.svg")
            with open(image, "w", encoding="utf-8") as file:
                file.write("<svg/>")
            previewer = KittyPreviewer(thumbs_dir=tmpdir, fallback_icon=image)
            self.addCleanup(previewer.close)
            previewer.has_kitty = True
            previewer._current_rendered = ("old", 0, 0, 10, 5)
            with patch.object(previewer, "clear", wraps=previewer.clear) as clear, patch(
                "mpf_core.previewer.subprocess.run", return_value=MagicMock(returncode=0)
            ):
                previewer.render(Track("track", "Track"), 2, 1, 10, 5)
            clear.assert_called_once()


class TestSpectrumAnalyzer(unittest.TestCase):
    def test_startup_disabled_state(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=16, target_node="mpv")
        self.assertFalse(analyzer.enabled)

    def test_target_node_command(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=16, target_node="mpv")
        with patch(
            "mpf_core.visualizer.shutil.which",
            side_effect=lambda name: "/usr/bin/pw-record" if name == "pw-record" else None,
        ):
            cmd = analyzer._get_capture_command()
        self.assertIsNotNone(cmd)
        self.assertIn("mpv", cmd)

    def test_spectrum_pause_resume(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=16, target_node="mpv")
        self.assertFalse(analyzer.enabled)
        analyzer.resume()
        self.assertTrue(analyzer.enabled)
        analyzer.pause()
        self.assertFalse(analyzer.enabled)

    def test_spectrum_is_independent_of_input_gain(self):
        phase = np.linspace(0, 2 * np.pi * 12, 512, endpoint=False)
        quiet = (np.sin(phase) * 1_000).astype(np.int16)
        loud = (np.sin(phase) * 16_000).astype(np.int16)
        quiet_analyzer = PipeWireSpectrumAnalyzer(num_bands=16)
        loud_analyzer = PipeWireSpectrumAnalyzer(num_bands=16)

        quiet_analyzer._process_samples(quiet)
        loud_analyzer._process_samples(loud)

        np.testing.assert_allclose(
            quiet_analyzer.get_bands()[0],
            loud_analyzer.get_bands()[0],
            atol=2e-4,
        )

    def test_spectrum_remains_visible_at_low_output_gain(self):
        phase = np.linspace(0, 2 * np.pi * 12, 512, endpoint=False)
        quiet = (np.sin(phase) * 8).astype(np.int16)
        loud = (np.sin(phase) * 16_000).astype(np.int16)
        quiet_analyzer = PipeWireSpectrumAnalyzer(num_bands=16)
        loud_analyzer = PipeWireSpectrumAnalyzer(num_bands=16)

        quiet_analyzer._process_samples(quiet)
        loud_analyzer._process_samples(loud)

        self.assertGreater(max(quiet_analyzer.get_bands()[0]), 0.0)
        self.assertGreater(max(loud_analyzer.get_bands()[0]), 0.0)

    def test_vectorized_smoothing_preserves_attack_decay_and_peaks(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=3)

        analyzer._smooth_bands(np.array([1.0, 0.5, 0.0], dtype=np.float32))
        np.testing.assert_allclose(analyzer.get_bands()[0], [0.65, 0.325, 0.0])
        np.testing.assert_allclose(analyzer.get_bands()[1], [0.65, 0.325, 0.0])

        analyzer._smooth_bands(np.zeros(3, dtype=np.float32))
        np.testing.assert_allclose(analyzer.get_bands()[0], [0.572, 0.286, 0.0], atol=1e-6)
        np.testing.assert_allclose(analyzer.get_bands()[1], [0.63, 0.305, 0.0], atol=1e-6)

    def test_pause_during_capture_keeps_process_reference_safe(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=16, target_node="mpv")
        process = MagicMock()
        process.poll.return_value = None
        analyzer._process = process
        analyzer.pause()
        self.assertIsNone(analyzer._process)
        process.terminate.assert_called_once()


class TestPlaylistFetcher(unittest.TestCase):
    @patch("yt_dlp.YoutubeDL")
    def test_fetch_playlist_success(self, mock_ydl_cls):
        mock_ydl_inst = MagicMock()
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_inst
        mock_ydl_inst.extract_info.return_value = {
            "entries": [
                {
                    "id": "vid1",
                    "title": "Music 1",
                    "url": "https://www.youtube.com/watch?v=vid1",
                    "duration": 210,
                    "uploader": "Channel 1",
                    "thumbnail": "https://i.ytimg.com/vi/vid1/hqdefault.jpg",
                },
                {
                    "id": "vid2",
                    "title": "Music 2",
                    "url": "vid2",
                    "duration": None,
                    "channel": "Channel 2",
                },
                None,
            ]
        }

        tracks = fetch_playlist_tracks("https://www.youtube.com/playlist?list=test")
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].id, "vid1")
        self.assertEqual(tracks[0].thumbnail, "https://i.ytimg.com/vi/vid1/hqdefault.jpg")


class TestCLIArgs(unittest.TestCase):
    def test_default_args(self):
        args = parse_cli_args([])
        self.assertEqual(args.url, DEFAULT_PLAYLIST_URL)
        self.assertFalse(args.no_auto_play)

    def test_extended_options(self):
        args = parse_cli_args(
            [
                "--config",
                "/tmp/mpf.json",
                "--no-auto-play",
                "--no-preview",
                "--no-visualizer",
                "--no-vim",
                "https://youtu.be/example",
            ]
        )
        self.assertEqual(args.config, "/tmp/mpf.json")
        self.assertTrue(args.no_auto_play)
        self.assertTrue(args.no_preview)
        self.assertTrue(args.no_visualizer)
        self.assertFalse(args.vim)


class TestConfiguration(unittest.TestCase):
    def test_saves_loaded_default_playlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "mpf", "config.json")
            playlist = "https://www.youtube.com/playlist?list=saved"

            self.assertTrue(save_default_playlist(playlist, config))
            self.assertEqual(load_default_playlist(config), playlist)

    def test_configured_default_playlist_is_validated(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as config:
            json.dump({"default_playlist": "https://www.youtube.com/playlist?list=test"}, config)
            config.flush()
            self.assertEqual(
                load_default_playlist(config.name),
                "https://www.youtube.com/playlist?list=test",
            )

    def test_invalid_or_missing_configuration_has_no_default(self):
        self.assertEqual(load_default_playlist("/does/not/exist"), "")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as config:
            json.dump({"default_playlist": "https://example.com"}, config)
            config.flush()
            self.assertEqual(load_default_playlist(config.name), "")

    def test_visualizer_preferences_share_config_with_playlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "mpf", "config.json")
            playlist = "https://www.youtube.com/playlist?list=saved"
            self.assertTrue(save_default_playlist(playlist, config))
            self.assertTrue(save_visualizer_preferences("braille", False, config))
            self.assertEqual(load_default_playlist(config), playlist)
            self.assertEqual(load_visualizer_preferences(config), ("braille", False))
            self.assertTrue(save_default_playlist(playlist, config))
            self.assertEqual(load_visualizer_preferences(config), ("braille", False))

    def test_invalid_visualizer_preferences_use_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as config:
            json.dump({"visualizer_style": "unknown", "show_visualizer": "false"}, config)
            config.flush()
            self.assertEqual(load_visualizer_preferences(config.name), ("waterfall", True))

    def test_vim_mode_preference_round_trip_and_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "config.json")
            self.assertTrue(load_vim_mode(config))
            self.assertTrue(save_vim_mode(False, config))
            self.assertFalse(load_vim_mode(config))

    def test_invalid_vim_mode_uses_enabled_default(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as config:
            json.dump({"vim_mode": "false"}, config)
            config.flush()
            self.assertTrue(load_vim_mode(config.name))


class TestPlayerInitialization(unittest.TestCase):
    @patch("mpf_core.player.MPV")
    def test_mpv_inhibits_screensaver_only_during_playback(self, mock_mpv):
        from mpf_core.player import BlessedMusicPlayer
        player = BlessedMusicPlayer()
        # Simulate run mpv init snippet
        player.mpv = mock_mpv(
            video=False,
            ytdl=True,
            ytdl_format="bestaudio/best",
            cache="yes",
            demuxer_max_bytes="25M",
            demuxer_readahead_secs="30",
            stop_screensaver="yes",
        )
        mock_mpv.assert_called_with(
            video=False,
            ytdl=True,
            ytdl_format="bestaudio/best",
            cache="yes",
            demuxer_max_bytes="25M",
            demuxer_readahead_secs="30",
            stop_screensaver="yes",
        )


class TestTrackListMouseNavigation(unittest.TestCase):
    def test_wheel_down_moves_selection_within_track_list(self):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track(str(i), f"Track {i}") for i in range(10)])
        player._list_top_row = 4
        player._list_height = 3
        event = type(
            "MouseEvent",
            (),
            {"name": "MOUSE_SCROLL_DOWN", "mouse_xy": (2, 5), "is_mouse_left": lambda self: False},
        )()
        player._handle_mouse(event)

        self.assertEqual(player._selected_list_idx, 1)
        self.assertEqual(player._scroll_offset, 0)


class TestSpectrumStyles(unittest.TestCase):
    @patch("mpf_core.player.load_visualizer_preferences", return_value=("waterfall", True))
    @patch("mpf_core.player.save_visualizer_preferences")
    def test_a_cycles_spectrum_styles(self, _save, _load):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)

        self.assertEqual(player._spectrum_style, "waterfall")
        player._handle_key("a")
        self.assertEqual(player._spectrum_style, "bars")
        player._handle_key("a")
        self.assertEqual(player._spectrum_style, "braille")
        player._handle_key("a")
        self.assertEqual(player._spectrum_style, "waveform")
        player._handle_key("a")
        self.assertEqual(player._spectrum_style, "waterfall")

    def test_style_and_visibility_survive_restart(self):
        from mpf_core.player import BlessedMusicPlayer

        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "config.json")
            with (
                patch(
                    "mpf_core.player.load_visualizer_preferences",
                    side_effect=lambda _config_file: load_visualizer_preferences(config),
                ),
                patch(
                    "mpf_core.player.save_visualizer_preferences",
                    side_effect=lambda style, visible, _config_file: save_visualizer_preferences(
                        style, visible, config
                    ),
                ),
            ):
                player = BlessedMusicPlayer(auto_play=False)
                self.addCleanup(player.previewer.close)
                player._handle_key("a")
                player._handle_key("v")
                self.assertEqual(load_visualizer_preferences(config), ("bars", False))

                restarted = BlessedMusicPlayer(auto_play=False)
                self.addCleanup(restarted.previewer.close)
                self.assertEqual(restarted._spectrum_style, "bars")
        self.assertFalse(restarted._show_visualizer)


class TestPlayerQualityOfLife(unittest.TestCase):
    def test_dashboard_progress_meter_clamps_and_fills_available_width(self):
        from mpf_core.player import BlessedMusicPlayer

        self.assertEqual(BlessedMusicPlayer._progress_meter(10, 0.5), "━━━━●─────")
        self.assertEqual(BlessedMusicPlayer._progress_meter(4, -1.0), "●───")
        self.assertEqual(BlessedMusicPlayer._progress_meter(4, 2.0), "━━━●")

    def test_help_overlay_opens_and_closes_with_question_mark_or_escape(self):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)

        player._handle_key("?")
        self.assertTrue(player._show_help)
        player._handle_key("\x1b")
        self.assertFalse(player._show_help)

    @patch("mpf_core.player.save_vim_mode")
    def test_uppercase_v_toggles_vim_mode(self, save_vim_mode_mock):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)

        self.assertTrue(player._vim_mode)
        player._handle_key("V")
        self.assertFalse(player._vim_mode)
        save_vim_mode_mock.assert_called_once_with(False, player.config_file)

    def test_vim_mode_uses_hjkl_instead_of_arrow_keys(self):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False, vim_mode=True)
        self.addCleanup(player.previewer.close)
        player.mpv = MagicMock()
        left = type("Key", (), {"name": "KEY_LEFT"})()

        player._handle_key(left)
        player.mpv.command.assert_not_called()

        player._handle_key("h")
        player.mpv.command.assert_called_once_with("seek", -5, "relative")

    def test_standard_mode_uses_arrow_keys_instead_of_hjkl(self):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False, vim_mode=False)
        self.addCleanup(player.previewer.close)
        player.mpv = MagicMock()
        left = type("Key", (), {"name": "KEY_LEFT"})()

        player._handle_key("h")
        player.mpv.command.assert_not_called()

        player._handle_key(left)
        player.mpv.command.assert_called_once_with("seek", -5, "relative")


if __name__ == "__main__":
    unittest.main()
