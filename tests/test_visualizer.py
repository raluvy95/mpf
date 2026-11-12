"""The spectrum analyzer is a port of cava 1.0.0's cavacore; expected values come from running cava itself."""

import unittest
from unittest.mock import patch

import numpy as np

from mpf_core.visualizer import PipeWireSpectrumAnalyzer, cava_layout, fft_sizes

SR = PipeWireSpectrumAnalyzer.SAMPLE_RATE
HOP = PipeWireSpectrumAnalyzer.HOP


def feed(analyzer, signal):
    """Push a float signal (-1..1) through the analyzer one step at a time; return the bars after each step."""
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    frames = []
    for start in range(0, len(pcm) - HOP + 1, HOP):
        analyzer._process_samples(pcm[start : start + HOP])
        frames.append(analyzer.bands.astype(float))
    return np.array(frames)


def sine(freq, seconds=3.0, amplitude=0.5):
    return amplitude * np.sin(2 * np.pi * freq * np.arange(int(SR * seconds)) / SR)


class TestCapture(unittest.TestCase):
    def test_pw_record_is_raw(self):
        # Without --raw pw-record writes an AU header and big-endian samples, which read as
        # little-endian PCM turn the music into treble-heavy noise (a rising diagonal of bars).
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/pw-record" if name == "pw-record" else None):
            cmd = PipeWireSpectrumAnalyzer(target_node="mpv")._get_capture_command()
        self.assertIn("--raw", cmd)
        self.assertEqual(cmd[cmd.index("--target") + 1], "mpv")
        self.assertEqual(cmd[cmd.index("--rate") + 1], str(SR))
        self.assertEqual(cmd[cmd.index("--channels") + 1], "1")
        self.assertEqual(cmd[cmd.index("--format") + 1], "s16")

    def test_parec_fallback_is_little_endian_mono(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/parec" if name == "parec" else None):
            cmd = PipeWireSpectrumAnalyzer()._get_capture_command()
        self.assertIn("--format=s16le", cmd)
        self.assertIn("--channels=1", cmd)

    def test_no_capture_tool(self):
        with patch("shutil.which", return_value=None):
            self.assertIsNone(PipeWireSpectrumAnalyzer()._get_capture_command())

    def test_step_size_makes_cavacore_measure_its_default_60_fps(self):
        self.assertEqual(SR / HOP, 60)


class TestLayout(unittest.TestCase):
    def test_fft_sizes_follow_the_sample_rate(self):
        self.assertEqual(fft_sizes(48000), (4096, 8192))
        self.assertEqual(fft_sizes(44100), (4096, 8192))
        self.assertEqual(fft_sizes(22050), (2048, 4096))

    def test_32_bars_at_48k_match_cava(self):
        lower, upper, _eq, cut, bass_bars = cava_layout(32, SR)
        self.assertEqual(bass_bars, 5)  # bars under 100 Hz use the long bass FFT
        self.assertEqual(lower[:5].tolist(), [8, 9, 11, 13, 16])
        self.assertEqual(upper[:5].tolist(), [8, 10, 12, 15, 17])
        self.assertEqual((lower[5], upper[5]), (10, 11))  # first mid bar, 4096-point FFT
        self.assertEqual((lower[31], upper[31]), (583, 682))
        self.assertTrue(np.all(np.diff(lower[:5]) > 0))  # strictly rising inside the bass FFT ...
        self.assertTrue(np.all(np.diff(lower[5:]) > 0))  # ... and inside the mid/treble FFT
        self.assertTrue(np.all(upper >= lower))

    def test_bars_span_50hz_to_8khz(self):
        _lower, _upper, _eq, cut, _bass = cava_layout(32, SR)
        self.assertAlmostEqual(float(cut[0]), 47, delta=3)
        self.assertAlmostEqual(float(cut[32]), 8000, delta=100)

    def test_eq_boosts_higher_bars_per_bin(self):
        eq = PipeWireSpectrumAnalyzer()._eq
        self.assertTrue(np.all(eq > 0))

    def test_rejects_impossible_bar_counts(self):
        with self.assertRaises(ValueError):
            PipeWireSpectrumAnalyzer(num_bands=0)


class TestResponse(unittest.TestCase):
    def test_tones_light_the_same_bar_as_cava(self):
        # Steady-state bar of a sine, measured on cava 1.0.0 (pipewire input, defaults, 32 mono bars).
        for freq, bar in ((40, 0), (100, 4), (300, 11), (1000, 18), (3000, 25), (6000, 30)):
            with self.subTest(freq=freq):
                bars = feed(PipeWireSpectrumAnalyzer(), sine(freq))[-1]
                self.assertEqual(int(np.argmax(bars)), bar)
                self.assertGreater(bars.max(), 0.9)  # cava settles at about 0.97
                self.assertLessEqual(bars.max(), 1.0)

    def test_a_tone_between_two_bass_bars_lights_both_like_cava(self):
        bars = feed(PipeWireSpectrumAnalyzer(), sine(50))[-1]  # cava: bar 0 0.96, bar 1 0.73
        self.assertGreater(bars[0], 0.9)
        self.assertAlmostEqual(float(bars[1]), 0.73, delta=0.05)

    def test_a_pure_tone_leaves_distant_bars_empty(self):
        bars = feed(PipeWireSpectrumAnalyzer(), sine(1000))[-1]
        self.assertLess(bars[:12].max(), 0.02)
        self.assertLess(bars[24:].max(), 0.02)

    def test_bass_is_left_and_treble_is_right(self):
        self.assertLess(int(np.argmax(feed(PipeWireSpectrumAnalyzer(), sine(80))[-1])), 6)
        self.assertGreater(int(np.argmax(feed(PipeWireSpectrumAnalyzer(), sine(7000))[-1])), 26)

    def test_bars_stay_within_zero_and_one(self):
        rng = np.random.default_rng(1)
        frames = feed(PipeWireSpectrumAnalyzer(), rng.uniform(-1, 1, SR * 2))
        self.assertGreaterEqual(frames.min(), 0.0)
        self.assertLessEqual(frames.max(), 1.0)

    def test_autosens_makes_volume_irrelevant(self):
        loud = feed(PipeWireSpectrumAnalyzer(), sine(440, amplitude=0.8))[-1]
        quiet = feed(PipeWireSpectrumAnalyzer(), sine(440, amplitude=0.02))[-1]
        self.assertEqual(int(np.argmax(loud)), int(np.argmax(quiet)))
        self.assertAlmostEqual(float(loud.max()), float(quiet.max()), delta=0.05)

    def test_autosens_lowers_the_gain_when_bars_overshoot(self):
        analyzer = PipeWireSpectrumAnalyzer()
        feed(analyzer, sine(300, seconds=0.5, amplitude=0.02))
        raised = analyzer.sens
        self.assertGreater(raised, 1.0)
        feed(analyzer, sine(300, seconds=2.0, amplitude=0.9))
        self.assertLess(analyzer.sens, raised)

    def test_silence_is_empty_and_does_not_grow_the_gain(self):
        analyzer = PipeWireSpectrumAnalyzer()
        frames = feed(analyzer, np.zeros(SR * 2))
        self.assertEqual(float(frames.max()), 0.0)
        self.assertEqual(analyzer.sens, 1.0)

    def test_measured_framerate_settles_at_60(self):
        analyzer = PipeWireSpectrumAnalyzer()
        feed(analyzer, sine(300, seconds=6.0))  # it starts at cavacore's 75 and moves 1/64 of the way per step
        self.assertAlmostEqual(analyzer._framerate, 60.0, delta=0.5)


class TestMotion(unittest.TestCase):
    def burst(self):
        """A 0.15 s burst of 100 Hz (bar 4) with autosens off, so the raw smoothing is visible."""
        analyzer = PipeWireSpectrumAnalyzer()
        analyzer.autosens = False
        signal = np.zeros(SR * 2)
        signal[SR // 2 : SR // 2 + int(0.15 * SR)] = sine(100, seconds=0.15, amplitude=0.05)
        return feed(analyzer, signal)[:, 4]

    def test_rises_quickly_and_peaks_near_cava_level(self):
        trace = self.burst()
        onset = int(np.argmax(trace > 0.02))
        self.assertAlmostEqual(float(trace.max()), 0.16, delta=0.03)  # cava: 0.161 for the same burst
        self.assertLess(int(np.argmax(trace)) - onset, 14)

    def test_falls_with_gravity_and_never_flickers_back_up(self):
        trace = self.burst()
        after_peak = trace[int(np.argmax(trace)) :]
        self.assertTrue(np.all(np.diff(after_peak) <= 1e-9))
        self.assertLess(after_peak[30], 0.03)  # cava is down to 0.01 about 24 frames after the peak
        self.assertGreater(after_peak[3], after_peak.max() * 0.8)  # a fall, not a cliff


class TestOutput(unittest.TestCase):
    def test_peaks_fall_slower_than_bars(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer._publish(np.array([1.0, 0.5, 0.0, 0.0], dtype=np.float32))
        analyzer._publish(np.zeros(4, dtype=np.float32))
        np.testing.assert_allclose(analyzer.peaks, [1.0 - analyzer.PEAK_FALL, 0.5 - analyzer.PEAK_FALL, 0.0, 0.0], rtol=1e-6)

    def test_a_hit_between_two_reads_is_not_skipped(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer._publish(np.array([0.0, 0.9, 0.0, 0.0], dtype=np.float32))
        analyzer._publish(np.zeros(4, dtype=np.float32))  # already fallen again before the screen redraws
        self.assertAlmostEqual(analyzer.get_bands()[0][1], 0.9, places=5)
        self.assertEqual(analyzer.get_bands()[0][1], 0.0)

    def test_resampling_interpolates_when_wider_and_keeps_maxima_when_narrower(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer._publish(np.array([0.0, 1.0, 0.0, 0.5], dtype=np.float32))
        wide = analyzer.get_bands(7)[0]
        self.assertEqual(len(wide), 7)
        self.assertAlmostEqual(wide[1], 0.5)
        analyzer._since_read[:] = analyzer.bands
        self.assertEqual(analyzer.get_bands(2)[0], [1.0, 0.5])
        self.assertEqual(analyzer.get_bands(0), ([], []))

    def test_pause_clears_the_display_but_keeps_the_learned_gain(self):
        analyzer = PipeWireSpectrumAnalyzer()
        feed(analyzer, sine(300, seconds=1.0, amplitude=0.02))
        gain = analyzer.sens
        analyzer.pause()
        self.assertEqual(float(analyzer.bands.max()), 0.0)
        self.assertEqual(float(analyzer.peaks.max()), 0.0)
        self.assertEqual(analyzer.sens, gain)
        self.assertEqual(float(np.abs(analyzer._input).max()), 0.0)
