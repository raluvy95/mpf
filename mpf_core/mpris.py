"""MPRIS2 D-Bus service so desktops (KDE lock screen, media keys) can show and control MPF."""
# ruff: noqa: F722, F821
# mypy: disable-error-code="name-defined,valid-type,no-redef"
# dbus-fast reads D-Bus type signatures ("s", "a{sv}", ...) from the annotations.

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from dbus_fast import NameFlag, RequestNameReply, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

if TYPE_CHECKING:
    from mpf_core.player import BlessedMusicPlayer

logger = logging.getLogger(__name__)

BUS_NAME = "org.mpris.MediaPlayer2.mpf"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
_US = 1_000_000


def _track_path(track_id: str) -> str:
    """D-Bus object paths only allow [A-Za-z0-9_]; YouTube ids also contain '-'."""
    return "/org/mpf/track/" + "".join(c if c.isalnum() else "_" for c in track_id)


class _Root(ServiceInterface):
    def __init__(self, player: "BlessedMusicPlayer") -> None:
        super().__init__("org.mpris.MediaPlayer2")
        self._player = player

    @method()
    def Raise(self) -> None:
        pass

    @method()
    def Quit(self) -> None:
        self._player._running = False

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "MPF"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class _Player(ServiceInterface):
    def __init__(self, player: "BlessedMusicPlayer") -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._p = player

    # ── Methods ────────────────────────────────────────────────────────────
    @method()
    def Next(self) -> None:
        self._p.next_track()

    @method()
    def Previous(self) -> None:
        self._p.previous_track()

    @method()
    def Pause(self) -> None:
        self._p.set_paused(True)

    @method()
    def PlayPause(self) -> None:
        self._p.set_paused(not self._p._is_paused)

    @method()
    def Stop(self) -> None:
        self._p.set_paused(True)

    @method()
    def Play(self) -> None:
        self._p.set_paused(False)

    @method()
    def Seek(self, offset: "x") -> None:
        self._p.seek_relative(offset / _US)

    @method()
    def SetPosition(self, track_id: "o", position: "x") -> None:
        current = self._p.queue.current_track
        if current and track_id == _track_path(current.id):
            self._p.seek_absolute(position / _US)

    @method()
    def OpenUri(self, uri: "s") -> None:
        pass

    @signal()
    def Seeked(self, position: "x") -> "x":
        return position

    # ── Properties ─────────────────────────────────────────────────────────
    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._p.mpris_playback_status()

    @dbus_property()
    def LoopStatus(self) -> "s":
        return self._p.mpris_loop_status()

    @LoopStatus.setter
    def LoopStatus(self, value: "s") -> None:
        self._p.set_loop_status(value)

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def Shuffle(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._p.mpris_metadata()

    @dbus_property()
    def Volume(self) -> "d":
        return self._p._volume / 100

    @Volume.setter
    def Volume(self, value: "d") -> None:
        self._p.set_volume(round(value * 100))

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return int(self._p._time_pos * _US)

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return bool(self._p.queue.tracks)

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return bool(self._p.queue.tracks)

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return self._p.queue.current_track is not None

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return self._p.queue.current_track is not None

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return self._p.queue.current_track is not None

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True


def build_metadata(track: Any, duration: float, art_path: str = "") -> Dict[str, Variant]:
    """MPRIS ``Metadata`` for a track; ``duration`` falls back to the track's own.

    ``art_path`` is a local thumbnail; shells like the lock screen often cannot load remote art.
    """
    if track is None:
        return {"mpris:trackid": Variant("o", NO_TRACK)}
    meta: Dict[str, Variant] = {
        "mpris:trackid": Variant("o", _track_path(track.id)),
        "xesam:title": Variant("s", track.title),
        "xesam:url": Variant("s", track.url),
        "mpris:artUrl": Variant("s", Path(art_path).as_uri() if art_path else track.thumbnail),
    }
    if track.uploader:
        meta["xesam:artist"] = Variant("as", [track.uploader])
    length = duration or track.duration or 0
    if length > 0:
        meta["mpris:length"] = Variant("x", int(length * _US))
    return meta


class MprisService:
    """Owns the session-bus connection and publishes player state changes."""

    def __init__(self, player: "BlessedMusicPlayer") -> None:
        self._player = player
        self._bus: Optional[MessageBus] = None
        self._iface: Optional[_Player] = None
        self._last: Dict[str, Any] = {}
        self._last_pos = 0.0

    async def start(self) -> bool:
        """Connect and claim the name. Never raises: MPRIS is optional."""
        try:
            bus = await MessageBus().connect()
            root, self._iface = _Root(self._player), _Player(self._player)
            bus.export(OBJECT_PATH, root)
            bus.export(OBJECT_PATH, self._iface)
            reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
            if reply != RequestNameReply.PRIMARY_OWNER:
                # Another mpf already owns the name; use the spec's per-instance form.
                await bus.request_name(f"{BUS_NAME}.instance{os.getpid()}")
            self._bus = bus
            return True
        except Exception:
            logger.warning("MPRIS unavailable; continuing without it", exc_info=True)
            self._bus = None
            return False

    def sync(self) -> None:
        """Emit PropertiesChanged for whatever differs since the last call (cheap; called often)."""
        iface, p = self._iface, self._player
        if not self._bus or not iface:
            return
        try:
            current = {
                "PlaybackStatus": p.mpris_playback_status(),
                "LoopStatus": p.mpris_loop_status(),
                "Volume": p._volume / 100,
                "Metadata": (p.queue.current_track.id if p.queue.current_track else None, round(p._duration), bool(p.mpris_art_path())),
                "CanGoNext": bool(p.queue.tracks),
                "CanGoPrevious": bool(p.queue.tracks),
                "CanPlay": p.queue.current_track is not None,
            }
            changed: Dict[str, Any] = {}
            for name, value in current.items():
                if self._last.get(name) == value:
                    continue
                changed[name] = p.mpris_metadata() if name == "Metadata" else value
                if name == "CanPlay":
                    changed["CanPause"] = changed["CanSeek"] = value
            self._last = current
            if changed:
                iface.emit_properties_changed(changed)

            # Position isn't signalled; clients extrapolate. Announce only jumps (seek/track change).
            pos = p._time_pos
            expected = self._last_pos + (0.2 if p.mpris_playback_status() == "Playing" else 0.0)
            if abs(pos - expected) > 1.5 and "Metadata" not in changed:
                iface.Seeked(int(pos * _US))
            self._last_pos = pos
        except Exception:
            logger.debug("MPRIS sync failed", exc_info=True)

    def stop(self) -> None:
        if self._bus:
            self._bus.disconnect()
            self._bus = None
