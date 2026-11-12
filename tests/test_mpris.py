"""MPRIS metadata and player-state mapping."""

import unittest

from mpf_core.models import RepeatMode, Track
from mpf_core.mpris import NO_TRACK, build_metadata
from mpf_core.player import BlessedMusicPlayer


class TestMpris(unittest.TestCase):
    def test_metadata_for_track(self):
        meta = build_metadata(Track("ab-c", "Song", 90, "Artist"), 0)
        self.assertEqual(meta["mpris:trackid"].value, "/org/mpf/track/ab_c")  # '-' is not valid in a D-Bus path
        self.assertEqual(meta["xesam:title"].value, "Song")
        self.assertEqual(meta["xesam:artist"].value, ["Artist"])
        self.assertEqual(meta["mpris:length"].value, 90_000_000)

    def test_metadata_prefers_live_duration_and_handles_no_track(self):
        self.assertEqual(build_metadata(Track("a", "S", 90), 120.0)["mpris:length"].value, 120_000_000)
        self.assertEqual(build_metadata(None, 0)["mpris:trackid"].value, NO_TRACK)

    def test_art_prefers_local_file(self):
        track = Track("a", "S", 10)
        self.assertEqual(build_metadata(track, 0)["mpris:artUrl"].value, track.thumbnail)
        self.assertEqual(build_metadata(track, 0, "/tmp/a b.jpg")["mpris:artUrl"].value, "file:///tmp/a%20b.jpg")

    def test_loop_status_round_trip(self):
        player = BlessedMusicPlayer(auto_play=False)
        for status in ("None", "Playlist", "Track"):
            player.set_loop_status(status)
            self.assertEqual(player.mpris_loop_status(), status)
        player.set_loop_status("bogus")
        self.assertEqual(player.queue.repeat_mode, RepeatMode.ONE)

    def test_playback_status(self):
        player = BlessedMusicPlayer(auto_play=False)
        self.assertEqual(player.mpris_playback_status(), "Stopped")
        player.queue.set_tracks([Track("a", "S", 10)])
        player._mpv_idle = False
        self.assertEqual(player.mpris_playback_status(), "Playing")
        player._is_paused = True
        self.assertEqual(player.mpris_playback_status(), "Paused")
