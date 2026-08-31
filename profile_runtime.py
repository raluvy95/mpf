"""Repeatable CPU and memory profile for long queues and spectrum processing."""

import argparse
import cProfile
import io
import pstats
import time
import tracemalloc

import numpy as np

from mpf_core.models import Track, TrackQueue
from mpf_core.visualizer import PipeWireSpectrumAnalyzer


def run(track_count: int, frames: int) -> None:
    tracemalloc.start()
    profiler = cProfile.Profile()
    profiler.enable()
    started = time.perf_counter()

    queue = TrackQueue([Track(str(i), f"Track {i}", 180.0, f"Artist {i % 100}") for i in range(track_count)])
    for query in ("track 9", "artist 42", "missing"):
        queue.set_filter(query)
        _ = queue.filtered_tracks
    queue.clear_filter()

    analyzer = PipeWireSpectrumAnalyzer(num_bands=32)
    rng = np.random.default_rng(42)
    samples = rng.integers(-32768, 32767, size=(frames, analyzer.CHUNK_SIZE), dtype=np.int16)
    for frame in samples:
        analyzer._process_samples(frame)

    elapsed = time.perf_counter() - started
    profiler.disable()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    output = io.StringIO()
    pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(12)
    print(f"tracks={track_count} frames={frames} elapsed={elapsed:.3f}s")
    print(f"memory_current={current / 1024 / 1024:.2f}MiB memory_peak={peak / 1024 / 1024:.2f}MiB")
    print(output.getvalue())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracks", type=int, default=10_000)
    parser.add_argument("--frames", type=int, default=5_000)
    args = parser.parse_args()
    run(args.tracks, args.frames)
