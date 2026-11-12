"""PipeWire audio spectrum visualizer: log-spaced band energy in dB, slow AGC, fast attack / gravity decay."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class PipeWireSpectrumAnalyzer:
    """Captures audio stream specifically from MPV and computes live frequency bands using numpy."""

    SAMPLE_RATE = 22050
    CHUNK_SIZE = 512  # samples consumed per analysis step (~23 ms)
    FFT_SIZE = 1024  # sliding analysis window (~46 ms): short enough to stay on the beat
    MIN_FREQ = 40.0
    MAX_FREQ = 10_000.0
    DB_FLOOR = -45.0  # band energy relative to the recent loudness that maps to an empty bar
    DB_CEIL = -6.0  # ... and to a full bar
    TILT_DB_PER_OCTAVE = 0.5  # lift highs slightly; band energy on log-spaced bands is already pink-flat
    AGC_RELEASE_SECONDS = 3.0  # how slowly the reference level follows quieter music
    MIN_LEVEL = 3e-5  # reference level floor (about -90 dBFS), so near-silence still shows
    ATTACK = 0.8  # fraction of the way to a louder value covered per step
    DECAY = 0.9  # per-step multiplier when a band falls
    PEAK_FALL = 0.02

    def __init__(self, num_bands: int = 32, target_node: str = "mpv") -> None:
        self.num_bands = num_bands
        self.target_node = target_node
        self.bands = np.zeros(num_bands, dtype=np.float32)
        self.peaks = np.zeros(num_bands, dtype=np.float32)
        # Highest band value seen since the last get_bands(), so a short hit that lands between two
        # screen redraws (the UI draws far less often than we analyse) is never skipped.
        self._since_read = np.zeros(num_bands, dtype=np.float32)
        self.enabled = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()

        self._window = np.hanning(self.FFT_SIZE).astype(np.float32)
        # One-sided power scale so that the bin powers of a stationary signal sum to its mean square.
        self._power_scale = np.float32(2.0 / (self.FFT_SIZE * float(np.sum(self._window**2))))
        self._buffer = np.zeros(self.FFT_SIZE, dtype=np.float32)
        self._level = self.MIN_LEVEL**2  # slow reference power (the AGC)
        self._level_decay = float(np.exp(-2.0 * self.CHUNK_SIZE / self.SAMPLE_RATE / self.AGC_RELEASE_SECONDS))

        # Geometric band edges as fractional FFT-bin positions. Bin k covers [k, k + 1) here, hence +0.5
        # to centre it on its frequency. Fractional edges are evaluated on the cumulative power, so bass
        # bands narrower than one bin still get a smooth, distinct share instead of repeating a bin.
        edges_hz = np.geomspace(self.MIN_FREQ, self.MAX_FREQ, num_bands + 1)
        self._edge_bins = edges_hz * self.FFT_SIZE / self.SAMPLE_RATE + 0.5
        self._cum_axis = np.arange(self.FFT_SIZE // 2 + 2, dtype=np.float64)
        centres = np.sqrt(edges_hz[:-1] * edges_hz[1:])
        self._tilt_db = (self.TILT_DB_PER_OCTAVE * np.log2(centres / 1000.0)).astype(np.float32)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self.pause()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None

    def pause(self) -> None:
        """Stop audio capture and clear the display."""
        self.freeze()
        with self._lock:
            self.bands.fill(0.0)
            self.peaks.fill(0.0)

    def freeze(self) -> None:
        """Stop audio capture but keep the last frame, e.g. while playback is paused."""
        self.enabled = False
        with self._process_lock:
            process = self._process
            self._process = None
        if process:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
            except OSError as err:
                logger.debug("Unable to stop audio capture process: %s", err)

    def resume(self) -> None:
        """Resume audio capture."""
        self.enabled = True

    def _get_capture_command(self) -> Optional[List[str]]:
        if shutil.which("pw-record"):
            return [
                "pw-record",
                "--target",
                self.target_node,
                "--latency",
                "20ms",  # tighter delivery than the 100 ms default (worst read gap 43 -> 32 ms measured)
                "--rate",
                str(self.SAMPLE_RATE),
                "--channels",
                "1",
                "--format",
                "s16",
                "-",
            ]
        if shutil.which("parec"):
            return [
                "parec",
                "--format=s16le",
                "--latency-msec=20",
                f"--rate={self.SAMPLE_RATE}",
                "--channels=1",
            ]
        return None

    def _capture_loop(self) -> None:
        while self._running:
            if not self.enabled:
                time.sleep(0.1)
                continue

            cmd = self._get_capture_command()
            if not cmd:
                time.sleep(0.5)
                continue

            process: Optional[subprocess.Popen] = None
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=self.CHUNK_SIZE * 4,
                )
                with self._process_lock:
                    if not self._running or not self.enabled:
                        process.terminate()
                        continue
                    self._process = process
                stdout = process.stdout
                if not stdout:
                    time.sleep(0.1)
                    continue

                bytes_to_read = self.CHUNK_SIZE * 2
                while self._running and self.enabled:
                    raw_data = stdout.read(bytes_to_read)
                    if len(raw_data) < bytes_to_read:
                        if process.poll() is not None:
                            break
                        time.sleep(0.01)
                        continue

                    samples = np.frombuffer(raw_data, dtype=np.int16)
                    self._process_samples(samples)

            except (OSError, ValueError) as err:
                logger.warning("Audio capture failed: %s", err)
                time.sleep(0.5)
            finally:
                with self._process_lock:
                    is_current_process = self._process is process
                    if is_current_process:
                        self._process = None
                if process and is_current_process:
                    try:
                        process.terminate()
                    except OSError as err:
                        logger.debug("Unable to terminate audio capture process: %s", err)

    def _process_samples(self, samples: np.ndarray) -> None:
        if len(samples) < self.CHUNK_SIZE:
            return

        buf = self._buffer
        buf[: -self.CHUNK_SIZE] = buf[self.CHUNK_SIZE :]
        buf[-self.CHUNK_SIZE :] = samples[: self.CHUNK_SIZE].astype(np.float32) * np.float32(1.0 / 32768.0)

        if not buf.any():
            levels = np.zeros(self.num_bands, dtype=np.float32)
            self._level = max(self._level * self._level_decay, self.MIN_LEVEL**2)
        else:
            power = np.abs(np.fft.rfft(buf * self._window)) ** 2 * self._power_scale
            cumulative = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
            at_edges = np.interp(self._edge_bins, self._cum_axis, cumulative)
            band_power = np.maximum(np.diff(at_edges), 0.0)

            # Slow AGC on the energy the bars can actually show (DC and sub-bass would otherwise
            # swamp it): jump up to a louder level at once, fall back over seconds. Unlike
            # normalising every chunk, beats and quiet passages stay different, while MPV's volume
            # still cancels out.
            self._level = max(float(band_power.sum()), self._level * self._level_decay, self.MIN_LEVEL**2)
            decibels = 10.0 * np.log10(band_power / self._level + 1e-12) + self._tilt_db
            levels = np.clip(
                (decibels - self.DB_FLOOR) / (self.DB_CEIL - self.DB_FLOOR), 0.0, 1.0
            ).astype(np.float32)

        self._smooth_bands(levels)

    def _smooth_bands(self, new_bands: np.ndarray) -> None:
        """Apply attack, decay, and peak falloff to all bands at once."""
        with self._lock:
            attack = new_bands > self.bands
            self.bands = np.where(
                attack,
                self.bands + self.ATTACK * (new_bands - self.bands),
                self.bands * self.DECAY,
            ).astype(np.float32, copy=False)
            self.peaks = np.where(
                self.bands >= self.peaks,
                self.bands,
                np.maximum(0.0, self.peaks - self.PEAK_FALL),
            ).astype(np.float32, copy=False)
            np.maximum(self._since_read, self.bands, out=self._since_read)

    def get_bands(self, num_output_bands: Optional[int] = None) -> Tuple[List[float], List[float]]:
        with self._lock:
            bands = np.maximum(self.bands, self._since_read)
            self._since_read = self.bands.copy()
            peaks = self.peaks
            n = num_output_bands
            if n is None or n == self.num_bands:
                return bands.tolist(), peaks.tolist()
            if n <= 0:
                return [], []
            return self._resample(bands, n).tolist(), self._resample(peaks, n).tolist()

    def _resample(self, values: np.ndarray, n: int) -> np.ndarray:
        """Stretch by linear interpolation, or squeeze by keeping each group's maximum."""
        if n >= self.num_bands:
            return np.interp(np.linspace(0.0, self.num_bands - 1, n), np.arange(self.num_bands), values)
        starts = np.linspace(0, self.num_bands, n, endpoint=False).astype(np.intp)
        return np.maximum.reduceat(values, starts)
