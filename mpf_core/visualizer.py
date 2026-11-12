"""PipeWire spectrum analyzer: a numpy port of cava's own algorithm (cavacore, cava 1.0.0).

It is cava's default look, bar for bar: bass on the left, treble on the right, bars that fill like a
mountain range and fall with gravity. What is ported, from ``cavacore.c`` (the settings are cava's
defaults for the PipeWire input; the optional ones are off, as they are by default):

* Two Hann-windowed FFTs of the newest samples. At 48 kHz cava uses 4096 samples for mid/treble and
  8192 for bass (bars under 100 Hz), so low notes get real frequency resolution.
* Bar layout: bars spread logarithmically over 50 Hz..8 kHz (``lower_cutoff_freq``/``higher_cutoff_freq``),
  each bar sums the FFT magnitudes inside its bin range; neighbouring bass bars are pushed apart
  where the log scale would put them on the same bin.
* Per-bar EQ: ``f ** 0.85`` rising with frequency, divided by log2(FFT size) and the bar's bin count.
* ``noise_reduction = 0.77``: gravity fall-off (``cava_fall`` / ``cava_peak``) and an integral memory
  (``cava_mem``). Their constants are scaled by the framerate that cavacore *measures* from the amount
  of audio per call, so we feed it 800 samples (48000 / 60) per step and it behaves as cava at 60 fps.
* ``autosens = 1``: sensitivity drops 2% per overshooting step, creeps up 0.1% per step otherwise (10%
  per step until the first overshoot), and does not grow during digital silence.

Not used, because cava's defaults leave them off: ``monstercat``, ``waves``, the sleep timer and manual
sensitivity. Input is mono (cava averages its two channels' outputs in mono mode, equivalent for a
mono mix); samples keep cava's raw integer scale (-32768..32767).
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

NOISE_REDUCTION = 0.77  # cava [smoothing] noise_reduction = 77
LOW_CUT_OFF = 50  # cava [general] lower_cutoff_freq
HIGH_CUT_OFF = 8000  # cava [general] higher_cutoff_freq (the code default; the sample config says 10000)
BASS_CUT_OFF = 100  # Hz: bars below this are read from the long bass FFT
TARGET_FRAMERATE = 60  # cava [general] framerate


def fft_sizes(rate: int) -> Tuple[int, int]:
    """(mid/treble FFT size, bass FFT size) for a sample rate, as in ``cava_init``."""
    size = 512
    for limit, factor in ((8125, 1), (16250, 2), (32500, 4), (75000, 8), (150000, 16), (300000, 32)):
        if rate <= limit:
            return size * factor, size * factor * 2
    return size * 64, size * 128


def cava_layout(
    num_bars: int, rate: int, low_cut_off: int = LOW_CUT_OFF, high_cut_off: int = HIGH_CUT_OFF
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """Port of the bar layout in ``cava_init``.

    Returns (lower FFT bin, upper FFT bin, eq, cut-off frequency per bar edge, number of bass bars).
    Float32 arithmetic is kept where the C code uses ``float`` so bin boundaries truncate identically.
    """
    fft, bass_fft = fft_sizes(rate)
    f32 = np.float32
    lower = np.zeros(num_bars + 1, dtype=np.int64)
    upper = np.zeros(num_bars + 1, dtype=np.int64)
    cut = np.zeros(num_bars + 1, dtype=np.float32)
    relative = np.zeros(num_bars + 1, dtype=np.float32)

    frequency_constant = float(
        np.log10(f32(low_cut_off) / f32(high_cut_off)) / (f32(1) / (f32(num_bars) + f32(1)) - f32(1))
    )
    min_bandwidth = float(rate // bass_fft)
    bass_cut_off_bar = 0
    first_bar = 1

    for n in range(num_bars + 1):
        coefficient = frequency_constant * -1
        coefficient += float((f32(n) + f32(1)) / (f32(num_bars) + f32(1))) * frequency_constant
        cut[n] = high_cut_off * 10.0**coefficient
        if n > 0 and cut[n - 1] >= cut[n]:
            cut[n] = cut[n - 1] + min_bandwidth
        relative[n] = f32(cut[n]) / f32(rate // 2)

        if cut[n] < BASS_CUT_OFF:
            lower[n] = int(relative[n] * f32(bass_fft // 2))
            bass_cut_off_bar += 1
            if bass_cut_off_bar > 1:
                first_bar = 0
            lower[n] = min(lower[n], bass_fft // 2)
        else:
            lower[n] = int(np.ceil(relative[n] * f32(fft // 2)))
            if n == bass_cut_off_bar:
                first_bar = 1
                if n > 0:
                    upper[n - 1] = int(relative[n] * f32(bass_fft // 2)) - 1
            else:
                first_bar = 0
            lower[n] = min(lower[n], fft // 2)

        if n > 0:
            if not first_bar:
                upper[n - 1] = lower[n] - 1
                # push the spectrum up if the log scale clumps bars into the bass
                if lower[n] <= lower[n - 1]:
                    limit = bass_fft // 2 + 1 if n < bass_cut_off_bar else fft // 2 + 1
                    if lower[n - 1] + 1 < limit:
                        lower[n] = lower[n - 1] + 1
                        upper[n - 1] = lower[n] - 1
            elif upper[n - 1] < lower[n - 1]:
                upper[n - 1] = lower[n - 1] + 1

        size = bass_fft if n < bass_cut_off_bar else fft
        relative[n] = f32(lower[n]) / f32(size / 2)
        cut[n] = relative[n] * f32(rate / 2)

    eq = np.zeros(num_bars, dtype=np.float64)
    for n in range(num_bars):
        eq[n] = 1.0 / 2.0**28
        eq[n] *= float(cut[n + 1]) ** 0.85
        eq[n] /= np.log2(bass_fft if n < bass_cut_off_bar else fft)
        eq[n] /= max(1, upper[n] - lower[n] + 1)
    return lower[:num_bars], upper[:num_bars], eq, cut, bass_cut_off_bar


class PipeWireSpectrumAnalyzer:
    """Captures the audio stream of MPV and turns it into cava-style bars (0..1) with numpy."""

    SAMPLE_RATE = 48000  # cava's PipeWire input default
    HOP = SAMPLE_RATE // TARGET_FRAMERATE  # 800 new samples per step: cavacore then measures 60 fps
    PEAK_FALL = 0.02

    def __init__(self, num_bands: int = 32, target_node: str = "mpv") -> None:
        fft, bass_fft = fft_sizes(self.SAMPLE_RATE)
        if num_bands < 1 or num_bands > fft // 2 + 1:
            raise ValueError(f"num_bands must be between 1 and {fft // 2 + 1}")
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

        self._fft = fft
        self._bass_fft = bass_fft
        lower, upper, self._eq, self._cut_off, self._bass_bars = cava_layout(num_bands, self.SAMPLE_RATE)
        nb = self._bass_bars
        # Sums over inclusive bin ranges via a cumulative sum: [lower, upper] -> cs[upper + 1] - cs[lower].
        self._bass_lo, self._bass_hi = lower[:nb], np.maximum(upper[:nb] + 1, lower[:nb])
        self._mid_lo, self._mid_hi = lower[nb:], np.maximum(upper[nb:] + 1, lower[nb:])
        self._bass_window = np.hanning(bass_fft)
        self._mid_window = np.hanning(fft)
        # Only the bins some bar reads are worth taking magnitudes of.
        self._bass_bins = int(self._bass_hi.max()) if nb else 0
        self._mid_bins = int(self._mid_hi.max()) if nb < num_bands else 0
        self._reset_state()
        self.sens = 1.0  # autosens' running gain; kept across pauses like cava keeps it
        self.autosens = True  # cava [general] autosens; only tests switch it off, to compare raw smoothing

    def _reset_state(self) -> None:
        n = self.num_bands
        self._input = np.zeros(self._bass_fft, dtype=np.float64)  # oldest ... newest
        self._fall = np.zeros(n)
        self._mem = np.zeros(n)
        self._peak = np.zeros(n)
        self._prev = np.zeros(n)
        self._framerate = 75.0  # cavacore's initial estimate; converges to our step rate
        self._sens_init = True

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
            self._reset_state()

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

    # ── Analysis (cava_execute) ──────────────────────────────────────────────

    def _process_samples(self, samples: np.ndarray) -> None:
        """One ``cava_execute`` step on ``samples`` (raw s16 values, any length up to the bass FFT size)."""
        count = min(len(samples), self._bass_fft)
        if count < 1:
            return
        fresh = samples[-count:].astype(np.float64)
        silence = not fresh.any()

        # cavacore estimates its own framerate from how much audio arrives per call.
        self._framerate -= self._framerate / 64.0
        self._framerate += self.SAMPLE_RATE / count / 64.0

        buf = self._input
        buf[:-count] = buf[count:]
        buf[-count:] = fresh

        # FFT magnitudes of the newest bass_fft / fft samples (cava reverses time; the spectrum magnitude is the same).
        out = np.empty(self.num_bands, dtype=np.float64)
        if self._bass_bars:
            bass = np.abs(np.fft.rfft(buf * self._bass_window)[: self._bass_bins])
            bass_sum = np.concatenate(([0.0], np.cumsum(bass)))
            out[: self._bass_bars] = bass_sum[self._bass_hi] - bass_sum[self._bass_lo]
        if self._bass_bars < self.num_bands:
            mid = np.abs(np.fft.rfft(buf[-self._fft :] * self._mid_window)[: self._mid_bins])
            mid_sum = np.concatenate(([0.0], np.cumsum(mid)))
            out[self._bass_bars :] = mid_sum[self._mid_hi] - mid_sum[self._mid_lo]
        out *= self._eq * self.sens  # per-bar EQ, then autosens

        # Smoothing: gravity fall-off, then the integral memory.
        framerate_mod = 66.0 / self._framerate
        gravity_mod = framerate_mod**2.5 * 2.0 / NOISE_REDUCTION
        integral_mod = framerate_mod**0.1
        falling = out < self._prev
        fallen = np.maximum(self._peak * (1.0 - self._fall * self._fall * gravity_mod), 0.0)
        self._fall = np.where(falling, self._fall + 0.028, 0.0)
        self._peak = np.where(falling, self._peak, out)
        out = np.where(falling, fallen, out)
        self._prev = out
        out = self._mem * NOISE_REDUCTION / integral_mod + out
        self._mem = out  # the integral keeps the unclamped value, as cavacore does
        overshoot = bool((out > 1.0).any())
        out = np.minimum(out, 1.0)

        # Automatic sensitivity.
        if not self.autosens:
            pass
        elif overshoot:
            self.sens *= 1.0 - 0.02 * framerate_mod
            self._sens_init = False
        elif not silence:
            self.sens *= 1.0 + 0.001 * framerate_mod
            if self._sens_init:
                self.sens *= 1.0 + 0.1 * framerate_mod

        self._publish(out.astype(np.float32))

    def _publish(self, new_bands: np.ndarray) -> None:
        with self._lock:
            self.bands = new_bands
            self.peaks = np.where(
                new_bands >= self.peaks, new_bands, np.maximum(0.0, self.peaks - self.PEAK_FALL)
            ).astype(np.float32, copy=False)
            np.maximum(self._since_read, new_bands, out=self._since_read)

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
