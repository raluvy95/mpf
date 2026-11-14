# Client/daemon split

Status: design, approved section by section in conversation, awaiting written review.

## Problem

Quitting the TUI while music plays takes about 0.7 s and the screen stays frozen. The cause is the
hand-off: `q` starts a second headless MPF process that takes over the running mpv, and the TUI waits
until it answers. About 460 ms of that is the new process importing modules (numpy and blessed
included, though it draws nothing). The wait cannot be trimmed much, because the design asks one
process to move playback state into another at the moment the user is leaving.

`BlessedMusicPlayer` is one class of 95 methods that holds both the playback engine (mpv, queue,
playlist loading, prefetch, resume state, MPRIS, visualizer capture) and the UI (render loop, keys,
dialogs, thumbnails). MPRIS also reads its private fields (`_p.queue`, `_p._time_pos`).

## Decisions

| Question | Decision |
|---|---|
| Architecture | A long-lived daemon owns playback; clients only render and send input (state mirror over a socket). |
| Clients | Many at once, sharing state; any client may send commands, applied in arrival order. |
| Daemon lifetime | Resident until `mpf.py quit`; it does not exit when the last client leaves or when paused. |
| Class name | `BlessedMusicPlayer` is renamed `MediaPlayerFluere` and becomes the TUI client class. |

## Processes and modules

One daemon per user, listening on the existing socket (`$XDG_RUNTIME_DIR`, else the private cache
directory; mode 0600). `mpf.py` connects to it and starts it first if it is not running; only that first
start pays the daemon's startup, shown as a "starting" screen. Closing a client closes its connection and
nothing else, so quitting is immediate and the music is untouched.

| Module | Role |
|---|---|
| `engine.py`: `PlaybackEngine` | Daemon side. mpv, `TrackQueue`, playlist loading and caching, prefetch and read-ahead, resume state, MPRIS, visualizer capture and analysis. Imports no `blessed`. |
| `protocol.py` | Message schema, protocol version, size limits, encode and validate. |
| `server.py` | Accepts clients, registry, broadcast, dispatches commands to the engine, drops slow clients. |
| `client.py` | Connection, handshake, local state mirror, spawns the daemon when absent. |
| `player.py`: `MediaPlayerFluere` | TUI client. Rendering and input only, reading the mirror and sending commands. |

Entry points: `mpf.py` (client), `mpf.py --daemon` (engine and server), `mpf.py quit` (shutdown command).

Launch semantics:
- `mpf.py` with no URL attaches and leaves playback as it is. On a daemon with an empty queue it asks it to
  load the saved default playlist.
- `mpf.py URL` sends `load_url`. `--no-auto-play` loads without starting playback. `--force-cache` sends
  `force_refresh` with the load.
- `--no-visualizer` and `--no-preview` are client-only. `--adopt-ipc` and the hand-off code are removed.
- MPRIS belongs to the daemon, so media keys and the desktop widget keep working with no client attached.

## Protocol

Unix stream socket, one UTF-8 JSON object per line, each with a type field `t`. Unknown types are ignored;
field types are validated on both sides.

Limits: a client line is at most 64 KiB; a server frame at most 1 MiB; a client whose unsent data exceeds
4 MiB is disconnected.

**Handshake.** Client sends `hello` with an integer protocol version (starting at 1). The daemon replies
`welcome`, or an error and closes. Because the daemon outlives upgrades, a version mismatch makes the client
show "the background player is an older version, run `mpf.py quit` to update"; it never stops playback itself.

**State sync**
- `snapshot` after `welcome`: current track, play state (paused, buffering, idle), position, duration, volume,
  mute, repeat, shuffle, playlist URL, loading flag, status message, queue revision.
- `queue` frames of up to 500 tracks each, using the cache's compact tuple format; resent only when the queue
  revision changes.
- `state` frames carry only changed fields; position at no more than 10 Hz, everything else at once.
- `bands`: about 32 floats at no more than 30 frames per second, only to clients that sent `subscribe_bands`.
  Capture runs only while at least one client is subscribed and audio plays. The learned gain therefore lives
  as long as the daemon.

**Commands (client to daemon):** `play_index`, `set_paused`, `next`, `prev`, `seek_rel`, `seek_abs`,
`set_volume`, `set_mute`, `set_shuffle`, `cycle_repeat`, `load_url`, `force_refresh`, `set_playback_config`
(default playlist, fallback clients), `subscribe_bands`, `shutdown`. Only `load_url` is answered, with `ack` or
`error`, so an invalid URL still appears under the open dialog.

**On the client:** fuzzy search over the mirrored queue (no round trip), selection, scroll, dialogs, help and
settings screens, thumbnails, and the display preferences (visualizer style, bar position, colour mode, Vim
keys, open view), which stay in `config.json`. The daemon writes only the playback keys (default playlist,
fallback clients); the existing atomic config writes cover the two writers.

## Build order

Each step is one commit with pytest, ruff and mypy green and the app working.

1. Rename `BlessedMusicPlayer` to `MediaPlayerFluere` (code, tests, docs, exports).
2. `protocol.py` with tests.
3. Extract `PlaybackEngine`; the player runs it in-process. MPRIS uses the engine's interface, not private fields.
4. `server.py`; daemon mode becomes engine plus server, replacing the headless, adopt and hand-off code.
5. `client.py` and the state mirror; `MediaPlayerFluere` renders from the mirror; default launch attaches.
6. Remove the old hand-off, adopt and spawn-on-quit paths; `mpf.py quit` shuts the daemon down; update the README.

## Failure handling

- Client crash or Ctrl-C: it disconnects; playback continues.
- Daemon crash: clients show "lost connection" and offer a restart; resume state is saved every 3 s.
- Daemon fails to start: the client shows the error and no music starts.
- Stale socket or pid file: removed by the existing cleanup.
- Simultaneous commands: applied in order by the daemon's event loop.

## Testing

- `protocol.py`: unit tests, including oversize and malformed input.
- Engine: tests with a fake mpv, as the player tests do now.
- Server and client: integration tests over a real unix socket in a temporary runtime directory, with a fake
  engine and no audio.
- Audio and visualizer: a synthetic signal played into PipeWire with `pw-play --target 0` and captured with
  the real `pw-record` command. Never the user's music or system audio.
- Latency: measure quit time before (about 700 ms) and after, and attach time.

## Success criteria

- Quitting a client while music plays takes under 50 ms.
- Music does not gap when a client quits or attaches.
- Every existing feature keeps working.

## Risks

- Step 3 is the largest; many existing tests drive the single-process player and need reworking.
- The wire protocol must be stable from the start; the version field and the handshake are the escape hatch.
