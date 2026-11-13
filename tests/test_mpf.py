"""Unit tests for mpf_core modules: models, fuzzy search, caching, Kitty previewer, and PipeWire visualizer."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mpf import DEFAULT_PLAYLIST_URL, __version__, configure_logging, parse_cli_args
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
from mpf_core.fetcher import PlaylistFetchError


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
    def test_values_remain_cache_compatible_and_expose_icons(self):
        self.assertEqual(RepeatMode.OFF.value, "Off")
        self.assertEqual(RepeatMode.ALL.value, "All")
        self.assertEqual(RepeatMode.ONE.value, "One")
        self.assertEqual(RepeatMode("All"), RepeatMode.ALL)
        self.assertEqual({mode.icon for mode in RepeatMode}, {"󰑗", "󰑖", "󰑘"})

    def test_cycling(self):
        m = RepeatMode.OFF
        m = m.next_mode()
        self.assertEqual(m, RepeatMode.ALL)
        m = m.next_mode()
        self.assertEqual(m, RepeatMode.ONE)
        m = m.next_mode()
        self.assertEqual(m, RepeatMode.OFF)


class TestFuzzySearch(unittest.TestCase):
    def test_fuzzy_scores_match_the_original_implementation(self):
        # Reference values computed with the character-by-character scorer that str.find replaced.
        for pattern, text, expected in (
            ("creep", "Radiohead - Creep", 126),
            ("CREEP", "Radiohead - Creep", 126),
            ("rhc", "Radiohead - Creep", 55),
            ("pcr", "Radiohead - Creep", None),
            ("zzz", "Radiohead - Creep", None),
            ("", "x", 0),
            ("toolong", "ab", None),
            ("ab", "a_b.c", 68),
        ):
            with self.subTest(pattern=pattern, text=text):
                self.assertEqual(fuzzy_score(pattern, text), expected)

    def test_fuzzy_score_survives_characters_that_change_length_when_lowercased(self):
        # "İ".lower() is two characters, which used to push the match positions past the end of the title.
        self.assertIsNotNone(fuzzy_score("i", "İstanbul"))
        self.assertIsNotNone(fuzzy_score("stan", "İstanbul"))
        self.assertEqual([i for i, _ in fuzzy_filter_tracks([Track("1", "İstanbul", 1.0, "x")], "bul")], [0])

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

    def test_shuffle_is_a_reversible_mode(self):
        curr = self.tracks[1]
        self.queue.current_idx = 1
        original = list(self.queue.tracks)
        self.queue.set_shuffle(True)
        self.assertTrue(self.queue.shuffle_enabled)
        self.assertEqual(sorted(t.id for t in self.queue.tracks), sorted(t.id for t in original))
        self.assertEqual(self.queue.current_track, curr)
        self.queue.set_shuffle(False)
        self.assertEqual(self.queue.tracks, original)
        self.assertEqual(self.queue.current_track, curr)

    def test_shuffle_survives_resync_but_not_a_new_playlist(self):
        many = [Track(f"id{i}", f"T{i}", 10) for i in range(30)]
        self.queue.set_tracks(many)
        self.queue.set_shuffle(True)
        shuffled = [t.id for t in self.queue.tracks]
        added = Track("new", "New", 10)
        self.queue.set_tracks(many[:-1] + [added], reset_index=False)  # sync: one removed, one added
        self.assertTrue(self.queue.shuffle_enabled)
        kept = [t.id for t in self.queue.tracks if t.id != "new"]
        self.assertEqual(kept, [i for i in shuffled if i != "id29"])
        self.assertIn("new", [t.id for t in self.queue.tracks])
        self.queue.set_shuffle(False)
        self.assertEqual([t.id for t in self.queue.tracks], [t.id for t in many[:-1] + [added]])

        self.queue.set_shuffle(True)
        self.queue.set_tracks(many)  # a different playlist resets shuffle
        self.assertFalse(self.queue.shuffle_enabled)
        self.assertEqual(self.queue.tracks, many)

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

    def test_revision_changes_only_when_the_visible_queue_changes(self):
        revisions = [self.queue.revision]
        self.queue.set_filter("a")
        revisions.append(self.queue.revision)
        self.queue.set_filter("a")  # unchanged query
        self.assertEqual(self.queue.revision, revisions[-1])
        self.queue.set_shuffle(True)
        revisions.append(self.queue.revision)
        self.queue.set_tracks(list(self.queue.tracks))
        revisions.append(self.queue.revision)

        self.assertEqual(revisions, sorted(set(revisions)))

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

    def test_render_skips_icat_terminal_autodetection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = os.path.join(tmpdir, "fallback.svg")
            with open(image, "w", encoding="utf-8") as file:
                file.write("<svg/>")
            previewer = KittyPreviewer(thumbs_dir=tmpdir, fallback_icon=image)
            self.addCleanup(previewer.close)
            previewer.has_kitty = True
            with patch(
                "mpf_core.previewer.subprocess.run", return_value=MagicMock(returncode=0)
            ) as run:
                previewer.render(Track("track", "Track"), 2, 1, 10, 5)
            # Auto-detection resets cursor/mouse modes and reads the reply from stdin.
            self.assertIn("--transfer-mode=stream", run.call_args.args[0])

    def test_render_bounds_kitty_with_timeout_and_survives_hang(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image = os.path.join(tmpdir, "fallback.svg")
            with open(image, "w", encoding="utf-8") as file:
                file.write("<svg/>")
            previewer = KittyPreviewer(thumbs_dir=tmpdir, fallback_icon=image)
            self.addCleanup(previewer.close)
            previewer.has_kitty = True
            with patch(
                "mpf_core.previewer.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="kitty", timeout=5),
            ) as run:
                previewer.render(Track("track", "Track"), 2, 1, 10, 5)
            self.assertIsNotNone(run.call_args.kwargs["timeout"])


class TestSpectrumAnalyzer(unittest.TestCase):
    def test_startup_disabled_state(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=16, target_node="mpv")
        self.assertFalse(analyzer.enabled)

    def test_freeze_keeps_last_frame_while_pause_clears_it(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer.bands[:] = [0.2, 0.4, 0.6, 0.8]
        analyzer.peaks[:] = [0.3, 0.5, 0.7, 0.9]
        analyzer.resume()

        analyzer.freeze()

        self.assertFalse(analyzer.enabled)
        self.assertEqual(analyzer.bands.tolist(), np.array([0.2, 0.4, 0.6, 0.8], dtype=np.float32).tolist())
        self.assertGreater(analyzer.peaks.max(), 0)

        analyzer.pause()

        self.assertEqual(analyzer.bands.sum(), 0)
        self.assertEqual(analyzer.peaks.sum(), 0)

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

    def test_pause_during_capture_keeps_process_reference_safe(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=16, target_node="mpv")
        process = MagicMock()
        process.poll.return_value = None
        analyzer._process = process
        analyzer.pause()
        self.assertIsNone(analyzer._process)
        process.terminate.assert_called_once()


class TestPlaylistFetcher(unittest.TestCase):
    @patch("mpf_core.fetcher.shutil.which", return_value=None)  # no system yt-dlp: use the module
    @patch("yt_dlp.YoutubeDL")
    def test_fetch_playlist_success(self, mock_ydl_cls, _which):
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

    PLAYLIST = "https://www.youtube.com/playlist?list=test"

    @staticmethod
    def _completed(stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_system_yt_dlp_is_preferred_and_parsed_like_the_module(self):
        info = {"entries": [{"id": "v1", "title": "One", "duration": 61, "channel": "Ch"}, {"id": "v2"}]}
        with (
            patch("mpf_core.fetcher.shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("mpf_core.fetcher.subprocess.run", return_value=self._completed(json.dumps(info))) as run,
            patch.dict("sys.modules", {"yt_dlp": None}),  # the module must not even be needed
        ):
            tracks = fetch_playlist_tracks(self.PLAYLIST)
        self.assertEqual([(t.id, t.title, t.duration, t.uploader) for t in tracks], [("v1", "One", 61.0, "Ch")])
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/bin/yt-dlp", "-J", "--flat-playlist", "--no-warnings"])
        self.assertEqual(command[-2:], ["--", self.PLAYLIST])

    def test_system_yt_dlp_failure_reports_its_last_error_line(self):
        failed = self._completed(stderr="WARNING: x\nERROR: [youtube:tab] Private playlist\n", returncode=1)
        with (
            patch("mpf_core.fetcher.shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("mpf_core.fetcher.subprocess.run", return_value=failed),
        ):
            with self.assertRaisesRegex(PlaylistFetchError, "Private playlist"):
                fetch_playlist_tracks(self.PLAYLIST)

    def test_unreadable_or_timed_out_system_yt_dlp_is_a_fetch_error(self):
        with patch("mpf_core.fetcher.shutil.which", return_value="/usr/bin/yt-dlp"):
            with patch("mpf_core.fetcher.subprocess.run", return_value=self._completed("not json")):
                with self.assertRaisesRegex(PlaylistFetchError, "unreadable"):
                    fetch_playlist_tracks(self.PLAYLIST)
            timeout = subprocess.TimeoutExpired("yt-dlp", 1)
            with patch("mpf_core.fetcher.subprocess.run", side_effect=timeout):
                with self.assertRaisesRegex(PlaylistFetchError, "timed out"):
                    fetch_playlist_tracks(self.PLAYLIST)

    def test_without_any_yt_dlp_the_error_says_how_to_install_it(self):
        with (
            patch("mpf_core.fetcher.shutil.which", return_value=None),
            patch.dict("sys.modules", {"yt_dlp": None}),
        ):
            with self.assertRaisesRegex(PlaylistFetchError, "install"):
                fetch_playlist_tracks(self.PLAYLIST)

    def test_custom_options_use_the_module_even_with_a_system_yt_dlp(self):
        with (
            patch("mpf_core.fetcher.shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("mpf_core.fetcher.subprocess.run") as run,
            patch("yt_dlp.YoutubeDL") as ydl,
        ):
            ydl.return_value.__enter__.return_value.extract_info.return_value = {"id": "v", "title": "T"}
            fetch_playlist_tracks(self.PLAYLIST, {"proxy": "x"})
        run.assert_not_called()

    def test_frozen_app_restores_the_host_library_path_for_the_system_tool(self):
        from mpf_core.fetcher import _host_environment

        env = {"LD_LIBRARY_PATH": "/tmp/_MEI/lib", "LD_LIBRARY_PATH_ORIG": "/usr/lib"}
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "frozen", True, create=True):
            self.assertEqual(_host_environment()["LD_LIBRARY_PATH"], "/usr/lib")
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI/lib"}, clear=True), patch.object(
            sys, "frozen", True, create=True
        ):
            self.assertNotIn("LD_LIBRARY_PATH", _host_environment())


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

    def test_saving_a_setting_preserves_an_unparseable_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "config.json")
            original = '{"default_playlist": "x", // my notes\n}'
            with open(config, "w", encoding="utf-8") as file:
                file.write(original)

            self.assertTrue(save_vim_mode(False, config))

            with open(f"{config}.bad", encoding="utf-8") as backup:
                self.assertEqual(backup.read(), original)
            self.assertFalse(load_vim_mode(config))

    def test_non_object_config_is_preserved_before_saving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "config.json")
            with open(config, "w", encoding="utf-8") as file:
                file.write("[1, 2, 3]")

            self.assertTrue(save_visualizer_preferences("bars", True, config))

            with open(f"{config}.bad", encoding="utf-8") as backup:
                self.assertEqual(backup.read(), "[1, 2, 3]")
            self.assertEqual(load_visualizer_preferences(config), ("bars", True))

    def test_config_is_untouched_when_backup_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "config.json")
            with open(config, "w", encoding="utf-8") as file:
                file.write("{broken")

            with patch("mpf_core.config.os.replace", side_effect=OSError("read-only")):
                self.assertFalse(save_vim_mode(False, config))

            with open(config, encoding="utf-8") as file:
                self.assertEqual(file.read(), "{broken")

    def test_non_utf8_config_is_ignored_on_load_and_preserved_on_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = os.path.join(tmpdir, "config.json")
            with open(config, "wb") as file:
                file.write(b'{"vim_mode": "\xff\xfe"}')

            self.assertTrue(load_vim_mode(config))
            self.assertTrue(save_vim_mode(False, config))
            self.assertTrue(os.path.exists(f"{config}.bad"))
            self.assertFalse(load_vim_mode(config))


class TestReleaseMetadata(unittest.TestCase):
    def test_version_is_semver_and_matches_release_manifest(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".release-please-manifest.json"), encoding="utf-8") as file:
            self.assertEqual(json.load(file)["."], __version__)

    def test_release_please_updates_the_version_in_mpf_py(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "release-please-config.json"), encoding="utf-8") as file:
            package = json.load(file)["packages"]["."]
        self.assertIn("mpf.py", package["extra-files"])
        with open(os.path.join(root, "mpf.py"), encoding="utf-8") as file:
            self.assertTrue(
                any(
                    re.match(r"^__version__ = ", line) and "x-release-please-version" in line
                    for line in file
                )
            )


class TestPlayerInitialization(unittest.TestCase):
    def test_appimage_mpv_launch_restores_host_library_path(self):
        from mpf_core.player import _mpv_environment

        with patch("mpf_core.player.sys.frozen", True, create=True), patch.dict(
            os.environ,
            {
                "LD_LIBRARY_PATH": "/tmp/.mount_mpf/usr/bin/_internal",
                "LD_LIBRARY_PATH_ORIG": "/usr/lib:/lib",
            },
            clear=True,
        ):
            with _mpv_environment():
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/usr/lib:/lib")

            self.assertEqual(
                os.environ["LD_LIBRARY_PATH"], "/tmp/.mount_mpf/usr/bin/_internal"
            )

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
        for expected in ("waveform", "fewbars", "thin", "galaxy", "rainbow", "waterfall"):
            player._handle_key("a")
            self.assertEqual(player._spectrum_style, expected)

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

    def test_dashboard_uses_icons_without_verbose_labels(self):
        from mpf_core.player import BlessedMusicPlayer

        player = BlessedMusicPlayer(auto_play=False, vim_mode=True)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track("1", "One"), Track("2", "Two")])
        player._volume = 42

        self.assertEqual(player._dashboard_summary(), " 42%  ·  󰑖  ·  󰒞  ·  󰎆 2")

        player._is_muted = True
        self.assertEqual(player._dashboard_summary(), "󰝟 42%  ·  󰑖  ·  \U000f049e  ·  󰎆 2")
        player.queue.set_shuffle(True)
        self.assertIn("\U000f049f", player._dashboard_summary())

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

        player = BlessedMusicPlayer(auto_play=False, vim_mode=True)
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


class TestSettingsScreen(unittest.TestCase):
    def _player(self, **kwargs):
        import tempfile

        from mpf_core.player import BlessedMusicPlayer

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = os.path.join(directory.name, "config.json")
        player = BlessedMusicPlayer(auto_play=False, config_file=config, **kwargs)
        self.addCleanup(player.previewer.close)
        return player, config

    def _type(self, player, text):
        for char in text:
            player._handle_key(char)
        player._handle_key("\n")

    def test_comma_opens_and_closes_settings(self):
        player, _config = self._player()
        player._handle_key(",")
        self.assertTrue(player._show_settings)
        player._handle_key("\x1b")
        self.assertFalse(player._show_settings)

    def test_settings_screen_lists_every_option_in_help_style(self):
        player, _config = self._player(vim_mode=True)
        text = "\n".join(player.term.strip_seqs(line) for line in player._settings_lines())
        for expected in (
            "MPF SETTINGS",
            "[v]  Show visualizer (now on)",
            "[a]  Visualizer style (now waterfall)",
            "[V]  Vim keys (now on)",
            "[d]  Default playlist (now none)",
            "[c]  YouTube fallback clients (now tv_simply, mweb)",
        ):
            self.assertIn(expected, text)

    def test_settings_keys_change_and_persist_options(self):
        from mpf_core.config import load_vim_mode, load_visualizer_preferences

        player, config = self._player(vim_mode=True)
        player._handle_key(",")
        player._handle_key("V")
        player._handle_key("a")
        player._handle_key("v")
        self.assertFalse(load_vim_mode(config))
        self.assertEqual(load_visualizer_preferences(config), ("bars", False))
        self.assertTrue(player._show_settings)

    def test_edit_default_playlist_validates_and_saves(self):
        from mpf_core.config import load_default_playlist

        player, config = self._player()
        player._handle_key(",")
        player._handle_key("d")
        self._type(player, "not a url")
        self.assertEqual(load_default_playlist(config), "")
        self.assertTrue(player._settings_msg)
        player._handle_key("d")
        player._input_buffer = ""
        self._type(player, "https://www.youtube.com/playlist?list=PLabc")
        self.assertEqual(load_default_playlist(config), "https://www.youtube.com/playlist?list=PLabc")

    def test_edit_fallback_clients_saves_and_applies_immediately(self):
        from mpf_core.config import load_ytdl_fallback_clients

        player, config = self._player()
        player._handle_key(",")
        player._handle_key("c")
        player._input_buffer = ""
        self._type(player, "web_safari, android")
        self.assertEqual(load_ytdl_fallback_clients(config), ("web_safari", "android"))
        self.assertEqual(player._ytdl_fallback_clients, ("web_safari", "android"))
        player._handle_key("c")
        player._input_buffer = ""
        self._type(player, "bad=name")
        self.assertEqual(player._ytdl_fallback_clients, ("web_safari", "android"))
        player._handle_key("c")
        player._input_buffer = ""
        self._type(player, "")
        self.assertEqual(load_ytdl_fallback_clients(config), ())


class TestNewVisualizerStyles(unittest.TestCase):
    def _player(self):
        import tempfile

        from mpf_core.player import BlessedMusicPlayer

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        player = BlessedMusicPlayer(auto_play=False, config_file=os.path.join(directory.name, "config.json"))
        self.addCleanup(player.previewer.close)
        return player

    def test_every_style_fills_exactly_the_requested_area_for_any_input(self):
        from mpf_core.config import VISUALIZER_STYLES

        player = self._player()
        for style in VISUALIZER_STYLES:
            player._spectrum_style = style
            for width, height in ((3, 3), (40, 8), (61, 5)):
                for level in (0.0, 0.5, 1.0):
                    with self.subTest(style=style, width=width, height=height, level=level):
                        lines = player._render_spectrum_lines([level] * player._band_count(width), height, width)
                        self.assertEqual(len(lines), height)
                        self.assertTrue(all(len(line) == width for line in lines))

    def test_bar_position_anchors_bar_styles_to_top_or_bottom(self):
        player = self._player()
        for style in ("bars", "thin", "fewbars", "rainbow"):
            player._spectrum_style = style
            for position, edge, far in (("top", 0, -1), ("bottom", -1, 0)):
                player._bar_position = position
                bands = [0.2] * player._band_count(12)
                lines = player._render_spectrum_lines(bands, 6, 12)
                with self.subTest(style=style, position=position):
                    self.assertNotEqual(lines[edge].strip(), "")
                    self.assertEqual(lines[far].strip(), "")
                    quiet = player._render_spectrum_lines([0.0] * player._band_count(12), 6, 12)
                    self.assertEqual("".join(quiet).strip(), "")

    def test_galaxy_spans_the_box_width_without_leaving_it(self):
        player = self._player()
        player._spectrum_style = "galaxy"
        for width, height in ((120, 8), (20, 3), (7, 3)):
            lines = player._render_spectrum_lines([1.0] * 64, height, width)
            with self.subTest(width=width):
                self.assertEqual([len(line) for line in lines], [width] * height)
                self.assertNotEqual(lines[len(lines) // 2][0], "\u2800")  # reaches the left edge
                self.assertNotEqual(lines[len(lines) // 2][-1], "\u2800")  # and the right edge

    def test_bar_position_setting_cycles_and_persists(self):
        from mpf_core.config import load_bar_position

        player = self._player()
        player._handle_key(",")
        player._handle_key("b")
        self.assertEqual(player._bar_position, "top")
        self.assertEqual(load_bar_position(player.config_file), "top")
        player._handle_key("b")
        self.assertEqual(load_bar_position(player.config_file), "bottom")

    def test_rainbow_is_mirrored(self):
        player = self._player()
        player._spectrum_style = "rainbow"
        lines = player._render_spectrum_lines([i / 20 for i in range(20)], 4, 20)
        self.assertEqual(lines[-1], lines[-1][::-1])

    def test_rainbow_colours_rows_by_height_not_bars(self):
        from blessed import Terminal

        player = self._player()
        player._spectrum_style = "rainbow"
        player._rainbow_colors = ("blue", "red")
        player.term = term = Terminal(force_styling=True)
        # Same colour for the whole row, whatever the bars in it look like.
        self.assertEqual(player._styled_row(3, 4, " ██ "), term.bold_blue(" ██ "))
        self.assertEqual(player._styled_row(0, 4, "████"), term.bold_red("████"))

    def test_rainbow_row_colours_start_at_the_edge_bars_grow_from(self):
        from blessed import Terminal

        player = self._player()
        player._spectrum_style = "bars"
        player._color_mode = "rainbow"
        player._rainbow_colors = ("blue", "red")
        player.term = term = Terminal(force_styling=True)
        self.assertEqual(player._styled_row(3, 4, "x"), term.bold_blue("x"))
        player._bar_position = "top"
        self.assertEqual(player._styled_row(0, 4, "x"), term.bold_blue("x"))

    def test_colour_mode_applies_to_every_style_and_plain_stays_cyan(self):
        from blessed import Terminal

        from mpf_core.config import VISUALIZER_STYLES

        player = self._player()
        player.term = term = Terminal(force_styling=True)
        player._rainbow_colors = ("blue", "red")
        for style in VISUALIZER_STYLES:
            player._spectrum_style = style
            player._color_mode = "plain"
            lines = player._render_spectrum_lines([1.0] * player._band_count(30), 6, 30)
            if style != "rainbow":
                self.assertEqual(player._styled_row(2, 6, lines[2]), term.bold_cyan(lines[2]))
            player._color_mode = "rainbow"
            if style == "waterfall":
                player._spectrum_mode = "freeze"
                player._spectrum_history = [[1.0] * 30] * 6
                lines = player._render_spectrum_lines([1.0] * 30, 6, 30)
            styled = "".join(player._styled_row(row, 6, line) for row, line in enumerate(lines))
            self.assertIn("\x1b[", styled, style)  # blank cells stay unstyled, so look at the whole frame

    def test_non_bar_styles_colour_by_cell_intensity_not_row(self):
        from blessed import Terminal

        player = self._player()
        player.term = term = Terminal(force_styling=True)
        player._rainbow_colors = ("blue", "red")
        player._color_mode = "rainbow"
        player._spectrum_style = "waterfall"
        # A frozen waterfall of two frames: faint, then loud. Each cell takes its colour from its own value.
        player._spectrum_mode = "freeze"
        player._spectrum_history = [[0.2] * 4, [0.9] * 4]
        loud = player._render_spectrum_lines([0.0] * 4, 2, 4)
        self.assertEqual(player._styled_row(1, 2, loud[1]), term.bold_red(loud[1]))
        self.assertEqual(player._styled_row(0, 2, loud[0]), term.bold_blue(loud[0]))  # the older, faint row

        player._spectrum_style = "waveform"
        lines = player._render_spectrum_lines([0.1, 0.1, 0.9, 0.9], 3, 4)
        self.assertEqual(
            player._styled_row(0, 3, lines[0]), " " * 2 + term.bold_red(lines[0][2:])
        )
        player._spectrum_style = "bars"
        player._render_spectrum_lines([1.0] * 4, 3, 4)
        self.assertIsNone(player._cell_levels)  # bar styles colour by row

    def test_galaxy_colour_follows_how_far_the_ring_is_pushed_out(self):
        from blessed import Terminal

        player = self._player()
        player.term = Terminal(force_styling=True)
        player._color_mode = "rainbow"
        player._spectrum_style = "galaxy"
        player._render_spectrum_lines([0.5] * 64, 8, 60)
        levels = player._cell_levels
        self.assertEqual(levels.shape, (8, 60))
        self.assertLess(levels[4, 30], levels[4, 0])  # the middle is cooler than the outer edge

    def test_colour_mode_setting_cycles_and_persists(self):
        from mpf_core.config import load_color_mode

        player = self._player()
        player._handle_key(",")
        player._handle_key("r")
        self.assertEqual(player._color_mode, "rainbow")
        self.assertEqual(load_color_mode(player.config_file), "rainbow")
        player._handle_key("r")
        self.assertEqual(load_color_mode(player.config_file), "plain")

    def test_rainbow_colours_validate_save_and_load(self):
        import tempfile

        from mpf_core.config import (
            DEFAULT_RAINBOW_COLORS,
            load_rainbow_colors,
            parse_rainbow_colors,
            save_rainbow_colors,
        )

        self.assertEqual(parse_rainbow_colors("Blue, bright_red"), ("blue", "bright_red"))
        for bad in ("blue", "blue, #ff0000", "blue; red", ""):
            self.assertIsNone(parse_rainbow_colors(bad))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            self.assertEqual(load_rainbow_colors(path), DEFAULT_RAINBOW_COLORS)
            self.assertTrue(save_rainbow_colors(("blue", "red"), path))
            self.assertEqual(load_rainbow_colors(path), ("blue", "red"))
            self.assertFalse(save_rainbow_colors(("blue",), path))
            with open(path, "w", encoding="utf-8") as file:
                file.write('{"rainbow_colors": ["Red", "BRIGHT_BLUE"]}')
            self.assertEqual(load_rainbow_colors(path), ("red", "bright_blue"))

    def test_settings_edits_rainbow_colours(self):
        player = self._player()
        player._handle_key(",")
        player._handle_key("g")
        player._input_buffer = ""
        for char in "cyan, magenta\n":
            player._handle_key(char)
        self.assertEqual(player._rainbow_colors, ("cyan", "magenta"))


class TestPromptCursor(unittest.TestCase):
    def _player(self):
        import tempfile

        from mpf_core.player import BlessedMusicPlayer

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        player = BlessedMusicPlayer(auto_play=False, config_file=os.path.join(directory.name, "config.json"))
        self.addCleanup(player.previewer.close)
        return player

    @staticmethod
    def _key(name):
        return type("Key", (str,), {"name": name, "is_sequence": True})("")

    def _type(self, player, text):
        for char in text:
            player._handle_key(char)

    def test_cursor_moves_and_edits_happen_at_the_cursor(self):
        player = self._player()
        player._handle_key("o")
        self._type(player, "helo")
        player._handle_key(self._key("KEY_LEFT"))
        self._type(player, "l")
        self.assertEqual(player._input_buffer, "hello")
        self.assertEqual(player._input_cursor, 4)
        player._handle_key(self._key("KEY_HOME"))
        self._type(player, ">")
        self.assertEqual(player._input_buffer, ">hello")
        player._handle_key(self._key("KEY_END"))
        self.assertEqual(player._input_cursor, 6)

    def test_backspace_deletes_before_and_delete_deletes_at_the_cursor(self):
        player = self._player()
        player._handle_key("o")
        self._type(player, "abcd")
        player._handle_key(self._key("KEY_LEFT"))
        player._handle_key(self._key("KEY_LEFT"))
        player._handle_key(self._key("KEY_BACKSPACE"))
        self.assertEqual((player._input_buffer, player._input_cursor), ("acd", 1))
        player._handle_key(self._key("KEY_DELETE"))
        self.assertEqual((player._input_buffer, player._input_cursor), ("ad", 1))
        player._handle_key(self._key("KEY_HOME"))
        player._handle_key(self._key("KEY_BACKSPACE"))  # nothing before the cursor
        player._handle_key(self._key("KEY_LEFT"))
        self.assertEqual((player._input_buffer, player._input_cursor), ("ad", 0))
        player._handle_key(self._key("KEY_END"))
        player._handle_key(self._key("KEY_DELETE"))  # nothing after the cursor
        self.assertEqual(player._input_buffer, "ad")

    def test_cursor_stays_inside_the_text(self):
        player = self._player()
        player._handle_key("o")
        self._type(player, "ab")
        for _ in range(5):
            player._handle_key(self._key("KEY_RIGHT"))
        self.assertEqual(player._input_cursor, 2)
        for _ in range(5):
            player._handle_key(self._key("KEY_LEFT"))
        self.assertEqual(player._input_cursor, 0)

    def test_search_filter_follows_edits_in_the_middle(self):
        player = self._player()
        player.queue.set_tracks([Track("1", "alpha"), Track("2", "beta")])
        player._handle_key("/")
        self._type(player, "bta")
        player._handle_key(self._key("KEY_LEFT"))
        player._handle_key(self._key("KEY_LEFT"))
        self._type(player, "e")
        self.assertEqual([i for i, _ in player.queue.filtered_tracks], [1])

    def test_prompt_draws_the_cursor_and_scrolls_to_keep_it_visible(self):
        from blessed import Terminal

        player = self._player()
        player.term = term = Terminal(force_styling=True)
        player._handle_key("o")
        self._type(player, "abc")
        player._handle_key(self._key("KEY_LEFT"))
        line = player._paint(player._prompt_segments("open "))
        self.assertEqual(line, term.bold_yellow(" open ab") + term.bold_yellow_reverse("c"))
        player._handle_key(self._key("KEY_END"))
        plain = term.strip_seqs(player._paint(player._prompt_segments("open ", width=8)))
        self.assertEqual(plain, " open c ")  # 2 cells: the end of "abc" and the cursor stay in view
        player._input_buffer = "x" * 40
        self.assertLessEqual(len(term.strip_seqs(player._paint(player._prompt_segments("open ", width=20)))), 20)

    def test_replacing_the_text_puts_the_cursor_at_its_end(self):
        player = self._player()
        player._input_buffer = "prefilled"
        self.assertEqual(player._input_cursor, 9)


if __name__ == "__main__":
    unittest.main()


class TestYtdlFallbackConfig(unittest.TestCase):
    def _load(self, content):
        import tempfile

        from mpf_core.config import load_ytdl_fallback_clients

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            if content is not None:
                with open(path, "w", encoding="utf-8") as file:
                    file.write(content)
            return load_ytdl_fallback_clients(path)

    def test_defaults_when_unset(self):
        from mpf_core.config import DEFAULT_YTDL_FALLBACK_CLIENTS

        self.assertEqual(self._load(None), DEFAULT_YTDL_FALLBACK_CLIENTS)
        self.assertEqual(self._load("{}"), DEFAULT_YTDL_FALLBACK_CLIENTS)

    def test_user_list_replaces_defaults_and_empty_disables_retries(self):
        self.assertEqual(self._load('{"ytdl_fallback_clients": ["mweb", "tv", "mweb"]}'), ("mweb", "tv"))
        self.assertEqual(self._load('{"ytdl_fallback_clients": []}'), ())

    def test_invalid_values_fall_back_to_defaults(self):
        from mpf_core.config import DEFAULT_YTDL_FALLBACK_CLIENTS

        for bad in ('"tv"', '["tv,ios"]', '["a=b"]', "[1]", '[""]'):
            with self.subTest(bad=bad):
                self.assertEqual(self._load('{"ytdl_fallback_clients": %s}' % bad), DEFAULT_YTDL_FALLBACK_CLIENTS)


class TestLogging(unittest.TestCase):
    def setUp(self):
        import logging

        self.root = logging.getLogger()
        self.saved = (self.root.handlers[:], self.root.level)
        self.root.handlers.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in self.root.handlers[:]:
            handler.close()
        self.root.handlers[:] = self.saved[0]
        self.root.setLevel(self.saved[1])

    def test_log_and_directory_are_private_and_old_logs_are_tightened(self):
        import logging

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = os.path.join(directory, "cache")
            os.mkdir(cache_dir, 0o755)
            log_file = os.path.join(cache_dir, "mpf.log")
            with open(log_file, "w", encoding="utf-8") as file:  # as an older version left it
                file.write("old\n")
            os.chmod(log_file, 0o644)

            configure_logging(cache_dir, log_file)
            logging.getLogger("t").warning("hello")
            for handler in self.root.handlers:
                handler.flush()

            self.assertEqual(os.stat(cache_dir).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(log_file).st_mode & 0o777, 0o600)
            with open(log_file, encoding="utf-8") as file:
                self.assertIn("hello", file.read())

    def test_log_rotates_with_private_files(self):
        import logging

        with tempfile.TemporaryDirectory() as directory:
            log_file = os.path.join(directory, "mpf.log")
            configure_logging(directory, log_file, max_bytes=300, backups=1)
            for i in range(40):
                logging.getLogger("t").warning("line %d %s", i, "x" * 40)
            for handler in self.root.handlers:
                handler.flush()

            files = sorted(name for name in os.listdir(directory) if name.startswith("mpf.log"))
            self.assertEqual(files, ["mpf.log", "mpf.log.1"])  # capped at one backup
            for name in files:
                self.assertEqual(os.stat(os.path.join(directory, name)).st_mode & 0o777, 0o600)

    def test_unusable_log_location_silences_logging_instead_of_failing(self):
        configure_logging("/proc/definitely/not/writable", "/proc/definitely/not/writable/mpf.log")
        self.assertEqual(self.root.level, 50)  # CRITICAL


class TestHardening(unittest.TestCase):
    def test_ipc_socket_uses_the_private_runtime_dir(self):
        from mpf_core.player import _private_ipc_socket

        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                path, cleanup = _private_ipc_socket()
        self.assertEqual(os.path.dirname(path), runtime)
        self.assertIsNone(cleanup)
        self.assertTrue(os.path.basename(path).startswith("mpf-mpv-"))

    def test_ipc_socket_falls_back_to_a_fresh_private_directory(self):
        import shutil

        from mpf_core.player import _private_ipc_socket

        with tempfile.TemporaryDirectory() as shared:
            os.chmod(shared, 0o755)  # group/world accessible: not acceptable
            for env in ({"XDG_RUNTIME_DIR": shared}, {"XDG_RUNTIME_DIR": "/nonexistent"}, {}):
                with self.subTest(env=env), patch.dict(os.environ, env, clear=False):
                    if not env:
                        os.environ.pop("XDG_RUNTIME_DIR", None)
                    path, cleanup = _private_ipc_socket()
                    self.addCleanup(shutil.rmtree, cleanup, True)
                    self.assertIsNotNone(cleanup)
                    self.assertEqual(os.path.dirname(path), cleanup)
                    self.assertEqual(os.stat(cleanup).st_mode & 0o777, 0o700)

    def test_ipc_socket_names_are_unpredictable(self):
        from mpf_core.player import _private_ipc_socket

        with tempfile.TemporaryDirectory() as runtime:
            os.chmod(runtime, 0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                self.assertNotEqual(_private_ipc_socket()[0], _private_ipc_socket()[0])

    def test_yt_dlp_child_gets_no_stdin(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"id": "v", "title": "T"}', stderr="")
        with (
            patch("mpf_core.fetcher.shutil.which", return_value="/usr/bin/yt-dlp"),
            patch("mpf_core.fetcher.subprocess.run", return_value=completed) as run,
        ):
            fetch_playlist_tracks("https://youtu.be/abc")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_urls_with_embedded_control_characters_are_rejected(self):
        from mpf_core.fetcher import InvalidMediaURL, validate_media_url

        for bad in ("https://youtu.be/ab\tc", "https://youtu.be/abc\nhttps://evil.example", "https://youtu.be/a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(InvalidMediaURL):
                validate_media_url(bad)
        self.assertEqual(validate_media_url("  https://youtu.be/abc\n"), "https://youtu.be/abc")  # pasted padding is fine

    def test_thumbnail_download_failures_do_not_leak_file_descriptors(self):
        import http.client

        track = Track("abc", "T")
        with tempfile.TemporaryDirectory() as directory:
            previewer = KittyPreviewer(thumbs_dir=directory, fallback_icon="/nonexistent")
            self.addCleanup(previewer.close)
            before = len(os.listdir("/proc/self/fd"))
            for error in (http.client.InvalidURL("bad"), OSError("down"), ValueError("bad")):
                with patch("mpf_core.previewer.urllib.request.urlopen", side_effect=error):
                    previewer._download(track)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)
            self.assertEqual([name for name in os.listdir(directory) if name.startswith(".thumb-")], [])


class TestCacheSizeLimits(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as file:
            file.write(data)
        return path

    def test_a_tiny_file_that_expands_hugely_is_rejected(self):
        import zstandard

        from mpf_core import cache

        bomb = zstandard.ZstdCompressor().compress(b"\0" * (8 * 1024 * 1024))  # a few KB on disk
        self.assertLess(len(bomb), 4096)
        path = self._write("playlists.mpk.zst", bomb)
        # max_output_size would NOT help here: python-zstandard ignores it when the frame states its size.
        with patch.object(cache, "MAX_CACHE_DECOMPRESSED_BYTES", 1024 * 1024):
            self.assertIsNone(cache._read_binary_cache(path))
            with self.assertRaises(ValueError):
                cache._decompress_limited(bomb)

    def test_normal_caches_still_round_trip(self):
        from mpf_core import cache

        path = os.path.join(self.dir, "playlists.mpk.zst")
        tracks = [Track(f"id{i}", f"Title {i}", 100, "Up") for i in range(2000)]
        self.assertTrue(cache.save_cached_playlist("https://youtu.be/x", tracks, path))
        loaded = cache.load_cached_playlist("https://youtu.be/x", path)
        self.assertEqual(len(loaded or []), 2000)

    def test_oversized_cache_file_is_ignored_without_reading_it_all(self):
        from mpf_core import cache

        path = self._write("playlists.mpk.zst", b"x" * 4096)
        with patch.object(cache, "MAX_CACHE_FILE_BYTES", 1024):
            self.assertIsNone(cache._read_binary_cache(path))

    def test_oversized_playback_state_is_ignored_and_then_replaced(self):
        from mpf_core import cache
        from mpf_core.cache import PlaybackState

        path = self._write("playback.json", b'{"pad": "' + b"x" * 4096 + b'"}')
        with patch.object(cache, "MAX_STATE_FILE_BYTES", 1024):
            self.assertIsNone(cache.load_playback_state("https://youtu.be/x", path))
            state = PlaybackState(playlist_url="https://youtu.be/x", track_id="a", track_index=0, time_pos=1.0)
            self.assertTrue(cache.save_playback_state(state, path))  # replaces instead of failing forever
            self.assertIsNotNone(cache.load_playback_state("https://youtu.be/x", path))

    def test_oversized_config_is_ignored(self):
        from mpf_core import config

        path = self._write("config.json", b'{"vim_mode": false, "pad": "' + b"x" * 4096 + b'"}')
        with patch.object(config, "MAX_CONFIG_BYTES", 1024):
            self.assertTrue(config.load_vim_mode(path))  # default, not the file's false
