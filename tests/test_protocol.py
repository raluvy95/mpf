"""Tests for the client/daemon wire protocol."""

import unittest

from mpf_core.protocol import (
    COMMANDS,
    MAX_CLIENT_LINE,
    ProtocolError,
    decode,
    encode,
    validate_command,
)

VALID = {
    "play_index": {"index": 3},
    "set_paused": {"paused": True},
    "next": {},
    "prev": {},
    "seek_rel": {"seconds": -5},
    "seek_abs": {"seconds": 12.5},
    "set_volume": {"volume": 80},
    "set_mute": {"muted": False},
    "set_shuffle": {"enabled": True},
    "cycle_repeat": {},
    "load_url": {"url": "https://youtu.be/x", "auto_play": False, "force": True},
    "force_refresh": {},
    "set_playback_config": {"default_playlist": None, "fallback_clients": ["mweb"]},
    "shutdown": {},
}


class TestFraming(unittest.TestCase):
    def test_round_trip_keeps_unicode(self):
        message = {"t": "state", "title": "Café ☕", "position": 1.5}
        line = encode(message)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(decode(line), message)

    def test_newlines_in_values_cannot_split_a_frame(self):
        line = encode({"t": "state", "status": "a\nb"})
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(decode(line)["status"], "a\nb")

    def test_oversize_lines_are_refused_both_ways(self):
        with self.assertRaises(ProtocolError):
            decode(b"{" + b" " * MAX_CLIENT_LINE + b"}")
        with self.assertRaises(ProtocolError):
            encode({"t": "queue", "tracks": "x" * 100}, limit=50)

    def test_garbage_is_refused(self):
        for line in (b"\xff\xfe", b"not json", b"[1, 2]", b'"t"', b'{"x": 1}', b'{"t": 5}'):
            with self.subTest(line=line), self.assertRaises(ProtocolError):
                decode(line)

    def test_encode_needs_a_type(self):
        with self.assertRaises(ProtocolError):
            encode({"position": 1})


class TestCommands(unittest.TestCase):
    def test_every_command_is_covered_here(self):
        self.assertEqual(set(VALID), set(COMMANDS))

    def test_valid_commands_pass(self):
        for name, args in VALID.items():
            with self.subTest(name=name):
                self.assertEqual(validate_command({"t": "cmd", "name": name, **args}), (name, args))

    def test_optional_arguments_may_be_left_out(self):
        self.assertEqual(validate_command({"t": "cmd", "name": "load_url", "url": "u"}), ("load_url", {"url": "u"}))

    def test_wrong_types_are_refused(self):
        for name, args in (
            ("play_index", {"index": "3"}),
            ("play_index", {"index": True}),  # a bool is not an index
            ("set_volume", {"volume": 1.5}),
            ("set_paused", {"paused": 1}),
            ("seek_rel", {"seconds": None}),
            ("load_url", {"url": 5}),
            ("set_playback_config", {"fallback_clients": "mweb"}),
            ("set_playback_config", {"fallback_clients": [1]}),
        ):
            with self.subTest(name=name, args=args), self.assertRaises(ProtocolError):
                validate_command({"t": "cmd", "name": name, **args})

    def test_missing_extra_and_unknown_are_refused(self):
        for message in (
            {"t": "cmd", "name": "play_index"},
            {"t": "cmd", "name": "next", "index": 1},
            {"t": "cmd", "name": "rm_rf"},
            {"t": "cmd"},
            {"t": "state", "name": "next"},
        ):
            with self.subTest(message=message), self.assertRaises(ProtocolError):
                validate_command(message)


if __name__ == "__main__":
    unittest.main()
