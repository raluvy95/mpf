"""Cleaning of untrusted track text."""

import unittest
from unittest.mock import MagicMock, patch

from mpf_core.fetcher import fetch_playlist_tracks
from mpf_core.metadata import clean_text, parse_track, redact_urls, strip_noise
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


class TestRedactUrls(unittest.TestCase):
    def test_signed_stream_urls_lose_their_query(self):
        line = "stream: Failed to open https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&ip=2a02%3Af700&sig=AB"
        out = redact_urls(line)
        self.assertEqual(out, "stream: Failed to open https://rr1---sn-x.googlevideo.com/videoplayback?...")
        self.assertNotIn("2a02", out)
        self.assertNotIn("sig=", out)

    def test_youtube_page_and_thumbnail_urls_keep_their_ids(self):
        for url in ("https://www.youtube.com/watch?v=abc123&list=PL1", "https://youtu.be/abc?t=5",
                    "https://i.ytimg.com/vi/abc/hqdefault.jpg?sqp=x"):
            with self.subTest(url=url):
                self.assertEqual(redact_urls(f"see {url} now"), f"see {url} now")

    def test_credentials_in_urls_are_dropped_and_plain_text_is_untouched(self):
        self.assertEqual(redact_urls("http://user:pw@evil.example/p?token=S#f"), "http://evil.example/p?...")
        self.assertEqual(redact_urls("no url, just words"), "no url, just words")
        self.assertEqual(redact_urls("https://a.example/x?q=1 and https://b.example/y"), "https://a.example/x?... and https://b.example/y")

    def test_mpv_log_lines_are_redacted_before_being_kept(self):
        player = BlessedMusicPlayer(auto_play=False)
        self.addCleanup(player.previewer.close)
        player._on_mpv_log("error", "ffmpeg", "https: HTTP error 403 Forbidden https://rr1.googlevideo.com/v?ip=9.9.9.9&sig=Q")
        self.assertNotIn("9.9.9.9", player._mpv_errors_text())
        self.assertIn("403 Forbidden", player._mpv_errors_text())


class TestParseTrack(unittest.TestCase):
    def check(self, title, channel, expected):
        self.assertEqual(parse_track(Track("id", title, 100, channel)), expected)

    def test_artist_dash_title_splits_on_the_first_separator(self):
        self.check("Halsey - Colors (Audien Remix)", "Halsey", ("Halsey", "Colors (Audien Remix)"))
        self.check("A - B - C", "Chan", ("A", "B - C"))

    def test_dash_variants_split(self):
        for dash in ("-", "–", "—", "‒"):
            with self.subTest(dash=dash):
                self.check(f"Beyoncé {dash} Halo", "Chan", ("Beyoncé", "Halo"))

    def test_hyphen_without_spaces_is_part_of_a_name(self):
        self.check("Jay-Z", "Jay-Z", ("Jay-Z", "Jay-Z"))
        self.check("Hi-Fi Song", "Chan", ("Chan", "Hi-Fi Song"))

    def test_upload_noise_is_removed_but_song_tags_are_kept(self):
        for noise in ("(Official Video)", "[Official Music Video]", "(Lyrics)", "[HD]", "(Official Audio)",
                      "(Visualizer)", "(Official Lyric Video)"):
            with self.subTest(noise=noise):
                self.check(f"Daft Punk - Get Lucky {noise}", "x", ("Daft Punk", "Get Lucky"))
        self.check("Artist - Song (Remix)", "x", ("Artist", "Song (Remix)"))
        self.check("Artist - Song (feat. Someone)", "x", ("Artist", "Song (feat. Someone)"))
        self.check("Artist - Song (Live)", "x", ("Artist", "Song (Live)"))
        self.assertEqual(strip_noise("Song [4K]"), "Song")

    def test_topic_channels_name_the_artist(self):
        self.check("Song Name", "Some Artist - Topic", ("Some Artist", "Song Name"))

    def test_no_separator_falls_back_to_the_channel(self):
        self.check("Just a title (Official Video)", "SomeChannel", ("SomeChannel", "Just a title"))
        self.check("Only Title", "", ("", "Only Title"))

    def test_degenerate_input_never_raises_or_returns_empty_titles_for_noise(self):
        self.check("", "", ("", ""))
        self.check("(Official Video)", "Ch", ("Ch", "(Official Video)"))
        self.check("- Title", "Ch", ("Ch", "- Title"))
        self.assertEqual(parse_track(object()), ("", ""))

    def test_output_is_cleaned(self):
        artist, title = parse_track(Track("id", f"Ar{OSC52}tist - So\x00ng", 1, "x"))
        self.assertFalse(set(artist + title) & set("\x1b\x07\x00"))


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
