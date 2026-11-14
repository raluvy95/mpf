"""The playback server: handshake, state sync, commands and slow clients, over a real unix socket."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mpf_core import daemon, server
from mpf_core.models import RepeatMode, Track, TrackQueue
from mpf_core.protocol import PROTOCOL_VERSION, QUEUE_CHUNK, decode, encode
from mpf_core.server import PlayerServer


class FakeEngine:
    """The part of PlaybackEngine the server uses, recording what it is asked to do."""

    def __init__(self, config_file):
        self.config_file = config_file
        self.playlist_url = "https://www.youtube.com/playlist?list=PLabc"
        self.queue = TrackQueue([Track("a", "A", 10.0, "U"), Track("b", "B")])
        self.paused = False
        self.position = 0.0
        self.status = "ready"
        self.on_change = None
        self.calls = []
        self.quit_requested = False
        self._ytdl_fallback_clients = ()

    def state(self):
        return {
            "playlist_url": self.playlist_url,
            "current_idx": self.queue.current_idx,
            "paused": self.paused,
            "position": self.position,
            "repeat": RepeatMode.ALL.value,
            "status": self.status,
            "queue_revision": self.queue.revision,
        }

    def _notify(self):
        if self.on_change:
            self.on_change()

    def _request_quit(self):
        self.quit_requested = True

    def _request_handover(self):
        return None

    def _play_index(self, index):
        self.calls.append(("play_index", index))

    def set_paused(self, paused):
        self.calls.append(("set_paused", paused))
        self.paused = paused

    def next_track(self):
        self.calls.append(("next",))

    def load_url(self, url, auto_play=None, force=False):
        self.calls.append(("load_url", url, auto_play, force))


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, version=PROTOCOL_VERSION):
        reader, writer = await asyncio.open_unix_connection(daemon.socket_path())
        client = cls(reader, writer)
        client.writer.write(encode({"t": "hello", "version": version}))
        await client.writer.drain()
        return client

    async def get(self, timeout=2.0):
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        return decode(line, 2**22) if line else None

    async def get_until(self, kind, timeout=2.0):
        while True:
            message = await self.get(timeout)
            if message is None or message["t"] == kind:
                return message

    def send(self, message):
        self.writer.write(encode(message))

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mpf-server-test-")
        os.chmod(self._tmp, 0o700)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self._tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.engine = FakeEngine(os.path.join(self._tmp, "config.json"))
        self.server = PlayerServer(self.engine)
        await self.server.start()
        self.addCleanup(self.server.close)
        self.clients = []

    async def connect(self, **kwargs):
        client = await Client.connect(**kwargs)
        self.clients.append(client)
        return client

    async def asyncTearDown(self):
        for client in self.clients:
            client.writer.close()

    async def welcome(self):
        client = await self.connect()
        self.assertEqual((await client.get())["t"], "welcome")
        snapshot = await client.get()
        self.assertEqual(snapshot["t"], "snapshot")
        await client.get_until("queue")
        return client


class TestHandshake(ServerTestCase):
    async def test_hello_gets_welcome_snapshot_and_queue(self):
        client = await self.connect()
        self.assertEqual(await client.get(), {"t": "welcome", "version": PROTOCOL_VERSION})
        snapshot = await client.get()
        self.assertEqual(snapshot["t"], "snapshot")
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["playlist_url"], self.engine.playlist_url)
        queue = await client.get()
        self.assertEqual((queue["t"], queue["chunk"], queue["chunks"]), ("queue", 0, 1))
        self.assertEqual([row[0] for row in queue["tracks"]], ["a", "b"])

    async def test_wrong_version_is_told_and_disconnected(self):
        client = await self.connect(version=PROTOCOL_VERSION + 1)
        message = await client.get()
        self.assertEqual((message["t"], message["code"], message["version"]), ("error", "version", PROTOCOL_VERSION))
        self.assertIsNone(await client.get())  # then the server closes the connection

    async def test_garbage_first_line_just_disconnects(self):
        reader, writer = await asyncio.open_unix_connection(daemon.socket_path())
        writer.write(b"this is not json\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2.0), b"")
        writer.close()
        self.assertEqual(self.server.client_count, 0)

    async def test_the_plain_text_control_lines_still_work(self):
        self.assertEqual(await asyncio.to_thread(daemon.send_command, "ping"), "ok")
        self.assertEqual(await asyncio.to_thread(daemon.send_command, "quit"), "ok")
        self.assertTrue(self.engine.quit_requested)

    async def test_the_socket_stays_private(self):
        self.assertEqual(os.stat(daemon.socket_path()).st_mode & 0o777, 0o600)


class TestSync(ServerTestCase):
    async def test_a_long_queue_arrives_in_chunks(self):
        self.engine.queue.set_tracks([Track(f"id{i}", f"T{i}") for i in range(QUEUE_CHUNK * 2 + 7)])
        client = await self.connect()
        await client.get_until("snapshot")
        chunks = [await client.get() for _ in range(3)]
        self.assertEqual([c["chunk"] for c in chunks], [0, 1, 2])
        self.assertTrue(all(c["chunks"] == 3 for c in chunks))
        self.assertEqual([len(c["tracks"]) for c in chunks], [QUEUE_CHUNK, QUEUE_CHUNK, 7])

    async def test_changed_fields_reach_every_client(self):
        first, second = await self.welcome(), await self.welcome()
        self.engine.paused = True
        self.engine.status = "paused"
        self.engine._notify()
        for client in (first, second):
            message = await client.get_until("state")
            self.assertEqual({k: v for k, v in message.items() if k != "t"}, {"paused": True, "status": "paused"})

    async def test_a_burst_of_changes_is_one_frame(self):
        client = await self.welcome()
        for status in ("one", "two", "three"):
            self.engine.status = status
            self.engine._notify()
        message = await client.get_until("state")
        self.assertEqual(message["status"], "three")
        with self.assertRaises(asyncio.TimeoutError):
            await client.get(timeout=0.05)  # nothing else queued behind it

    async def test_position_is_picked_up_without_any_notification(self):
        client = await self.welcome()
        self.engine.position = 12.5
        message = await client.get_until("state", timeout=1.0)  # the periodic tick sends it
        self.assertEqual(message["position"], 12.5)

    async def test_a_new_queue_is_resent(self):
        client = await self.welcome()
        self.engine.queue.set_tracks([Track("z", "Z")])
        self.engine._notify()
        queue = await client.get_until("queue")
        self.assertEqual([row[0] for row in queue["tracks"]], ["z"])


class TestCommands(ServerTestCase):
    async def test_valid_commands_reach_the_engine(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "set_paused", "paused": True})
        client.send({"t": "cmd", "name": "next"})
        client.send({"t": "cmd", "name": "play_index", "index": 1})
        await asyncio.sleep(0.1)
        self.assertEqual(self.engine.calls, [("set_paused", True), ("next",), ("play_index", 1)])

    async def test_an_index_outside_the_queue_is_ignored(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "play_index", "index": 99})
        client.send({"t": "cmd", "name": "play_index", "index": -1})
        await asyncio.sleep(0.1)
        self.assertEqual(self.engine.calls, [])

    async def test_an_invalid_command_gets_an_error_and_the_client_stays(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "play_index", "index": "x"})
        error = await client.get_until("error")
        self.assertEqual(error["code"], "command")
        client.send({"t": "cmd", "name": "next"})
        await asyncio.sleep(0.1)
        self.assertEqual(self.engine.calls, [("next",)])

    async def test_load_url_is_checked_and_acknowledged(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "load_url", "url": "https://example.com/x"})
        self.assertEqual((await client.get_until("error"))["code"], "load_url")
        client.send({"t": "cmd", "name": "load_url", "url": "https://youtu.be/abc", "auto_play": False})
        self.assertEqual((await client.get_until("ack"))["name"], "load_url")
        self.assertEqual(self.engine.calls, [("load_url", "https://youtu.be/abc", False, False)])

    async def test_shutdown_asks_the_engine_to_quit(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "shutdown"})
        await asyncio.sleep(0.1)
        self.assertTrue(self.engine.quit_requested)

    async def test_an_oversized_line_disconnects_that_client_only(self):
        good, bad = await self.welcome(), await self.welcome()
        bad.writer.write(b"x" * (server.MAX_CLIENT_LINE + 10) + b"\n")
        await asyncio.sleep(0.2)
        self.assertEqual(self.server.client_count, 1)
        good.send({"t": "cmd", "name": "next"})
        await asyncio.sleep(0.1)
        self.assertEqual(self.engine.calls, [("next",)])


class TestSlowClients(ServerTestCase):
    async def test_a_client_that_falls_too_far_behind_is_dropped(self):
        slow, fine = await self.welcome(), await self.welcome()
        with patch.object(server, "MAX_BACKLOG", 2000):
            self.engine.queue.set_tracks([Track(f"id{i}", "T" * 40) for i in range(200)])
            self.engine._notify()
            await asyncio.sleep(0.2)
        self.assertEqual(self.server.client_count, 0)  # every frame was over the (patched) cap
        del slow, fine


class TestClose(ServerTestCase):
    async def test_closing_the_server_disconnects_clients_and_removes_the_socket(self):
        client = await self.welcome()
        self.server.close()
        self.assertIsNone(await client.get_until("never"))
        self.assertFalse(os.path.exists(daemon.socket_path()))


class TestPlaybackConfig(ServerTestCase):
    async def test_fallback_clients_are_saved_and_applied(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "set_playback_config", "fallback_clients": ["tv_simply", "mweb"]})
        self.assertEqual((await client.get_until("ack"))["name"], "set_playback_config")
        self.assertEqual(self.engine._ytdl_fallback_clients, ("tv_simply", "mweb"))
        with open(self.engine.config_file, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["ytdl_fallback_clients"], ["tv_simply", "mweb"])

    async def test_invalid_client_names_are_refused(self):
        client = await self.welcome()
        client.send({"t": "cmd", "name": "set_playback_config", "fallback_clients": ["bad name!"]})
        self.assertEqual((await client.get_until("error"))["code"], "config")
        self.assertEqual(self.engine._ytdl_fallback_clients, ())


if __name__ == "__main__":
    unittest.main()
