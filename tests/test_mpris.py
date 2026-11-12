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

    def test_metadata_uses_the_parsed_artist_and_title(self):
        meta = build_metadata(Track("a", "Halsey - Colors (Official Video)", 238, "HalseyVEVO"), 0)
        self.assertEqual(meta["xesam:artist"].value, ["Halsey"])
        self.assertEqual(meta["xesam:title"].value, "Colors")
        topic = build_metadata(Track("b", "Song", 10, "Band - Topic"), 0)
        self.assertEqual(topic["xesam:artist"].value, ["Band"])

    def test_hostile_text_never_reaches_dbus(self):
        track = Track("a", "x", 10, "y")
        track.title = "Evil\x1b]52;c;ZXZpbA==\x07 - So\x00ng\ud800"  # bypasses ingestion on purpose
        meta = build_metadata(track, 0)
        text = meta["xesam:title"].value + "".join(meta["xesam:artist"].value)
        self.assertFalse(set(text) & set("\x1b\x07\x00\ud800"))
        # and the message really marshals
        from dbus_fast import Message

        Message(path="/x", interface="a.b", member="M", signature="a{sv}", body=[meta])._marshall(False)

    def test_failed_emit_is_retried_on_the_next_sync(self):
        from unittest.mock import MagicMock

        from mpf_core.mpris import MprisService

        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track("a", "Artist - Song", 10, "x")])
        service = MprisService(player)
        service._bus = MagicMock()
        service._iface = MagicMock()
        service._iface.emit_properties_changed.side_effect = [RuntimeError("boom"), None]

        service.sync()  # the emit fails: nothing may be remembered as sent
        service.sync()  # so the same change is sent again
        calls = service._iface.emit_properties_changed.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("Metadata", calls[1].args[0])
        service.sync()  # now in sync: nothing new to send
        self.assertEqual(service._iface.emit_properties_changed.call_count, 2)

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

    def test_shuffle_property_drives_the_queue(self):
        player = BlessedMusicPlayer(auto_play=False)
        player.queue.set_tracks([Track(str(i), f"S{i}", 10) for i in range(5)])
        player.set_shuffle(True)
        self.assertTrue(player.queue.shuffle_enabled)
        player.set_shuffle(False)
        self.assertEqual([t.id for t in player.queue.tracks], [str(i) for i in range(5)])
