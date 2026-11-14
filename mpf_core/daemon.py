"""Background mode: a headless player that outlives the TUI, controlled over a private unix socket.

The socket and a pid file live in $XDG_RUNTIME_DIR (0700, cleared at logout), or in the private cache
directory without one. The only commands are ``ping`` and ``quit``; the socket is 0600.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import stat
import subprocess
import sys
import time
from typing import Callable, Optional

from mpf_core.paths import CACHE_DIR

logger = logging.getLogger(__name__)

SOCKET_NAME = "mpf-daemon.sock"
PID_NAME = "mpf-daemon.pid"
_COMMAND_LIMIT = 64


class DaemonAlreadyRunning(RuntimeError):
    """Another background player already owns the control socket."""


def _state_dir() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    try:
        info = os.stat(runtime_dir)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
            return runtime_dir
    except OSError:
        pass
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    return CACHE_DIR


def socket_path() -> str:
    return os.path.join(_state_dir(), SOCKET_NAME)


def pid_path() -> str:
    return os.path.join(_state_dir(), PID_NAME)


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as err:
        logger.warning("Unable to remove %s: %s", path, err)


def send_command(command: str, timeout: float = 2.0) -> Optional[str]:
    """Send one command to the background player and return its reply, or None if none is running."""
    path = socket_path()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
        client.sendall(command.encode() + b"\n")
        return client.makefile("rb").readline(_COMMAND_LIMIT).decode(errors="replace").strip()
    except FileNotFoundError:
        return None
    except ConnectionRefusedError:
        # A socket file nobody listens on: left behind by a crashed daemon.
        _remove(path)
        _remove(pid_path())
        return None
    except OSError as err:
        logger.warning("Background player did not answer %r: %s", command, err)
        return None
    finally:
        client.close()


def is_running() -> bool:
    return send_command("ping") == "ok"


def stop(timeout: float = 10.0) -> bool:
    """Ask a running background player to quit and wait until it has saved its state and gone.

    Returns False if none was running; raises TimeoutError if it does not exit in time.
    """
    if send_command("quit") is None:
        return False
    deadline = time.monotonic() + timeout
    while is_running():
        if time.monotonic() >= deadline:
            raise TimeoutError("the background player did not exit in time")
        time.sleep(0.1)
    return True


def spawn(playlist_url: str, config_file: str) -> bool:
    """Start a detached headless player that resumes where the TUI stopped."""
    command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(sys.argv[0])]
    command += ["--daemon", "--config", config_file]
    if playlist_url:
        command.append(playlist_url)
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as err:
        logger.error("Unable to start the background player: %s", err)
        return False
    return True


class DaemonServer:
    def __init__(self, server: asyncio.AbstractServer, socket_file: str, pid_file: str) -> None:
        self._server = server
        self._socket_file = socket_file
        self._pid_file = pid_file

    def close(self) -> None:
        self._server.close()
        _remove(self._socket_file)
        _remove(self._pid_file)


async def serve(on_quit: Callable[[], None]) -> DaemonServer:
    """Listen for control commands; raises DaemonAlreadyRunning if another daemon is listening."""
    path = socket_path()
    if await asyncio.to_thread(is_running):  # off the loop: a probe must not block a loop that may be serving
        raise DaemonAlreadyRunning(f"another background player is listening on {path}")
    _remove(path)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 2.0)
            command = line[:_COMMAND_LIMIT].decode(errors="replace").strip()
            if command == "ping":
                reply = "ok"
            elif command == "quit":
                reply = "ok"
                on_quit()
            else:
                reply = "error: unknown command"
            writer.write(reply.encode() + b"\n")
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    previous_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(handle, path=path)
    finally:
        os.umask(previous_umask)
    os.chmod(path, 0o600)
    pid_file = pid_path()
    fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle_file:
        handle_file.write(str(os.getpid()))
    return DaemonServer(server, path, pid_file)
