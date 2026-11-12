"""Spectrum analyzer behaviour: capture format, band placement, levels, motion."""

import unittest
from unittest.mock import patch

import numpy as np

from mpf_core.visualizer import PipeWireSpectrumAnalyzer

SR = PipeWireSpectrumAnalyzer.SAMPLE_RATE
HOP = PipeWireSpectrumAnalyzer.HOP


def feed(analyzer, signal):
    """Push a float signal (-1..1) through the analyzer one hop at a time; return per-hop bars."""
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    frames = []
    for start in range(0, len(pcm) - HOP + 1, HOP):
        analyzer._process_samples(pcm[start : start + HOP])
        frames.append(analyzer.bands.copy())
    return np.array(frames)


def sine(freq, seconds=1.0, amplitude=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return amplitude * np.sin(2 * np.pi * freq * t)


class TestCapture(unittest.TestCase):
    def test_pw_record_is_raw(self):
        # Without --raw pw-record writes an AU header and big-endian samples, which read as
        # little-endian PCM turn the music into treble-heavy noise (a rising diagonal of bars).
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/pw-record" if name == "pw-record" else None):
            cmd = PipeWireSpectrumAnalyzer(target_node="mpv")._get_capture_command()
        self.assertIn("--raw", cmd)
        self.assertEqual(cmd[cmd.index("--target") + 1], "mpv")
        self.assertEqual(cmd[cmd.index("--rate") + 1], str(SR))
        self.assertEqual(cmd[cmd.index("--format") + 1], "s16")

    def test_parec_fallback_is_little_endian_mono(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/parec" if name == "parec" else None):
            cmd = PipeWireSpectrumAnalyzer()._get_capture_command()
        self.assertIn("--format=s16le", cmd)
        self.assertIn("--channels=1", cmd)

    def test_no_capture_tool(self):
        with patch("shutil.which", return_value=None):
            self.assertIsNone(PipeWireSpectrumAnalyzer()._get_capture_command())


class TestBands(unittest.TestCase):
    def test_tones_light_their_own_log_scale_band(self):
        edges = np.geomspace(20, 20_000, 33)
        for freq in (50, 100, 440, 1000, 4000, 10_000, 16_000):
            with self.subTest(freq=freq):
                bars = feed(PipeWireSpectrumAnalyzer(), sine(freq))[-1]
                expected = int(np.searchsorted(edges, freq) - 1)
                self.assertLessEqual(abs(int(np.argmax(bars)) - expected), 1)
                self.assertGreater(bars.max(), 0.9)

    def test_bass_is_left_and_treble_is_right(self):
        bass = feed(PipeWireSpectrumAnalyzer(), sine(80))[-1]
        treble = feed(PipeWireSpectrumAnalyzer(), sine(12_000))[-1]
        self.assertLess(np.argmax(bass), 8)
        self.assertGreater(np.argmax(treble), 24)

    def test_a_pure_tone_leaves_far_away_bars_empty(self):
        bars = feed(PipeWireSpectrumAnalyzer(), sine(1000))[-1]
        self.assertLess(float(bars[:6].max()), 0.01)
        self.assertLess(float(bars[-6:].max()), 0.01)

    def test_volume_does_not_change_bar_height(self):
        loud = feed(PipeWireSpectrumAnalyzer(), sine(440, amplitude=0.8))[-1]
        quiet = feed(PipeWireSpectrumAnalyzer(), sine(440, amplitude=0.2))[-1]
        np.testing.assert_allclose(loud, quiet, atol=0.02)

    def test_silence_is_empty(self):
        self.assertEqual(float(feed(PipeWireSpectrumAnalyzer(), np.zeros(SR))[-1].max()), 0.0)

    def test_typical_music_spectrum_reads_level_not_diagonal(self):
        # Noise whose spectral density falls 4.5 or 5.5 dB/octave, like typical music.
        rng = np.random.default_rng(7)
        n = SR * 4
        freqs = np.fft.rfftfreq(n, 1 / SR)
        white = np.fft.rfft(rng.standard_normal(n))
        for slope in (4.5, 5.5):
            with self.subTest(slope=slope):
                shape = np.zeros_like(freqs)
                shape[1:] = (freqs[1:] / 1000.0) ** (-slope / 6.0206)
                noise = np.fft.irfft(white * shape, n)
                noise = noise / np.max(np.abs(noise)) * 0.7
                levels = feed(PipeWireSpectrumAnalyzer(), noise)[10:].mean(axis=0)
                groups = [levels[:12].mean(), levels[12:24].mean(), levels[24:].mean()]  # bass / mid / treble
                self.assertLess(max(groups) - min(groups), 0.12, groups)

    def test_without_the_tilt_the_same_music_would_slope_diagonally(self):
        rng = np.random.default_rng(7)
        n = SR * 4
        freqs = np.fft.rfftfreq(n, 1 / SR)
        shape = np.zeros_like(freqs)
        shape[1:] = (freqs[1:] / 1000.0) ** (-4.5 / 6.0206)
        noise = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n)
        noise = noise / np.max(np.abs(noise)) * 0.7
        flat = PipeWireSpectrumAnalyzer()
        flat._tilt_db[:] = 0.0
        levels = feed(flat, noise)[10:].mean(axis=0)
        self.assertGreater(levels[:12].mean() - levels[24:].mean(), 0.3)

    def test_a_kick_is_a_left_side_spike_that_decays_smoothly(self):
        analyzer = PipeWireSpectrumAnalyzer()
        feed(analyzer, np.zeros(SR // 2))
        t = np.arange(HOP * 3) / SR
        kick = 0.8 * np.sin(2 * np.pi * 60 * t)
        frames = feed(analyzer, np.concatenate([kick, np.zeros(HOP * 20)]))
        peak_hop = int(np.argmax(frames[:, :8].max(axis=1)))
        self.assertLessEqual(peak_hop, 3)  # reacts within a hop or two
        self.assertGreater(frames[peak_hop, :8].max(), 0.9)
        self.assertLess(frames[peak_hop, 20:].max(), 0.1)  # nothing lights up on the right
        tail = frames[peak_hop + 1 :, :8].max(axis=1)
        self.assertTrue(np.all(np.diff(tail) <= 1e-6))  # falls, never jitters back up


class TestOutput(unittest.TestCase):
    def test_a_hit_between_two_reads_is_not_skipped(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer.bands[:] = [0, 0.9, 0, 0]
        analyzer._since_read[:] = [0, 0.9, 0, 0]
        analyzer.bands[:] = 0.0  # decayed again before the screen redraws
        self.assertAlmostEqual(analyzer.get_bands()[0][1], 0.9)
        self.assertEqual(analyzer.get_bands()[0][1], 0.0)

    def test_resampling_interpolates_when_wider_and_keeps_maxima_when_narrower(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer.bands[:] = [0.0, 1.0, 0.0, 0.5]
        analyzer._since_read[:] = analyzer.bands
        wide = analyzer.get_bands(7)[0]
        self.assertEqual(len(wide), 7)
        self.assertAlmostEqual(wide[1], 0.5)
        analyzer._since_read[:] = analyzer.bands
        self.assertEqual(analyzer.get_bands(2)[0], [1.0, 0.5])
        self.assertEqual(analyzer.get_bands(0), ([], []))
