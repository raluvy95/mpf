"""Control subcommands: argument checking, their effect on a running player, and the status output."""

import asyncio
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_server import ServerTestCase

import mpf
from mpf_core import control, daemon
from mpf_core.control import ControlError, check_usage, parse_seek, parse_volume
from mpf_core.models import Track


class TestParsing(unittest.TestCase):
    def test_seek_absolute_and_relative(self):
        for text, expected in (
            ("90", (False, 90.0)),
            ("1:30", (False, 90.0)),
            ("1:02:03", (False, 3723.0)),
            ("+30", (True, 30.0)),
            ("-30", (True, -30.0)),
            ("+1:05", (True, 65.0)),
            ("-1:05", (True, -65.0)),
            ("12.5", (False, 12.5)),
            ("0", (False, 0.0)),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_seek(text), expected)

    def test_bad_seek_values(self):
        for text in ("", "abc", "1:", ":30", "1:2:3:4", "--5", "5s", "1,5"):
            with self.subTest(text=text), self.assertRaises(ControlError):
                parse_seek(text)

    def test_volume_absolute_and_relative(self):
        for text, expected in (("40", (False, 40)), ("0", (False, 0)), ("100", (False, 100)), ("+5", (True, 5)), ("-5", (True, -5))):
            with self.subTest(text=text):
                self.assertEqual(parse_volume(text), expected)

    def test_bad_volume_values(self):
        for text in ("", "loud", "101", "1000", "4.5", "5%"):
            with self.subTest(text=text), self.assertRaises(ControlError):
                parse_volume(text)

    def test_usage_rules(self):
        self.assertIsNone(check_usage("pause", None, False))
        self.assertIsNone(check_usage("volume", None, False))
        self.assertIsNone(check_usage("volume", "+5", False))
        self.assertIsNone(check_usage("seek", "-10", False))
        self.assertIsNone(check_usage("status", None, True))
        self.assertIn("takes no argument", check_usage("next", "3", False))
        self.assertIn("needs a time", check_usage("seek", None, False))
        self.assertIn("--json", check_usage("pause", None, True))
        self.assertIn("0 to 100", check_usage("volume", "loud", False))


class TestCommandLine(unittest.TestCase):
    def _parse(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                return mpf.parse_cli_args(list(argv)), ""
            except SystemExit as exit_:
                self.assertEqual(exit_.code, 2)
                return None, err.getvalue()

    def test_valid_forms_parse(self):
        for argv, command, value in (
            (("pause",), "pause", None),
            (("volume", "40"), "volume", "40"),
            (("volume", "-5"), "volume", "-5"),
            (("seek", "+30"), "seek", "+30"),
            (("seek", "-30"), "seek", "-30"),
            (("seek", "1:30"), "seek", "1:30"),
            (("quit",), "quit", None),
        ):
            with self.subTest(argv=argv):
                args, _ = self._parse(*argv)
                self.assertEqual((args.url, args.value), (command, value))
        args, _ = self._parse("status", "--json")
        self.assertTrue(args.json)

    def test_misuse_is_reported_with_exit_code_2(self):
        for argv, text in (
            (("seek",), "needs a time"),
            (("volume", "loud"), "0 to 100"),
            (("pause", "now"), "takes no argument"),
            (("next", "--json"), "--json only applies"),
            (("quit", "now"), "takes no argument"),
            (("https://youtu.be/abc", "extra"), "unexpected argument"),
            (("https://youtu.be/abc", "--json"), "--json only applies"),
        ):
            with self.subTest(argv=argv):
                args, err = self._parse(*argv)
                self.assertIsNone(args)
                self.assertIn(text, err)

    def test_a_url_and_the_old_flags_still_parse_as_before(self):
        args, _ = self._parse("--no-auto-play", "https://youtu.be/abc")
        self.assertEqual((args.url, args.value, args.json), ("https://youtu.be/abc", None, False))

    def test_help_lists_the_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            mpf.parse_cli_args(["--help"])
        for word in ("volume", "seek", "status", "toggle", "quit"):
            self.assertIn(word, out.getvalue())


class ControlTestCase(ServerTestCase):
    async def run_command(self, *argv):
        """Run a control command as the real entry point does; returns (exit code, stdout, stderr).

        It blocks, so it runs in a thread: the test's server lives on this event loop. It does not wait for
        anything afterwards, because the command has to have been handled by the time it returns.
        """
        command, value = argv[0], (argv[1] if len(argv) > 1 and argv[1] != "--json" else None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = await asyncio.to_thread(control.run, command, value, "--json" in argv)
        return code, out.getvalue(), err.getvalue()

    async def settle(self, seconds=0.1):
        await asyncio.sleep(seconds)


class TestCommandsReachThePlayer(ControlTestCase):
    async def test_the_simple_controls(self):
        for argv in (("pause",), ("play",), ("toggle",), ("next",), ("prev",)):
            code, out, err = await self.run_command(*argv)
            self.assertEqual((code, out, err), (0, "", ""), argv)
        self.assertEqual(
            self.engine.calls, [("set_paused", True), ("set_paused", False), ("set_paused", True), ("next",), ("prev",)]
        )

    async def test_volume_set_and_relative(self):
        await self.run_command("volume", "40")
        await self.run_command("volume", "+5")  # relative to what the player now reports: 40
        await self.run_command("volume", "-100")  # clamped at 0
        self.assertEqual(
            [call for call in self.engine.calls if call[0] == "set_volume"],
            [("set_volume", 40), ("set_volume", 45), ("set_volume", 0)],
        )

    async def test_volume_without_an_argument_prints_it(self):
        code, out, _ = await self.run_command("volume")
        self.assertEqual((code, out.strip()), (0, "70"))
        self.assertEqual(self.engine.calls, [])

    async def test_seek_forms(self):
        await self.run_command("seek", "+30")
        await self.run_command("seek", "-10")
        await self.run_command("seek", "1:30")
        self.assertEqual(self.engine.calls, [("seek_rel", 30.0), ("seek_rel", -10.0), ("seek_abs", 90.0)])

    async def test_toggle_follows_the_players_state(self):
        self.engine.paused = True
        self.engine._notify()
        await self.settle()
        await self.run_command("toggle")
        self.assertEqual(self.engine.calls, [("set_paused", False)])

    async def test_the_command_has_been_handled_by_the_time_it_returns(self):
        # No waiting at all: the control command only returns once the player has read it and closed.
        for argv in (("pause",), ("next",), ("volume", "33"), ("seek", "+7")):
            self.engine.calls.clear()
            code, _out, _err = await self.run_command(*argv)
            self.assertEqual(code, 0)
            self.assertEqual(len(self.engine.calls), 1, argv)

    async def test_bad_usage_exits_2_without_connecting(self):
        code, _, err = await self.run_command("seek", "soon")
        self.assertEqual(code, 2)
        self.assertIn("seek takes", err)
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.server.client_count, 0)

    async def test_a_control_command_leaves_nothing_connected(self):
        await self.run_command("next")
        await self.settle()
        self.assertEqual(self.server.client_count, 0)


class TestStatus(ControlTestCase):
    async def test_playing(self):
        self.engine.queue.current_idx = 0
        self.engine.position = 12.0
        self.engine._notify()
        await self.settle()
        code, out, _ = await self.run_command("status")
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            "Playing: A\n  U · 00:12 / 01:40 · volume 70% · repeat All · shuffle off · track 1 of 2\n",
        )

    async def test_paused(self):
        self.engine.paused = True
        self.engine._notify()
        await self.settle()
        _, out, _ = await self.run_command("status")
        self.assertTrue(out.startswith("Paused: A"))

    async def test_stopped_when_nothing_is_loaded_or_idle(self):
        self.engine.idle = True
        self.engine._notify()
        await self.settle()
        _, out, _ = await self.run_command("status")
        self.assertEqual(out, "Stopped: nothing playing\n")
        self.engine.idle = False
        self.engine.queue.set_tracks([])
        self.engine._notify()
        await self.settle()
        _, out, _ = await self.run_command("status")
        self.assertEqual(out, "Stopped: nothing playing\n")

    async def test_json_has_everything_a_status_bar_needs(self):
        self.engine.queue.current_idx = 1
        self.engine._notify()
        await self.settle()
        code, out, _ = await self.run_command("status", "--json")
        info = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(
            (info["state"], info["title"], info["id"], info["url"], info["index"], info["count"]),
            ("playing", "B", "b", "https://youtu.be/b", 1, 2),
        )
        self.assertEqual((info["volume"], info["muted"], info["repeat"], info["shuffle"]), (70, False, "All", False))
        self.assertEqual((info["duration"], info["playlist_url"]), (100.0, self.engine.playlist_url))
        self.assertEqual(len(out.strip().splitlines()), 1)  # one line: easy for a bar to read

    async def test_json_for_a_stopped_player_has_empty_track_fields(self):
        self.engine.queue.set_tracks([])
        self.engine._notify()
        await self.settle()
        _, out, _ = await self.run_command("status", "--json")
        info = json.loads(out)
        self.assertEqual((info["state"], info["title"], info["index"], info["count"]), ("stopped", "", None, 0))

    async def test_text_from_the_track_cannot_carry_escape_sequences(self):
        self.engine.queue.set_tracks([Track("a", "Hi\x1b]0;PWNED\x07", 10.0, "Up\x1b[2J")])
        self.engine._notify()
        await self.settle()
        _, out, _ = await self.run_command("status")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)


class TestNoPlayerRunning(unittest.IsolatedAsyncioTestCase):
    async def test_it_says_so_exits_1_and_never_starts_one(self):
        tmp = tempfile.mkdtemp(prefix="mpf-control-test-")
        os.chmod(tmp, 0o700)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}), patch("mpf_core.daemon.spawn") as spawn:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                for command, value in (("pause", None), ("status", None), ("volume", "50"), ("seek", "+5")):
                    code = await asyncio.to_thread(control.run, command, value, False)
                    self.assertEqual(code, 1, command)
        spawn.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().count("MPF is not running in the background"), 4)


class TestHelpColours(unittest.TestCase):
    ANSI = r"\x1b\[[0-9;]*m"

    def _help(self, **env):
        import subprocess
        import sys

        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpf.py")
        base = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE")}
        return subprocess.run(
            [sys.executable, script, "--help"], env={**base, **env}, capture_output=True, text=True, check=True
        ).stdout

    def _section(self, text):
        start = text.rindex("\n", 0, text.index("control commands")) + 1  # from the start of its line, colour code included
        return text[start:]

    def test_piped_help_has_no_escape_codes(self):
        self.assertNotIn("\x1b", self._help())

    def test_no_color_is_respected_even_when_forced(self):
        self.assertNotIn("\x1b", self._help(FORCE_COLOR="1", NO_COLOR="1"))

    def test_coloured_help_uses_the_same_theme_as_the_rest(self):
        import re

        coloured = self._help(FORCE_COLOR="1")
        section = self._section(coloured)
        heading = re.search(r"\x1b\[1;34mcontrol commands", section)  # the colour of "options:" and the rest
        self.assertTrue(heading, section)
        self.assertIn("\x1b[1;34moptions:", coloured)  # argparse's own heading is the same bold blue
        for command in ("play", "pause", "toggle", "next", "prev", "volume", "seek", "status", "quit"):
            self.assertIn(f"\x1b[1;32m{command}\x1b[0m", section, command)  # like the url positional
        for placeholder in ("N", "+N", "-N", "SECONDS"):
            self.assertIn(f"\x1b[1;33m{placeholder}\x1b[0m", section, placeholder)  # like PATH
        self.assertIn("\x1b[1;36m--json\x1b[0m", section)  # like every other long option

    def test_stripping_the_colours_leaves_exactly_the_plain_text(self):
        import re

        plain = self._section(self._help())
        coloured = re.sub(self.ANSI, "", self._section(self._help(FORCE_COLOR="1")))
        self.assertEqual(coloured, plain)

    def test_descriptions_line_up_in_one_column(self):
        lines = self._section(self._help()).splitlines()
        starts = {
            next(line.index(text) for line in lines if text in line)
            for text in ("show or set the volume", "jump to, or by", "what is playing", "stop the background player")
        }
        self.assertEqual(len(starts), 1, lines)

    def test_no_character_pairs_that_fonts_draw_as_one_glyph(self):
        # Programming fonts turn "|-" into a tee (so "[+|-]" looked like "[+├]"): keep such pairs apart.
        for pair in ("|-", "-|", "|=", "=|", "<-", "->", "=>", "<=", "!="):
            self.assertNotIn(pair, self._section(self._help()), pair)

    def test_a_missing_theme_gives_the_plain_text(self):
        self.assertEqual(control.usage_help(None), control.USAGE_HELP)
        self.assertNotIn("\x1b", control.USAGE_HELP)


class TestOlderBackgroundPlayer(unittest.IsolatedAsyncioTestCase):
    """A 1.2.0 background player only knows the plain-text lines."""

    async def test_it_is_explained_and_nothing_is_sent(self):
        tmp = tempfile.mkdtemp(prefix="mpf-control-test-")
        os.chmod(tmp, 0o700)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            quits = []
            old = await daemon.serve(lambda: quits.append(True))  # the old plain-text-only handler
            self.addCleanup(old.close)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = await asyncio.to_thread(control.run, "pause", None, False)
        self.assertEqual(code, 1)
        self.assertIn("mpf.py quit", err.getvalue())
        self.assertEqual(quits, [])


class TestStaysQuick(unittest.TestCase):
    """These run from key bindings and status bars, so what they import is part of how they behave."""

    HEAVY = ("asyncio", "logging", "numpy", "blessed", "argparse", "mpf_core.client", "mpf_core.player")

    def _loaded_after(self, code):
        import subprocess
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = f"import sys; sys.path.insert(0, {root!r}); {code}; print(','.join(m for m in {self.HEAVY!r} if m in sys.modules))"
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=root)
        return out.stdout.strip()

    def test_the_control_module_loads_none_of_the_heavy_parts(self):
        self.assertEqual(self._loaded_after("import mpf_core.control"), "")

    def test_a_plain_command_through_main_never_builds_the_parser(self):
        # control.run is replaced, so this checks only what main() imports on the way to it.
        code = "import mpf, mpf_core.control as c; c.run = lambda *a: 0; mpf.main(['status'])"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import subprocess
        import sys

        script = f"import sys; sys.path.insert(0, {root!r}); \ntry:\n    {code}\nexcept SystemExit: pass\nprint(','.join(m for m in {self.HEAVY!r} if m in sys.modules))"
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=root)
        self.assertEqual(out.stdout.strip(), "")


class TestFastPath(unittest.TestCase):
    """mpf.main() runs the plain forms itself and leaves everything else to the argument parser."""

    def _run(self, *argv):
        with patch("mpf_core.control.run", return_value=0) as run:
            try:
                mpf.main(list(argv))
            except SystemExit as exit_:
                return run, exit_.code
        return run, None

    def test_plain_forms_are_taken_directly(self):
        for argv, expected in (
            (("pause",), ("pause", None, False)),
            (("next",), ("next", None, False)),
            (("volume",), ("volume", None, False)),
            (("volume", "40"), ("volume", "40", False)),
            (("volume", "-5"), ("volume", "-5", False)),
            (("seek", "+30"), ("seek", "+30", False)),
            (("seek", "1:30"), ("seek", "1:30", False)),
            (("status",), ("status", None, False)),
            (("status", "--json"), ("status", None, True)),
            (("--json",), None),
        ):
            with self.subTest(argv=argv):
                run, code = self._run(*argv)
                if expected is None:
                    run.assert_not_called()
                else:
                    run.assert_called_once_with(*expected)
                    self.assertEqual(code, 0)

    def test_mistakes_and_other_options_go_to_the_parser(self):
        for argv in (("seek",), ("volume", "loud"), ("pause", "now"), ("next", "--json"), ("pause", "--config", "x"),
                     ("status", "a", "b")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err:
                run, code = self._run(*argv)
                if argv == ("pause", "--config", "x"):
                    self.assertEqual(run.call_count, 1)  # valid: the parser accepts it and runs it
                else:
                    run.assert_not_called()
                    self.assertEqual(code, 2)
                    self.assertIn("error:", err.getvalue())

    def test_the_exit_code_is_the_commands(self):
        with patch("mpf_core.control.run", return_value=1):
            with self.assertRaises(SystemExit) as raised:
                mpf.main(["status"])
        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
