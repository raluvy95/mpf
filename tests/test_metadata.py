"""Cleaning of untrusted track text."""

import unittest
from unittest.mock import MagicMock, patch

from mpf_core.fetcher import fetch_playlist_tracks
from mpf_core.metadata import clean_text
from mpf_core.models import Track
from mpf_core.player import BlessedMusicPlayer

OSC52 = "\x1b]52;c;ZXZpbA==\x07"  # would overwrite the clipboard in a terminal that honours OSC 52


class TestCleanText(unittest.TestCase):
    def test_escape_sequences_lose_their_control_characters(self):
        self.assertEqual(clean_text(f"Song {OSC52}name"), "Song ]52;c;ZXZpbA==name")
        self.assertNotIn("\x1b", clean_text("\x1b[2J\x1b[31mred"))
        self.assertNotIn("\x07", clean_text("bell\x07"))

    def test_nul_c1_del_bidi_and_surrogates_are_removed(self):
        self.assertEqual(clean_text("a\x00b\x7fc\x9bd‮e⁦f\ud800g"), "abcdefg")

    def test_whitespace_controls_become_spaces(self):
        self.assertEqual(clean_text("one\ttwo\nthree\r\nfour five"), "one two three four five")

    def test_normal_unicode_is_kept(self):
        for text in ("Björk – Jóga", "日本語のタイトル", "👨‍👩‍👧 family", "Ünïcödé"):
            with self.subTest(text=text):
                self.assertEqual(clean_text(text), text)

    def test_length_is_capped_and_non_strings_are_empty(self):
        self.assertEqual(clean_text("x" * 1000, 50), "x" * 50)
        for value in (None, 5, b"bytes", ["x"]):
            with self.subTest(value=value):
                self.assertEqual(clean_text(value), "")


class TestIngestionPoints(unittest.TestCase):
    def test_cache_entries_are_cleaned_on_load(self):
        track = Track.from_cache_tuple((f"id{OSC52}", f"T{OSC52}itle", 10, f"Up{OSC52}"))
        for text in (track.id, track.title, track.uploader):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)

    @patch("mpf_core.fetcher.shutil.which", return_value=None)
    @patch("yt_dlp.YoutubeDL")
    def test_fetched_entries_are_cleaned(self, ydl, _which):
        ydl.return_value.__enter__.return_value.extract_info.return_value = {
            "entries": [{"id": f"abc{OSC52}", "title": f"Hi{OSC52}", "channel": f"Ch\x00an{OSC52}", "duration": 5}]
        }
        (track,) = fetch_playlist_tracks("https://www.youtube.com/playlist?list=x")
        for text in (track.id, track.title, track.uploader):
            self.assertFalse(set(text) & set("\x1b\x07\x00"), text)

    def test_mpv_error_lines_are_cleaned_and_capped(self):
        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)
        player._on_mpv_log("error", "ffmpeg", f"bad {OSC52}" + "y" * 1000)
        line = player._mpv_errors[-1]
        self.assertNotIn("\x1b", line)
        self.assertLessEqual(len(line), 300)

    def test_mpv_end_file_error_text_is_cleaned_in_the_status_line(self):
        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track("a", "A"), Track("b", "B")])
        player.mpv = MagicMock()
        player.previewer.request = MagicMock()
        player._ytdl_plain_retries = 0
        player._ytdl_fallback_clients = ()
        player._handle_mpv_end_file({"reason": "error", "error": f"oops{OSC52}"})
        self.assertNotIn("\x1b", player._status_msg)
