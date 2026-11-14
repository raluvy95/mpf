"""Background (daemon) mode: leaving the TUI keeps playback running until `mpf.py quit`."""

import asyncio
import contextlib
import io
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
from mpf_core.models import Track
from mpf_core.player import BlessedMusicPlayer

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


class TestDetachRule(unittest.TestCase):
    """q and Ctrl-C both end in run()'s cleanup, which asks _should_detach()."""

    def _player(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track(id="a", title="A", uploader="X")])
        player.queue.current_idx = 0
        player.mpv = MagicMock()
        player._is_paused = False
        return player

    def test_playing_track_keeps_playing_in_background(self):
        self.assertTrue(self._player()._should_detach())

    def test_paused_player_exits_completely(self):
        player = self._player()
        player._is_paused = True
        self.assertFalse(player._should_detach())

    def test_nothing_loaded_exits_completely(self):
        player = self._player()
        player.queue.set_tracks([])
        self.assertFalse(player._should_detach())

    def test_no_mpv_exits_completely(self):
        player = self._player()
        player.mpv = None
        self.assertFalse(player._should_detach())

    def test_a_headless_player_never_detaches_again(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False, headless=True)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track(id="a", title="A", uploader="X")])
        player.queue.current_idx = 0
        player.mpv = MagicMock()
        player._is_paused = False
        self.assertFalse(player._should_detach())


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
    async def test_quit_command_stops_a_headless_player_without_touching_the_terminal(self):
        with patch("mpf_core.player.MPV") as mock_mpv:
            player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False, headless=True)
            player.previewer.close()
            player.term = MagicMock()
            player.spectrum = MagicMock()
            player.previewer = MagicMock()
            player.mpris = MagicMock()
            player.mpris.start = AsyncMock()
            player.load_playlist = AsyncMock()

            task = asyncio.create_task(player.run())
            for _ in range(100):
                if await asyncio.to_thread(daemon.is_running):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(await asyncio.to_thread(daemon.send_command, "quit"), "ok")
            await asyncio.wait_for(task, 5)

        mock_mpv.return_value.terminate.assert_called_once()
        player.term.fullscreen.assert_not_called()
        player.spectrum.start.assert_not_called()
        player.previewer.clear.assert_not_called()
        self.assertFalse(os.path.exists(daemon.socket_path()))
        self.assertFalse(player.detach_on_exit)

    async def test_interactive_run_requests_a_handoff_only_when_it_should(self):
        for paused, expected in ((False, True), (True, False)):
            with self.subTest(paused=paused), patch("mpf_core.player.MPV"):
                player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
                player.previewer.close()
                player.term = MagicMock()
                player.term.inkey.return_value = "q"
                player.spectrum = MagicMock()
                player.previewer = MagicMock()
                player.load_playlist = AsyncMock()
                player._render = MagicMock()
                player.queue.set_tracks([Track(id="a", title="A", uploader="X")])
                player.queue.current_idx = 0
                player._is_paused = paused

                await player.run()

                self.assertEqual(player.detach_on_exit, expected)


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

    def test_starting_the_tui_takes_over_from_a_running_daemon(self):
        order = []
        player = MagicMock()
        player.run = AsyncMock(side_effect=lambda: order.append("run"))
        player.detach_on_exit = False
        fake_class = MagicMock(return_value=player)
        stop = MagicMock(side_effect=lambda: order.append("stop") or True)
        with patch("mpf_core.BlessedMusicPlayer", fake_class, create=True):
            self._main([PLAYLIST_URL], **{"mpf_core.daemon.stop": stop})
        self.assertEqual(order, ["stop", "run"])

    def test_leaving_while_playing_prints_the_note_once_the_daemon_took_over(self):
        player = MagicMock()
        player.run = AsyncMock()
        player.detach_on_exit = True
        player.handed_off = True
        with (
            patch("mpf_core.BlessedMusicPlayer", MagicMock(return_value=player), create=True),
            patch.object(sys, "argv", ["./mpf.py"]),
        ):
            output = self._main([PLAYLIST_URL], **{"mpf_core.daemon.stop": MagicMock(return_value=False)})
        self.assertIn("still running", output)
        self.assertIn("./mpf.py quit", output)

    def test_leaving_while_paused_prints_no_note_and_starts_nothing(self):
        player = MagicMock()
        player.run = AsyncMock()
        player.detach_on_exit = False
        player.handed_off = False
        with patch("mpf_core.BlessedMusicPlayer", MagicMock(return_value=player), create=True):
            output = self._main([PLAYLIST_URL], **{"mpf_core.daemon.stop": MagicMock(return_value=False)})
        self.assertNotIn("still running", output)

    def test_failed_daemon_start_is_reported(self):
        player = MagicMock()
        player.run = AsyncMock()
        player.detach_on_exit = True
        player.handed_off = False
        with patch("mpf_core.BlessedMusicPlayer", MagicMock(return_value=player), create=True):
            output = self._main([PLAYLIST_URL], **{"mpf_core.daemon.stop": MagicMock(return_value=False)})
        self.assertIn("could not", output.lower())
        self.assertNotIn("still running", output)

    def test_daemon_flag_builds_a_headless_player(self):
        player = MagicMock()
        player.run = AsyncMock()
        fake_class = MagicMock(return_value=player)
        with patch("mpf_core.BlessedMusicPlayer", fake_class, create=True):
            self._main(["--daemon", PLAYLIST_URL])
        kwargs = fake_class.call_args.kwargs
        self.assertTrue(kwargs["headless"])
        self.assertTrue(kwargs["auto_play"])
        self.assertFalse(kwargs["show_visualizer"])
        self.assertFalse(kwargs["show_preview"])


class TestSpawnArguments(unittest.TestCase):
    def test_adopt_socket_is_passed_to_the_daemon(self):
        with patch("subprocess.Popen") as popen:
            self.assertTrue(daemon.spawn(PLAYLIST_URL, "/tmp/config.json", adopt_ipc="/run/x.sock"))
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--adopt-ipc") + 1], "/run/x.sock")

    def test_no_adopt_flag_by_default(self):
        with patch("subprocess.Popen") as popen:
            daemon.spawn(PLAYLIST_URL, "/tmp/config.json")
        self.assertNotIn("--adopt-ipc", popen.call_args.args[0])


class TestHandoff(RuntimeDirTestCase, unittest.IsolatedAsyncioTestCase):
    """The TUI keeps its mpv playing and lets the daemon take it over; nothing is restarted."""

    def _tui(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=False)
        player.previewer.close()
        player.term = MagicMock()
        player.term.inkey.return_value = "q"
        player.spectrum = MagicMock()
        player.previewer = MagicMock()
        player.load_playlist = AsyncMock()
        player._render = MagicMock()
        player.queue.set_tracks([Track(id="a", title="A", uploader="X")])
        player.queue.current_idx = 0
        return player

    async def test_successful_handoff_leaves_mpv_running(self):
        with patch("mpf_core.player.MPV") as mock_mpv, patch("mpf_core.daemon.spawn", return_value=True) as spawn, patch(
            "mpf_core.daemon.is_running", return_value=True
        ):
            player = self._tui()
            await player.run()
        self.assertTrue(player.handed_off)
        self.assertEqual(spawn.call_args.kwargs["adopt_ipc"], player._ipc_socket)
        self.assertIsNone(mock_mpv.return_value.mpv_process)  # terminate() no longer owns a process to kill

    async def test_failed_handoff_stops_mpv_as_before(self):
        with patch("mpf_core.player.MPV") as mock_mpv, patch("mpf_core.daemon.spawn", return_value=False):
            player = self._tui()
            await player.run()
        self.assertTrue(player.detach_on_exit)
        self.assertFalse(player.handed_off)
        self.assertIsNotNone(mock_mpv.return_value.mpv_process)
        mock_mpv.return_value.terminate.assert_called_once()

    async def test_a_daemon_that_never_answers_is_not_left_to_start_late(self):
        with (
            patch("mpf_core.player.MPV"),
            patch("mpf_core.player.HANDOFF_TIMEOUT_SECONDS", 0.2),
            patch("mpf_core.daemon.spawn", return_value=True),
            patch("mpf_core.daemon.is_running", return_value=False),
            patch("mpf_core.daemon.stop") as stop,
        ):
            player = self._tui()
            await player.run()
        self.assertFalse(player.handed_off)
        stop.assert_called_once()

    def _daemon_player(self):
        player = BlessedMusicPlayer(PLAYLIST_URL, auto_play=True, headless=True)
        self.addCleanup(player.previewer.close)
        player.queue.set_tracks([Track(id="a", title="A", uploader="X"), Track(id="b", title="B", uploader="X")])
        player.mpv = MagicMock()
        player.previewer = MagicMock()
        return player

    def test_resuming_an_adopted_mpv_does_not_reload_the_track(self):
        player = self._daemon_player()
        player._adopting = True
        player.mpv.path = player.queue.tracks[1].url
        player._resume_index(1, 42.0)
        player.mpv.play.assert_not_called()
        self.assertEqual(player.queue.current_idx, 1)
        self.assertIsNone(player._pending_seek)
        self.assertFalse(player._adopting)

    def test_an_adopted_mpv_playing_another_track_is_restarted_on_the_saved_one(self):
        player = self._daemon_player()
        player._adopting = True
        player.mpv.path = player.queue.tracks[0].url
        player._resume_index(1, 42.0)
        player.mpv.play.assert_called_once_with(player.queue.tracks[1].url)
        self.assertEqual(player._pending_seek, ("b", 42.0))

    def test_without_adoption_the_saved_track_starts_normally(self):
        player = self._daemon_player()
        player._resume_index(1, 0.0)
        player.mpv.play.assert_called_once_with(player.queue.tracks[1].url)

    def test_adopting_a_missing_socket_falls_back_to_a_fresh_mpv(self):
        player = self._daemon_player()
        player.mpv = None
        with patch("mpf_core.player.MPV") as mock_mpv:
            player._adopt_running_mpv(os.path.join(self._tmp, "gone.sock"))
        self.assertIsNone(player.mpv)
        self.assertFalse(player._adopting)
        mock_mpv.assert_not_called()  # refused before connecting: not a socket we own


@unittest.skipUnless(shutil.which("mpv"), "mpv is not installed")
class TestRealHandoff(RuntimeDirTestCase):
    """A real mpv plays a synthetic tone; the TUI lets go and a real daemon takes it over without a restart."""

    def test_daemon_takes_over_the_same_mpv_without_interrupting_it(self):
        import math
        import struct
        import wave

        from python_mpv_jsonipc import MPV

        tone = os.path.join(self._tmp, "tone.wav")
        with wave.open(tone, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(
                b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(8000 * 60))
            )
        sock = os.path.join(self._tmp, "mpv.sock")
        tui_mpv = MPV(ipc_socket=sock, video=False, ao="null")  # the TUI's mpv, silent
        process = tui_mpv.mpv_process.process
        self.addCleanup(process.kill)
        tui_mpv.play(tone)
        time.sleep(1.0)
        before = tui_mpv.time_pos
        self.assertGreater(before, 0.5)

        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mpf.py")
        env = dict(os.environ, XDG_RUNTIME_DIR=self._tmp, XDG_CONFIG_HOME=self._tmp, XDG_CACHE_HOME=self._tmp)
        config = os.path.join(self._tmp, "config.json")
        daemon_process = subprocess.Popen(
            [sys.executable, script, "--daemon", "--config", config, "--adopt-ipc", sock],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(daemon_process.kill)
        deadline = time.monotonic() + 20
        while not daemon.is_running() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(daemon.is_running(), "daemon never started listening")
        time.sleep(0.5)  # let it finish connecting to mpv

        # What the TUI does on a handoff: disconnect without killing mpv.
        tui_mpv.mpv_process = None
        tui_mpv.terminate()
        time.sleep(1.0)

        self.assertIsNone(process.poll(), "mpv died when the TUI let go")
        probe = MPV(start_mpv=False, ipc_socket=sock)
        after = probe.time_pos
        probe.terminate()
        self.assertGreater(after, before + 1.0, "playback was interrupted or restarted")

        quit_run = subprocess.run([sys.executable, script, "quit"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(quit_run.returncode, 0, quit_run.stderr)
        self.assertEqual(daemon_process.wait(timeout=20), 0)
        process.wait(timeout=10)  # the daemon adopted this mpv, so it must stop it too


@unittest.skipUnless(shutil.which("mpv"), "mpv is not installed")
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


if __name__ == "__main__":
    unittest.main()
