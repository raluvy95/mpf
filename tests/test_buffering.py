"""Adaptive read-ahead and next-track prefetch."""

import unittest
from unittest.mock import MagicMock

from mpf_core.buffering import AdaptiveReadahead
from mpf_core.models import RepeatMode, Track, TrackQueue
from mpf_core.player import PREFETCH_LEAD_SECONDS, BlessedMusicPlayer

LEVELS = (10, 20, 45, 90)


class TestAdaptiveReadahead(unittest.TestCase):
    def _ra(self):
        return AdaptiveReadahead(LEVELS, cooldown=10.0, stable_seconds=300.0)

    def test_starts_at_the_lowest_level(self):
        self.assertEqual(self._ra().secs, 10)

    def test_each_stall_steps_up_and_stops_at_the_top(self):
        ra = self._ra()
        seen = []
        for i in range(6):
            ra.on_stall(i * 100.0)
            seen.append(ra.secs)
        self.assertEqual(seen, [20, 45, 90, 90, 90, 90])

    def test_stalls_inside_the_cooldown_count_once(self):
        ra = self._ra()
        self.assertTrue(ra.on_stall(100.0))
        self.assertFalse(ra.on_stall(105.0))
        self.assertEqual(ra.secs, 20)
        self.assertTrue(ra.on_stall(111.0))
        self.assertEqual(ra.secs, 45)

    def test_steps_down_one_level_per_stable_period(self):
        ra = self._ra()
        ra.on_stall(0.0)
        ra.on_stall(20.0)
        self.assertEqual(ra.secs, 45)
        self.assertFalse(ra.tick(319.0))
        self.assertTrue(ra.tick(321.0))
        self.assertEqual(ra.secs, 20)
        self.assertFalse(ra.tick(400.0))  # the timer restarts after each step
        self.assertTrue(ra.tick(621.0))
        self.assertEqual(ra.secs, 10)
        self.assertFalse(ra.tick(5000.0))  # never below the lowest level

    def test_a_new_stall_restarts_the_stable_timer(self):
        ra = self._ra()
        ra.on_stall(0.0)
        ra.on_stall(200.0)
        self.assertFalse(ra.tick(350.0))  # only 150 s since the last stall
        self.assertEqual(ra.secs, 45)


class TestTuneBuffering(unittest.TestCase):
    def setUp(self):
        self.player = BlessedMusicPlayer("https://www.youtube.com/playlist?list=x", auto_play=False)
        self.addCleanup(self.player.previewer.close)
        self.player.mpv = MagicMock()

    def test_a_stall_raises_the_readahead_once_per_stall(self):
        self.player._tune_buffering(True)
        self.player._tune_buffering(True)  # still the same stall
        self.assertEqual(self.player.mpv.demuxer_readahead_secs, 20)
        self.player._tune_buffering(False)
        self.assertEqual(self.player._readahead.secs, 20)

    def test_no_stall_leaves_mpv_alone(self):
        self.player.mpv = MagicMock(spec=[])
        self.player._tune_buffering(False)
        self.assertFalse(hasattr(self.player.mpv, "demuxer_readahead_secs"))


class TestPrefetch(unittest.TestCase):
    def setUp(self):
        self.player = BlessedMusicPlayer("https://www.youtube.com/playlist?list=x", auto_play=False)
        self.addCleanup(self.player.previewer.close)
        self.player.queue = TrackQueue([Track("a", "A"), Track("b", "B"), Track("c", "C")])
        self.player.mpv = MagicMock()
        self.player.previewer = MagicMock()
        self.player._show_visualizer = False
        self.player.queue.current_idx = 0
        self.player._duration = 200.0
        self.player._time_pos = 200.0 - PREFETCH_LEAD_SECONDS + 1  # inside the lead window

    def _appended(self):
        return [c.args for c in self.player.mpv.command.call_args_list if c.args[0] == "loadfile"]

    def test_nothing_is_prefetched_early_in_the_track(self):
        self.player._time_pos = 50.0
        self.player._sync_prefetch()
        self.assertEqual(self._appended(), [])

    def test_prefetches_the_next_track_near_the_end_once(self):
        self.player._sync_prefetch()
        self.player._sync_prefetch()
        self.assertEqual(self._appended(), [("loadfile", Track("b", "B").url, "append")])
        self.assertEqual(self.player._prefetched_id, "b")

    def test_repeat_one_never_prefetches(self):
        self.player.queue.repeat_mode = RepeatMode.ONE
        self.player._sync_prefetch()
        self.assertEqual(self._appended(), [])

    def test_a_one_track_loop_never_prefetches_itself(self):
        self.player.queue = TrackQueue([Track("a", "A")])
        self.player.queue.repeat_mode = RepeatMode.ALL
        self.player._sync_prefetch()
        self.assertEqual(self._appended(), [])

    def test_last_track_with_repeat_off_has_nothing_to_prefetch(self):
        self.player.queue.current_idx = 2
        self.player.queue.repeat_mode = RepeatMode.OFF
        self.player._sync_prefetch()
        self.assertEqual(self._appended(), [])

    def test_failed_tracks_are_not_prefetched(self):
        self.player._failed_track_ids.add("b")
        self.player._sync_prefetch()
        self.assertEqual(self._appended(), [])

    def test_toggling_shuffle_drops_a_stale_prefetch(self):
        self.player._sync_prefetch()
        self.assertEqual(self.player._prefetched_id, "b")
        self.player.queue.tracks = [Track("a", "A"), Track("c", "C"), Track("b", "B")]  # shuffled order
        self.player._sync_prefetch()
        commands = [c.args[0] for c in self.player.mpv.command.call_args_list]
        self.assertEqual(commands, ["loadfile", "playlist-clear", "loadfile"])
        self.assertEqual(self.player._prefetched_id, "c")

    def test_switching_to_repeat_one_clears_the_prefetch(self):
        self.player._sync_prefetch()
        self.player.queue.repeat_mode = RepeatMode.ONE
        self.player._sync_prefetch()
        self.player.mpv.command.assert_called_with("playlist-clear")
        self.assertIsNone(self.player._prefetched_id)

    def test_eof_on_a_prefetched_track_does_not_reload_it(self):
        self.player._sync_prefetch()
        self.player.mpv.play.reset_mock()
        self.player._handle_mpv_end_file({"reason": "eof"})
        self.player.mpv.play.assert_not_called()
        self.assertEqual(self.player.queue.current_track.id, "b")
        self.assertIsNone(self.player._prefetched_id)

    def test_eof_on_a_different_track_plays_it_normally(self):
        self.player._sync_prefetch()
        self.player.queue.tracks = [Track("a", "A"), Track("c", "C"), Track("b", "B")]
        self.player.mpv.play.reset_mock()
        self.player._handle_mpv_end_file({"reason": "eof"})
        self.player.mpv.play.assert_called_once_with(Track("c", "C").url)

    def test_an_error_never_trusts_the_prefetch(self):
        self.player._ytdl_plain_retries = 0
        self.player._ytdl_fallback_clients = ()
        self.player._sync_prefetch()
        self.player.mpv.play.reset_mock()
        self.player._handle_mpv_end_file({"reason": "error"})
        self.player.mpv.play.assert_called_once_with(Track("b", "B").url)

    def test_manual_play_forgets_the_prefetch(self):
        self.player._sync_prefetch()
        self.player._play_index(2)
        self.assertIsNone(self.player._prefetched_id)
