"""PipeWire audio spectrum visualizer with smooth asymmetric EMA and spatial filtering."""

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
    CHUNK_SIZE = 512

    def __init__(self, num_bands: int = 32, target_node: str = "mpv") -> None:
        self.num_bands = num_bands
        self.target_node = target_node
        self.bands = [0.0] * num_bands
        self.peaks = [0.0] * num_bands
        self.enabled = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._window = np.hanning(self.CHUNK_SIZE).astype(np.float32)
        num_fft_bins = self.CHUNK_SIZE // 2 + 1
        starts = []
        ends = []
        for band_idx in range(self.num_bands):
            start_bin = int((band_idx / self.num_bands) ** 2 * (num_fft_bins - 1))
            end_bin = max(start_bin + 1, int(((band_idx + 1) / self.num_bands) ** 2 * num_fft_bins))
            starts.append(start_bin)
            ends.append(min(num_fft_bins, end_bin))
        self._band_starts = np.asarray(starts, dtype=np.intp)
        self._band_ends = np.asarray(ends, dtype=np.intp)
        self._band_widths = self._band_ends - self._band_starts
        self._band_boosts = 1.0 + (np.arange(self.num_bands, dtype=np.float32) / self.num_bands) * 2.2

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
        """Pause audio capture and kill backend recording subprocess."""
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
        with self._lock:
            self.bands = [0.0] * self.num_bands
            self.peaks = [0.0] * self.num_bands

    def resume(self) -> None:
        """Resume audio capture."""
        self.enabled = True

    def _get_capture_command(self) -> Optional[List[str]]:
        if shutil.which("pw-record"):
            return [
                "pw-record",
                "--target",
                self.target_node,
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

        norm_samples = (samples[: self.CHUNK_SIZE].astype(np.float32) / 32768.0) * self._window
        rms = float(np.sqrt(np.mean(np.square(norm_samples))))
        if not np.any(samples):
            raw_bands = np.zeros(self.num_bands, dtype=np.float32)
        else:
            # Audio capture is post-mix, so remove absolute output gain before
            # the FFT. This keeps MPV's volume independent from bar height.
            norm_samples /= rms
            fft_result = np.abs(np.fft.rfft(norm_samples))
            num_fft_bins = len(fft_result)
            if num_fft_bins == 0:
                return

            cumulative = np.concatenate(([0.0], np.cumsum(fft_result)))
            means = (cumulative[self._band_ends] - cumulative[self._band_starts]) / self._band_widths
            raw_bands = np.clip(
                np.log1p(means * 0.025 * self._band_boosts) / np.log1p(8.0),
                0.0,
                1.0,
            )

        # 1. Spatial smoothing across neighboring bands (Gaussian 3-tap filter)
        spatial_bands = np.copy(raw_bands)
        if self.num_bands >= 3:
            spatial_bands[1:-1] = (
                0.22 * raw_bands[:-2] + 0.56 * raw_bands[1:-1] + 0.22 * raw_bands[2:]
            )

        # 2. Temporal smoothing (Asymmetric EMA attack/decay)
        with self._lock:
            for i in range(self.num_bands):
                target = float(spatial_bands[i])
                if target > self.bands[i]:
                    # Fast attack to catch beats
                    self.bands[i] = self.bands[i] + 0.65 * (target - self.bands[i])
                else:
                    # Gentle exponential decay
                    self.bands[i] = max(0.0, self.bands[i] * 0.88)

                # Smooth peak falloff
                if self.bands[i] >= self.peaks[i]:
                    self.peaks[i] = self.bands[i]
                else:
                    self.peaks[i] = max(0.0, self.peaks[i] - 0.02)

    def get_bands(self, num_output_bands: Optional[int] = None) -> Tuple[List[float], List[float]]:
        with self._lock:
            if num_output_bands is None or num_output_bands == self.num_bands:
                return list(self.bands), list(self.peaks)

            n = num_output_bands
            res_bands = [0.0] * n
            res_peaks = [0.0] * n
            for i in range(n):
                src_idx = int((i / n) * self.num_bands)
                src_idx = min(self.num_bands - 1, src_idx)
                res_bands[i] = self.bands[src_idx]
                res_peaks[i] = self.peaks[src_idx]
            return res_bands, res_peaks
