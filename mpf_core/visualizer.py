"""PipeWire spectrum analyzer: log-frequency bars, bass on the left, treble on the right.

Pipeline, once per ~23 ms hop:
  raw s16 PCM -> 2048-sample Hann window -> FFT magnitude (0 dBFS = full-scale sine)
  -> 32 log-spaced bands, 20 Hz..20 kHz (peak magnitude per band) -> dB
  -> +4 dB/octave tilt around 1 kHz (music falls 4.5-5.5 dB/oct, so this levels it out)
  -> slow auto-gain (bars are relative to the recent loudest band, so MPV's volume cancels)
  -> fast attack, gravity decay.

Bands never mix with their neighbours, so every bar rises and falls on its own, vertically.
"""

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
    """Captures the audio stream of MPV and computes live frequency bands using numpy."""

    SAMPLE_RATE = 44100
    HOP = 1024  # new samples per analysis step (~23 ms)
    FFT_SIZE = 2048  # analysis window (~46 ms, 21.5 Hz bins): short enough to stay on the beat
    MIN_FREQ = 20.0
    MAX_FREQ = 20_000.0
    # Around 1 kHz, on the per-band peak magnitude. Music falls 4.5-5.5 dB/oct; the peak over the many bins
    # of a wide treble band reads a little high, so 4 levels typical music to within a few percent.
    TILT_DB_PER_OCTAVE = 4.0
    RANGE_DB = 48.0  # dynamic range shown: the loudest recent band is the top, this far below is empty
    REF_FALL_DB_PER_SECOND = 3.0  # how slowly the reference follows quieter music
    MIN_REF_DB = -40.0  # never amplify below this (dBFS), so quiet passages stay quiet
    SILENCE = 1e-5  # peak magnitude (about -100 dBFS) under which the input counts as silent
    ATTACK = 0.8  # fraction of the way to a louder value covered per step
    DECAY = 0.9  # per-step multiplier when a bar falls
    PEAK_FALL = 0.02

    def __init__(self, num_bands: int = 32, target_node: str = "mpv") -> None:
        self.num_bands = num_bands
        self.target_node = target_node
        self.bands = np.zeros(num_bands, dtype=np.float32)
        self.peaks = np.zeros(num_bands, dtype=np.float32)
        # Highest bar seen since the last get_bands(), so a hit that lands between two screen
        # redraws (the UI draws less often than we analyse) is never skipped.
        self._since_read = np.zeros(num_bands, dtype=np.float32)
        self.enabled = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()

        self._window = np.hanning(self.FFT_SIZE).astype(np.float32)
        self._mag_scale = np.float32(2.0 / float(self._window.sum()))  # full-scale sine -> magnitude 1.0
        self._buffer = np.zeros(self.FFT_SIZE, dtype=np.float32)
        self._ref_db = self.MIN_REF_DB
        self._ref_fall = self.REF_FALL_DB_PER_SECOND * self.HOP / self.SAMPLE_RATE

        # Band layout. Bin k sits at k * SAMPLE_RATE / FFT_SIZE Hz. A band at least one bin wide takes the
        # peak of the bins inside it; the narrow bass bands (finer than the FFT can resolve) read the
        # spectrum interpolated at their centre frequency, so they stay smooth instead of repeating a bin.
        edges_hz = np.geomspace(self.MIN_FREQ, self.MAX_FREQ, num_bands + 1)
        edge_bins = edges_hz * self.FFT_SIZE / self.SAMPLE_RATE
        centre_hz = np.sqrt(edges_hz[:-1] * edges_hz[1:])
        self._tilt_db = (self.TILT_DB_PER_OCTAVE * np.log2(centre_hz / 1000.0)).astype(np.float32)
        self._num_bins = int(np.ceil(edge_bins[-1])) + 1
        self._bin_axis = np.arange(self._num_bins, dtype=np.float64)
        wide = np.diff(edge_bins) >= 1.0
        self._first_wide = int(np.argmax(wide)) if wide.any() else num_bands
        self._wide_starts = np.ceil(edge_bins[self._first_wide : num_bands]).astype(np.intp)
        self._narrow_centres = centre_hz[: self._first_wide] * self.FFT_SIZE / self.SAMPLE_RATE

    # ── Lifecycle ────────────────────────────────────────────────────────────

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
            self._since_read.fill(0.0)
        self._buffer.fill(0.0)
        self._ref_db = self.MIN_REF_DB

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

    # ── Capture ──────────────────────────────────────────────────────────────

    def _get_capture_command(self) -> Optional[List[str]]:
        if shutil.which("pw-record"):
            return [
                "pw-record",
                # Without --raw, pw-record writes a Sun AU header and big-endian samples to stdout.
                # Read as native little-endian PCM that is byte-swapped noise with a rising,
                # treble-heavy spectrum instead of the music.
                "--raw",
                "--target",
                self.target_node,
                "--latency",
                "20ms",
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
                    bufsize=self.HOP * 4,
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

                bytes_to_read = self.HOP * 2
                while self._running and self.enabled:
                    raw_data = stdout.read(bytes_to_read)
                    if len(raw_data) < bytes_to_read:
                        if process.poll() is not None:
                            break
                        time.sleep(0.01)
                        continue
                    self._process_samples(np.frombuffer(raw_data, dtype="<i2"))

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

    # ── Analysis ─────────────────────────────────────────────────────────────

    def _band_magnitudes(self, samples: np.ndarray) -> np.ndarray:
        """Slide ``samples`` into the window and return the peak magnitude (0 dBFS = 1.0) of each band."""
        buf = self._buffer
        buf[: -len(samples)] = buf[len(samples) :]
        buf[-len(samples) :] = samples.astype(np.float32) * np.float32(1.0 / 32768.0)
        spectrum = np.abs(np.fft.rfft((buf - buf.mean()) * self._window))[: self._num_bins] * self._mag_scale
        magnitudes = np.empty(self.num_bands, dtype=np.float32)
        magnitudes[: self._first_wide] = np.interp(self._narrow_centres, self._bin_axis, spectrum)
        if self._first_wide < self.num_bands:
            magnitudes[self._first_wide :] = np.maximum.reduceat(spectrum, self._wide_starts)
        return magnitudes

    def _process_samples(self, samples: np.ndarray) -> None:
        if len(samples) != self.HOP:
            return
        magnitudes = self._band_magnitudes(samples)
        if magnitudes.max() < self.SILENCE:
            levels = np.zeros(self.num_bands, dtype=np.float32)
            self._ref_db = max(self.MIN_REF_DB, self._ref_db - self._ref_fall)
        else:
            decibels = 20.0 * np.log10(np.maximum(magnitudes, 1e-9)) + self._tilt_db
            # Jump up to a louder reference at once, fall back over seconds: beats stay taller than
            # the passages around them, while the player's volume setting still cancels out.
            self._ref_db = max(float(decibels.max()), self._ref_db - self._ref_fall, self.MIN_REF_DB)
            levels = np.clip(
                (decibels - (self._ref_db - self.RANGE_DB)) / self.RANGE_DB, 0.0, 1.0
            ).astype(np.float32)
        self._smooth_bands(levels)

    def _smooth_bands(self, new_bands: np.ndarray) -> None:
        """Fast attack, gravity decay, and falling peak markers, per band."""
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

    # ── Output ───────────────────────────────────────────────────────────────

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
