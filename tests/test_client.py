"""The client: its mirror of the daemon's state, the commands it sends, and what it does when the daemon goes."""

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from helpers import fake_daemon
from test_server import FakeEngine, ServerTestCase

from mpf_core import client as client_module
from mpf_core.client import DaemonUnavailable, RemoteEngine, VersionMismatch
from mpf_core.models import RepeatMode, Track, TrackQueue
from mpf_core.player import MediaPlayerFluere
from mpf_core.protocol import QUEUE_CHUNK
from mpf_core.server import PlayerServer

ESC = "\x1b"


class ClientTestCase(ServerTestCase):
    """A running server with a fake engine behind it, and helpers to attach clients to it."""

    def new_client(self, cls=RemoteEngine, **kwargs):
        player = cls(config_file=os.path.join(self._tmp, "client.json"), **kwargs)
        if isinstance(player, MediaPlayerFluere):
            player.previewer.close()
            player.previewer = MagicMock()
            player.spectrum = MagicMock(enabled=False)
        self.addCleanup(player.close)
        return player

    async def attached(self, cls=RemoteEngine, **kwargs):
        player = self.new_client(cls, **kwargs)
        await player.attach()
        return player

    async def settle(self, seconds=0.1):
        await asyncio.sleep(seconds)


class TestMirror(ClientTestCase):
    async def test_attaching_mirrors_the_snapshot_and_the_queue(self):
        player = await self.attached()
        self.assertTrue(player._connected)
        self.assertEqual(player.playlist_url, self.engine.playlist_url)
        self.assertEqual(player._status_msg, "ready")
        self.assertEqual([t.id for t in player.queue.tracks], ["a", "b"])
        self.assertEqual(player.queue.tracks[0].duration, 10.0)
        self.assertEqual(player.queue.repeat_mode, RepeatMode.ALL)
        self.assertFalse(player.started_daemon)

    async def test_a_long_queue_is_reassembled_in_order(self):
        self.engine.queue.set_tracks([Track(f"id{i}", f"T{i}") for i in range(QUEUE_CHUNK * 2 + 7)])
        player = await self.attached()
        self.assertEqual([t.id for t in player.queue.tracks], [f"id{i}" for i in range(QUEUE_CHUNK * 2 + 7)])

    async def test_later_changes_arrive_as_deltas(self):
        player = await self.attached(MediaPlayerFluere)
        player._render_requested.clear()
        self.engine.paused = True
        self.engine.position = 3.5
        self.engine.status = "paused"
        self.engine._notify()
        await self.settle()
        self.assertTrue(player._is_paused)
        self.assertEqual(player._time_pos, 3.5)
        self.assertEqual(player._status_msg, "paused")
        self.assertTrue(player._render_requested.is_set())

    async def test_a_new_queue_replaces_the_old_one_and_keeps_the_selection_on_the_same_track(self):
        player = await self.attached(MediaPlayerFluere)
        player._selected_list_idx = 1  # "b"
        self.engine.queue.set_tracks([Track("z", "Z"), Track("a", "A"), Track("b", "B")])
        self.engine._notify()
        await self.settle()
        self.assertEqual([t.id for t in player.queue.tracks], ["z", "a", "b"])
        self.assertEqual(player._selected_list_idx, 2)

    async def test_the_first_queue_selects_the_current_track(self):
        self.engine.queue.current_idx = 1
        player = await self.attached(MediaPlayerFluere)
        self.assertEqual(player._selected_list_idx, 1)

    async def test_text_from_the_daemon_is_cleaned_before_it_reaches_the_screen(self):
        player = await self.attached()
        self.engine.status = f"hi{ESC}]0;PWNED\x07 there"
        self.engine._notify()
        await self.settle()
        self.assertNotIn(ESC, player._status_msg)
        self.assertNotIn("\x07", player._status_msg)

    async def test_malformed_frames_are_ignored_not_fatal(self):
        player = await self.attached()
        player._apply({"t": "state", "volume": "loud", "position": None})
        player._apply({"t": "state", "repeat": "Sideways"})
        player._apply({"t": "queue", "revision": "x"})
        player._apply({"t": "queue", "revision": 5, "chunk": 0, "chunks": 1, "tracks": [["only-an-id"], 7, ["ok", "T", 1, "U"]]})
        player._apply({"t": "from-the-future"})
        self.assertEqual(player._volume, 100)
        self.assertEqual([t.id for t in player.queue.tracks], ["ok"])  # unusable rows are skipped

    async def test_starting_a_track_tells_the_screen_once(self):
        player = await self.attached(MediaPlayerFluere)
        player.previewer.request.reset_mock()
        self.engine.queue.current_idx = 1
        self.engine._notify()
        await self.settle()
        self.engine.position = 5.0  # same track: no new start
        self.engine._notify()
        await self.settle()
        player.previewer.request.assert_called_once_with(player.queue.tracks[1])


class TestCommands(ClientTestCase):
    async def test_controls_become_commands_the_engine_receives(self):
        player = await self.attached()
        player.set_paused(True)
        player.next_track()
        player._play_index(1)
        player.load_url("https://youtu.be/abc", auto_play=False, force=True)
        await self.settle()
        self.assertEqual(
            self.engine.calls,
            [("set_paused", True), ("next",), ("play_index", 1), ("load_url", "https://youtu.be/abc", False, True)],
        )

    async def test_changes_the_user_makes_show_at_once(self):
        player = await self.attached()
        player.set_paused(True)
        player.set_volume(150)
        player.seek_absolute(-4)
        player.set_mute(True)
        before = player.queue.repeat_mode
        player._cycle_repeat()
        self.assertEqual((player._is_paused, player._volume, player._time_pos, player._is_muted), (True, 100, 0.0, True))
        self.assertEqual(player.queue.repeat_mode, before.next_mode())

    async def test_without_a_connection_nothing_is_sent_and_nothing_pretends_to_change(self):
        player = RemoteEngine(config_file=os.path.join(self._tmp, "c.json"))
        player.set_paused(True)
        player.set_volume(10)
        self.assertEqual((player._is_paused, player._volume), (False, 100))
        self.assertFalse(player._send("next"))

    async def test_a_command_from_one_client_reaches_the_other(self):
        first, second = await self.attached(), await self.attached()
        first.set_paused(True)
        await self.settle()
        self.assertTrue(second._is_paused)

    async def test_an_error_frame_is_kept_and_shown(self):
        player = await self.attached()
        player._send("load_url", url="https://example.com/x")
        await self.settle()
        self.assertEqual(player.last_error[0], "load_url")
        self.assertIn("youtube.com", player._status_msg)


class TestLeaving(ClientTestCase):
    async def test_closing_a_client_leaves_the_daemon_and_other_clients_alone(self):
        first, second = await self.attached(), await self.attached()
        self.assertEqual(self.server.client_count, 2)
        first.close()
        await self.settle()
        self.assertEqual(self.server.client_count, 1)
        self.engine.paused = True
        self.engine._notify()
        await self.settle()
        self.assertTrue(second._is_paused)
        self.assertFalse(self.engine.quit_requested)

    async def test_music_continues_is_remembered_after_the_client_has_closed(self):
        player = await self.attached()
        self.assertTrue(player.music_continues)  # a track is current and not paused
        player.close()
        self.assertTrue(player.music_continues)
        paused = await self.attached()
        paused.set_paused(True)
        paused.close()
        self.assertFalse(paused.music_continues)

    async def test_closing_is_instant(self):
        player = await self.attached()
        loop = asyncio.get_running_loop()
        start = loop.time()
        player.close()
        self.assertLess(loop.time() - start, 0.05)


class TestHandshakeFailures(ClientTestCase):
    async def test_a_version_mismatch_is_explained_not_retried(self):
        player = self.new_client()
        with patch.object(client_module, "PROTOCOL_VERSION", 99), self.assertRaises(VersionMismatch) as raised:
            await player.attach()
        self.assertIn("mpf.py quit", str(raised.exception))
        self.assertFalse(player._connected)

    async def test_a_version_mismatch_is_an_unavailable_daemon_too(self):
        self.assertTrue(issubclass(VersionMismatch, DaemonUnavailable))


class TestStartingTheDaemon(unittest.IsolatedAsyncioTestCase):
    """No daemon running: the client starts one, waits for it, and connects."""

    async def asyncSetUp(self):
        import shutil
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="mpf-client-test-")
        os.chmod(self._tmp, 0o700)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.engine = FakeEngine(os.path.join(self._tmp, "config.json"))
        self.server = PlayerServer(self.engine)
        self.addCleanup(self.server.close)

    async def test_it_spawns_the_daemon_with_the_launch_arguments_and_connects(self):
        loop = asyncio.get_running_loop()
        spawned = []

        def fake_spawn(url, config, extra_args=()):
            spawned.append((url, config, list(extra_args)))
            asyncio.run_coroutine_threadsafe(self.server.start(), loop)  # the "daemon" comes up a moment later
            return True

        player = RemoteEngine(
            playlist_url="https://youtu.be/abc", auto_play=False, config_file="/tmp/c.json", force_refresh=True
        )
        self.addCleanup(player.close)
        with patch.object(client_module.daemon, "spawn", fake_spawn):
            await player.attach()
        self.assertEqual(spawned, [("https://youtu.be/abc", "/tmp/c.json", ["--no-auto-play", "--force-cache"])])
        self.assertTrue(player.started_daemon)
        self.assertTrue(player._connected)
        self.assertEqual(self.engine.calls, [])  # the daemon was started with the URL; no load_url on top

    async def test_it_reports_a_daemon_that_cannot_be_started(self):
        player = RemoteEngine(config_file="/tmp/c.json")
        with patch.object(client_module.daemon, "spawn", return_value=False), self.assertRaises(DaemonUnavailable):
            await player.attach()

    async def test_it_gives_up_on_a_daemon_that_never_comes_up(self):
        player = RemoteEngine(config_file="/tmp/c.json")
        with patch.object(client_module.daemon, "spawn", return_value=True), patch.object(
            client_module, "STARTUP_TIMEOUT_SECONDS", 0.2
        ), self.assertRaises(DaemonUnavailable) as raised:
            await player.attach()
        self.assertIn("in time", str(raised.exception))

    async def test_an_already_running_daemon_is_not_started_again_and_gets_the_url(self):
        await self.server.start()
        with patch.object(client_module.daemon, "spawn") as spawn:
            player = RemoteEngine(playlist_url="https://youtu.be/other", config_file="/tmp/c.json")
            self.addCleanup(player.close)
            await player.attach()
            await asyncio.sleep(0.1)
        spawn.assert_not_called()
        self.assertEqual(self.engine.calls, [("load_url", "https://youtu.be/other", True, False)])

    async def test_attaching_to_a_daemon_already_playing_that_url_does_not_reload_it(self):
        await self.server.start()
        player = RemoteEngine(playlist_url=self.engine.playlist_url, config_file="/tmp/c.json")
        self.addCleanup(player.close)
        await player.attach()
        await asyncio.sleep(0.1)
        self.assertEqual(self.engine.calls, [])

    async def test_force_cache_reloads_even_the_url_that_is_already_playing(self):
        await self.server.start()
        player = RemoteEngine(playlist_url=self.engine.playlist_url, config_file="/tmp/c.json", force_refresh=True)
        self.addCleanup(player.close)
        await player.attach()
        await asyncio.sleep(0.1)
        self.assertEqual(self.engine.calls, [("load_url", self.engine.playlist_url, True, True)])


class TestReconnecting(unittest.IsolatedAsyncioTestCase):
    async def test_a_lost_connection_is_reported_and_then_restored(self):
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="mpf-client-test-")
        os.chmod(tmp, 0o700)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        engine = FakeEngine(os.path.join(tmp, "config.json"))
        first = PlayerServer(engine)
        await first.start()
        player = RemoteEngine(config_file=os.path.join(tmp, "c.json"))
        self.addCleanup(player.close)
        await player.attach()
        self.assertTrue(player._connected)

        with patch.object(client_module.daemon, "spawn", return_value=False):
            first.close()  # the daemon goes away
            await asyncio.sleep(0.15)
            self.assertFalse(player._connected)
            self.assertIn("Lost connection", player._status_msg)
            second = PlayerServer(engine)  # and comes back before the retries run out
            await second.start()
            self.addCleanup(second.close)
            for _ in range(60):
                if player._connected:
                    break
                await asyncio.sleep(0.05)
        self.assertTrue(player._connected)
        self.assertEqual([t.id for t in player.queue.tracks], ["a", "b"])

    async def test_a_daemon_that_does_not_return_is_given_up_on(self):
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="mpf-client-test-")
        os.chmod(tmp, 0o700)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        server = PlayerServer(FakeEngine(os.path.join(tmp, "config.json")))
        await server.start()
        player = RemoteEngine(config_file=os.path.join(tmp, "c.json"))
        self.addCleanup(player.close)
        await player.attach()
        with patch.object(client_module.daemon, "spawn", return_value=False), patch.object(
            client_module, "RECONNECT_ATTEMPTS", 2
        ):
            server.close()
            for _ in range(60):
                if "gone" in player._status_msg:
                    break
                await asyncio.sleep(0.05)
        self.assertIn("The background player is gone", player._status_msg)
        self.assertFalse(player._connected)


class TestKeysBecomeCommands(ClientTestCase):
    """What pressing a key does in the TUI: it sends a command; the daemon's reply changes the screen."""

    async def _tui(self):
        player = await self.attached(MediaPlayerFluere)
        player._vim_mode = False
        return player

    async def test_playback_keys_send_their_commands(self):
        player = await self._tui()
        for key in ("n", " ", "m", "+", "-", "s", "r", "]"):  # the fake engine records only some of them
            player._handle_key(key)
        await self.settle()
        self.assertEqual(self.engine.calls, [("next",), ("set_paused", True)])

    async def test_a_key_the_player_does_not_know_does_not_get_in_the_way(self):
        from blessed.keyboard import Keystroke

        player = await self._tui()
        player._handle_key(Keystroke("x"))
        player._handle_key(Keystroke("n"))
        await self.settle()
        self.assertEqual(self.engine.calls, [("next",)])

    async def test_pressing_s_asks_for_shuffle_and_the_new_order_arrives_as_a_queue(self):
        player = await self._tui()
        sent = fake_daemon(player)
        player._handle_key("s")
        self.assertEqual(sent, [("set_shuffle", {"enabled": True})])

    async def test_fuzzy_search_cursor_selects_the_second_match_and_plays_it(self):
        player = await self._tui()
        sent = fake_daemon(player)
        player.queue = TrackQueue([Track("a", "Song A"), Track("b", "Song B")])
        player._input_mode = "search"
        player._input_buffer = "song"
        player.queue.set_filter("song")
        player._list_height = 3
        down = type("Key", (), {"name": "KEY_DOWN", "is_sequence": True})()

        player._handle_key(down)
        player._handle_key("\n")

        self.assertEqual(player._selected_list_idx, 1)
        self.assertEqual(sent, [("play_index", {"index": 1})])

    async def test_navigating_requests_an_immediate_redraw(self):
        player = await self._tui()
        player._vim_mode = True
        player.queue = TrackQueue([Track("a", "A"), Track("b", "B")])
        player._list_height = 3
        player._render_requested.clear()

        player._handle_key("j")

        self.assertTrue(player._render_requested.is_set())


if __name__ == "__main__":
    unittest.main()
