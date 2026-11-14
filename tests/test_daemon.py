"""Background (daemon) mode: leaving the TUI keeps playback running until `mpf.py quit`."""

import asyncio
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import mpf
from mpf_core import daemon
from mpf_core.engine import PlaybackEngine

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabc"


class RuntimeDirTestCase(unittest.TestCase):
    """Point the daemon's socket and pid file at a private 0700 directory."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mpf-daemon-test-")
        os.chmod(self._tmp, 0o700)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)


class TestControlSocket(RuntimeDirTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_quit_command_runs_the_callback_and_replies(self):
        quits = []
        server = await daemon.serve(lambda: quits.append(True))
        self.addCleanup(server.close)

        reply = await asyncio.to_thread(daemon.send_command, "quit")

        self.assertEqual(reply, "ok")
        self.assertEqual(quits, [True])

    async def test_unknown_command_is_rejected_without_quitting(self):
        quits = []
        server = await daemon.serve(lambda: quits.append(True))
        self.addCleanup(server.close)

        reply = await asyncio.to_thread(daemon.send_command, "rm -rf")

        self.assertEqual(reply, "error: unknown command")
        self.assertEqual(quits, [])

    async def test_socket_and_pid_file_are_private_and_removed_on_close(self):
        server = await daemon.serve(lambda: None)

        self.assertEqual(stat.S_IMODE(os.stat(daemon.socket_path()).st_mode), 0o600)
        with open(daemon.pid_path(), encoding="utf-8") as handle:
            self.assertEqual(int(handle.read()), os.getpid())

        server.close()
        self.assertFalse(os.path.exists(daemon.socket_path()))
        self.assertFalse(os.path.exists(daemon.pid_path()))

    async def test_a_second_daemon_is_refused(self):
        server = await daemon.serve(lambda: None)
        self.addCleanup(server.close)

        with self.assertRaises(daemon.DaemonAlreadyRunning):
            await daemon.serve(lambda: None)
        self.assertTrue(os.path.exists(daemon.socket_path()))  # the first daemon is untouched

    async def test_a_stale_socket_is_replaced(self):
        stale = daemon.socket_path()
        import socket

        leftover = socket.socket(socket.AF_UNIX)
        leftover.bind(stale)
        leftover.close()  # bound but nobody listening: what a crashed daemon leaves behind

        server = await daemon.serve(lambda: None)
        self.addCleanup(server.close)
        self.assertTrue(await asyncio.to_thread(daemon.is_running))


class TestClientHelpers(RuntimeDirTestCase):
    def test_nothing_running(self):
        self.assertFalse(daemon.is_running())
        self.assertIsNone(daemon.send_command("quit"))
        self.assertFalse(daemon.stop())

    def test_stale_socket_file_is_cleaned_up(self):
        import socket

        leftover = socket.socket(socket.AF_UNIX)
        leftover.bind(daemon.socket_path())
        leftover.close()
        self.assertFalse(daemon.is_running())
        self.assertFalse(os.path.exists(daemon.socket_path()))

    def test_stop_asks_the_daemon_to_quit_and_waits_until_it_is_gone(self):
        replies = iter(["ok"])
        states = iter([True, True, False])  # still shutting down for two polls, then gone
        with (
            patch.object(daemon, "send_command", side_effect=lambda _c, timeout=2.0: next(replies)),
            patch.object(daemon, "is_running", side_effect=lambda: next(states)),
            patch("time.sleep"),
        ):
            self.assertTrue(daemon.stop(timeout=5))

    def test_stop_gives_up_after_the_timeout(self):
        with (
            patch.object(daemon, "send_command", return_value="ok"),
            patch.object(daemon, "is_running", return_value=True),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=[0, 1, 99]),
        ):
            with self.assertRaises(TimeoutError):
                daemon.stop(timeout=5)


class TestSpawn(unittest.TestCase):
    def test_spawns_a_detached_headless_process(self):
        with patch("subprocess.Popen") as popen:
            self.assertTrue(daemon.spawn(PLAYLIST_URL, "/tmp/config.json"))
        command = popen.call_args.args[0]
        self.assertIn("--daemon", command)
        self.assertEqual(command[command.index("--config") + 1], "/tmp/config.json")
        self.assertEqual(command[-1], PLAYLIST_URL)
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_spawn_failure_is_reported_not_raised(self):
        with patch("subprocess.Popen", side_effect=OSError("nope")):
            self.assertFalse(daemon.spawn(PLAYLIST_URL, "/tmp/config.json"))


class TestHeadlessRun(RuntimeDirTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_quit_command_stops_a_headless_engine_and_removes_the_socket(self):
        with patch("mpf_core.engine.MPV") as mock_mpv:
            engine = PlaybackEngine(PLAYLIST_URL, auto_play=False, headless=True)
            engine.mpris = MagicMock()
            engine.mpris.start = AsyncMock()
            engine.load_playlist = AsyncMock()

            task = asyncio.create_task(engine.run_headless())
            for _ in range(100):
                if await asyncio.to_thread(daemon.is_running):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(await asyncio.to_thread(daemon.send_command, "quit"), "ok")
            await asyncio.wait_for(task, 5)

        mock_mpv.return_value.terminate.assert_called_once()
        self.assertFalse(os.path.exists(daemon.socket_path()))


class TestCli(unittest.TestCase):
    def _main(self, argv, **patches):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("mpf.configure_logging"):
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(patch(name, value))
                mpf.main(argv)
        return out.getvalue()

    def test_daemon_flag_is_parsed_but_hidden_from_help(self):
        self.assertTrue(mpf.parse_cli_args(["--daemon"]).daemon)
        self.assertFalse(mpf.parse_cli_args([]).daemon)
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text), self.assertRaises(SystemExit):
            mpf.parse_cli_args(["--help"])
        self.assertNotIn("--daemon", help_text.getvalue())
        self.assertIn("quit", help_text.getvalue())

    def test_quit_stops_a_running_daemon(self):
        stop = MagicMock(return_value=True)
        output = self._main(["quit"], **{"mpf_core.daemon.stop": stop})
        stop.assert_called_once()
        self.assertIn("stopped", output.lower())

    def test_quit_without_a_daemon_says_so(self):
        output = self._main(["quit"], **{"mpf_core.daemon.stop": MagicMock(return_value=False)})
        self.assertIn("not running", output.lower())

    def _client(self, **attrs):
        player = MagicMock()
        player.run = AsyncMock()
        player.music_continues = False
        for key, value in attrs.items():
            setattr(player, key, value)
        return player

    def test_starting_the_tui_attaches_and_never_stops_the_daemon(self):
        player = self._client()
        fake_class = MagicMock(return_value=player)
        stop = MagicMock()
        with patch("mpf_core.MediaPlayerFluere", fake_class, create=True):
            self._main([PLAYLIST_URL], **{"mpf_core.daemon.stop": stop})
        player.run.assert_awaited_once()
        stop.assert_not_called()
        kwargs = fake_class.call_args.kwargs
        self.assertEqual((kwargs["playlist_url"], kwargs["auto_play"], kwargs["force_refresh"]), (PLAYLIST_URL, True, False))

    def test_no_url_means_whatever_the_background_player_is_playing(self):
        fake_class = MagicMock(return_value=self._client())
        with patch("mpf_core.MediaPlayerFluere", fake_class, create=True):
            self._main([])
        self.assertEqual(fake_class.call_args.kwargs["playlist_url"], "")

    def test_flags_reach_the_client(self):
        fake_class = MagicMock(return_value=self._client())
        with patch("mpf_core.MediaPlayerFluere", fake_class, create=True):
            self._main(["--no-auto-play", "--force-cache", "--no-visualizer", "--no-preview", PLAYLIST_URL])
        kwargs = fake_class.call_args.kwargs
        self.assertFalse(kwargs["auto_play"])
        self.assertTrue(kwargs["force_refresh"])
        self.assertFalse(kwargs["show_visualizer"])
        self.assertFalse(kwargs["show_preview"])

    def test_leaving_while_music_plays_says_how_to_stop_it(self):
        player = self._client(music_continues=True)
        with (
            patch("mpf_core.MediaPlayerFluere", MagicMock(return_value=player), create=True),
            patch.object(sys, "argv", ["./mpf.py"]),
        ):
            output = self._main([PLAYLIST_URL])
        self.assertIn("still playing", output)
        self.assertIn("./mpf.py quit", output)

    def test_leaving_while_paused_prints_no_note(self):
        with patch("mpf_core.MediaPlayerFluere", MagicMock(return_value=self._client()), create=True):
            output = self._main([PLAYLIST_URL])
        self.assertNotIn("still playing", output)

    def test_an_unavailable_background_player_is_reported_and_exits_nonzero(self):
        from mpf_core.client import DaemonUnavailable

        player = self._client()
        player.run = AsyncMock(side_effect=DaemonUnavailable("could not start the background player"))
        with patch("mpf_core.MediaPlayerFluere", MagicMock(return_value=player), create=True):
            with self.assertRaises(SystemExit) as raised:
                self._main([PLAYLIST_URL])
        self.assertEqual(raised.exception.code, 1)

    def test_daemon_flag_runs_the_engine_alone_without_the_tui(self):
        engine = MagicMock()
        engine.run_headless = AsyncMock()
        fake_class = MagicMock(return_value=engine)
        tui = MagicMock()
        with patch("mpf_core.engine.PlaybackEngine", fake_class), patch("mpf_core.MediaPlayerFluere", tui, create=True):
            self._main(["--daemon", PLAYLIST_URL])
        kwargs = fake_class.call_args.kwargs
        self.assertTrue(kwargs["headless"])
        self.assertTrue(kwargs["auto_play"])
        engine.run_headless.assert_awaited_once()
        tui.assert_not_called()  # no terminal, numpy or blessed for the background player

    def test_daemon_flag_honours_no_auto_play(self):
        engine = MagicMock()
        engine.run_headless = AsyncMock()
        fake_class = MagicMock(return_value=engine)
        with patch("mpf_core.engine.PlaybackEngine", fake_class):
            self._main(["--daemon", "--no-auto-play", PLAYLIST_URL])
        self.assertFalse(fake_class.call_args.kwargs["auto_play"])


class TestSpawnArguments(unittest.TestCase):
    def test_extra_arguments_and_the_url_are_passed_to_the_daemon(self):
        with patch("subprocess.Popen") as popen:
            self.assertTrue(
                daemon.spawn(PLAYLIST_URL, "/tmp/config.json", extra_args=["--no-auto-play", "--force-cache"])
            )
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--daemon") :], [
            "--daemon", "--config", "/tmp/config.json", "--no-auto-play", "--force-cache", PLAYLIST_URL,
        ])

    def test_no_url_means_the_daemon_loads_the_saved_default(self):
        with patch("subprocess.Popen") as popen:
            daemon.spawn("", "/tmp/config.json")
        self.assertEqual(popen.call_args.args[0][-3:], ["--daemon", "--config", "/tmp/config.json"])

    def test_the_old_adopt_option_is_gone(self):
        with patch("subprocess.Popen") as popen:
            daemon.spawn(PLAYLIST_URL, "/tmp/config.json")
        self.assertNotIn("--adopt-ipc", popen.call_args.args[0])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            mpf.parse_cli_args(["--adopt-ipc", "/x.sock"])


class TestRealDaemonProcess(RuntimeDirTestCase):
    """End to end with the real script and a real (idle, silent) mpv: no playlist, so no audio at all."""

    def test_daemon_starts_answers_and_quits(self):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpf.py")
        env = dict(os.environ, XDG_RUNTIME_DIR=self._tmp, XDG_CONFIG_HOME=self._tmp, XDG_CACHE_HOME=self._tmp)
        config = os.path.join(self._tmp, "config.json")
        process = subprocess.Popen(
            [sys.executable, script, "--daemon", "--config", config],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 20
        while not daemon.is_running() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(daemon.is_running(), "daemon never started listening")

        quit_run = subprocess.run(
            [sys.executable, script, "quit"], env=env, capture_output=True, text=True, timeout=30
        )

        self.assertEqual(quit_run.returncode, 0, quit_run.stderr)
        self.assertIn("stopped", quit_run.stdout.lower())
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertFalse(daemon.is_running())


class TestRealControlCommands(RuntimeDirTestCase):
    """The real script and a real idle daemon (no playlist, so no audio): the commands work end to end."""

    def _mpf(self, *args, env):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpf.py")
        return subprocess.run([sys.executable, script, *args], env=env, capture_output=True, text=True, timeout=30)

    def test_commands_against_a_running_player(self):
        env = dict(os.environ, XDG_RUNTIME_DIR=self._tmp, XDG_CONFIG_HOME=self._tmp, XDG_CACHE_HOME=self._tmp)
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpf.py")
        # Before any player exists: a clear message, exit 1, and nothing is started.
        result = self._mpf("status", env=env)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("not running", result.stderr)
        self.assertFalse(daemon.is_running())

        process = subprocess.Popen(
            [sys.executable, script, "--daemon", "--config", os.path.join(self._tmp, "config.json")],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 20
        while not daemon.is_running() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(daemon.is_running(), "daemon never started listening")

        info = json.loads(self._mpf("status", "--json", env=env).stdout)
        self.assertEqual((info["state"], info["volume"], info["count"]), ("stopped", 100, 0))
        self.assertEqual(self._mpf("status", env=env).stdout, "Stopped: nothing playing\n")

        self.assertEqual(self._mpf("volume", "40", env=env).returncode, 0)
        self.assertEqual(self._mpf("volume", env=env).stdout.strip(), "40")
        self.assertEqual(self._mpf("volume", "-15", env=env).returncode, 0)
        self.assertEqual(self._mpf("volume", env=env).stdout.strip(), "25")
        for command in ("pause", "play", "toggle", "next", "prev"):  # nothing loaded: harmless, still exit 0
            self.assertEqual(self._mpf(command, env=env).returncode, 0, command)
        self.assertEqual(self._mpf("seek", "+30", env=env).returncode, 0)
        self.assertEqual(self._mpf("seek", "soon", env=env).returncode, 2)
        self.assertTrue(daemon.is_running())  # none of that disturbed the player

        quit_run = self._mpf("quit", env=env)
        self.assertEqual(quit_run.returncode, 0, quit_run.stderr)
        self.assertEqual(process.wait(timeout=20), 0)


if __name__ == "__main__":
    unittest.main()
