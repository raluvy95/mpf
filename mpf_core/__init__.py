"""mpf_core package initialization with PEP 562 lazy loading."""

import importlib
from typing import Any

__all__ = [
    "BlessedMusicPlayer",
    "KittyPreviewer",
    "PipeWireSpectrumAnalyzer",
    "PlaybackState",
    "RepeatMode",
    "Track",
    "TrackQueue",
    "fetch_playlist_tracks",
    "format_time",
    "fuzzy_filter_tracks",
    "fuzzy_score",
    "is_playlist_cache_stale",
    "load_cached_playlist",
    "load_playback_state",
    "save_cached_playlist",
    "save_playback_state",
]

_LOOKUP = {
    "PlaybackState": ".cache",
    "is_playlist_cache_stale": ".cache",
    "load_cached_playlist": ".cache",
    "load_playback_state": ".cache",
    "save_cached_playlist": ".cache",
    "save_playback_state": ".cache",
    "fetch_playlist_tracks": ".fetcher",
    "fuzzy_filter_tracks": ".fuzzy",
    "fuzzy_score": ".fuzzy",
    "RepeatMode": ".models",
    "Track": ".models",
    "TrackQueue": ".models",
    "format_time": ".models",
    "BlessedMusicPlayer": ".player",
    "KittyPreviewer": ".previewer",
    "PipeWireSpectrumAnalyzer": ".visualizer",
}


def __getattr__(name: str) -> Any:
    if name in _LOOKUP:
        module = importlib.import_module(_LOOKUP[name], __name__)
        value = getattr(module, name)
        globals()[name] = value  # Cache it on module level so future lookups hit instant C speed
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
