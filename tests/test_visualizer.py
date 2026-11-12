"""Spectrum analyzer behaviour on synthetic audio: band placement, dynamics, and latency."""

import unittest
from unittest.mock import patch

import numpy as np

from mpf_core.visualizer import PipeWireSpectrumAnalyzer

RATE = PipeWireSpectrumAnalyzer.SAMPLE_RATE
HOP = PipeWireSpectrumAnalyzer.CHUNK_SIZE


def feed(analyzer, signal):
    """Run float audio through the analyzer like the capture loop and return one frame per hop."""
    pcm = (np.clip(signal, -1, 1) * 32767).astype(np.int16)
    frames = []
    for start in range(0, len(pcm) - HOP + 1, HOP):
        analyzer._process_samples(pcm[start : start + HOP])
        frames.append(analyzer.get_bands()[0])
    return np.array(frames)


def pink_noise(seconds, seed=1):
    white = np.random.default_rng(seed).standard_normal(int(RATE * seconds))
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(len(white), 1 / RATE)
    spectrum[1:] /= np.sqrt(freqs[1:])
    pink = np.fft.irfft(spectrum, len(white))
    return pink / pink.std()


class TestSpectrumShape(unittest.TestCase):
    def test_a_tone_lights_the_band_that_contains_it(self):
        analyzer = PipeWireSpectrumAnalyzer()
        edges = np.geomspace(analyzer.MIN_FREQ, analyzer.MAX_FREQ, analyzer.num_bands + 1)
        seconds = np.arange(RATE * 2) / RATE
        for freq in (60, 100, 1000, 4000, 9000):
            with self.subTest(freq=freq):
                frame = feed(PipeWireSpectrumAnalyzer(), 0.3 * np.sin(2 * np.pi * freq * seconds))[-1]
                self.assertEqual(int(frame.argmax()), int(np.searchsorted(edges, freq)) - 1)

    def test_low_bands_are_distinct_not_copies_of_one_bin(self):
        frame = feed(PipeWireSpectrumAnalyzer(), 0.3 * np.sin(2 * np.pi * 100 * np.arange(RATE * 2) / RATE))[-1]
        self.assertGreater(len(set(np.round(frame[:8], 2))), 6)

    def test_pink_noise_is_roughly_flat_across_bands(self):
        means = feed(PipeWireSpectrumAnalyzer(), 0.25 * pink_noise(12))[100:].mean(axis=0)
        self.assertLess(means.max() - means.min(), 0.15)

    def test_silence_empties_the_bars(self):
        audio = np.concatenate([0.3 * pink_noise(3), np.zeros(RATE * 2)])
        self.assertLess(feed(PipeWireSpectrumAnalyzer(), audio)[-1].max(), 0.01)

    def test_dc_and_sub_bass_do_not_mute_the_music(self):
        music = 0.2 * pink_noise(6)
        plain = feed(PipeWireSpectrumAnalyzer(), music)[-1]
        offset = feed(PipeWireSpectrumAnalyzer(), music + 0.5)[-1]
        np.testing.assert_allclose(offset, plain, atol=0.05)


class TestSpectrumDynamics(unittest.TestCase):
    def test_beats_are_followed_within_two_hops_and_stay_dynamic(self):
        rng = np.random.default_rng(2)
        total, period = RATE * 10, int(0.469 * RATE)
        audio = 0.02 * rng.standard_normal(total)
        hit = np.arange(RATE // 3) / RATE
        starts = list(range(0, total - len(hit), period))
        for start in starts:
            audio[start : start + len(hit)] += 0.8 * np.sin(2 * np.pi * 60 * hit) * np.exp(-hit * 18)

        bass = feed(PipeWireSpectrumAnalyzer(), audio)[:, :6].mean(axis=1)

        onsets = [start // HOP for start in starts][3:-1]
        latency = [int(np.argmax(bass[o - 1 : o + 8])) - 1 for o in onsets]
        self.assertLessEqual(max(latency), 3)
        self.assertLessEqual(float(np.mean(latency)), 2.0)
        peaks = np.array([bass[o : o + 8].max() for o in onsets])
        valleys = np.array([bass[o + 14 : o + 19].min() for o in onsets])
        self.assertGreater(peaks.mean() - valleys.mean(), 0.4)

    def test_loud_passages_look_louder_than_quiet_ones(self):
        noise = pink_noise(10)
        gain = np.where(np.arange(len(noise)) < RATE * 5, 0.4, 0.04)
        frames = feed(PipeWireSpectrumAnalyzer(), noise * gain)
        self.assertGreater(frames[int(4.5 * RATE / HOP)].mean() - frames[int(5.3 * RATE / HOP)].mean(), 0.1)

    def test_volume_change_settles_back_to_the_same_height(self):
        noise = pink_noise(16)
        gain = np.where(np.arange(len(noise)) < RATE * 8, 0.3, 0.15)  # volume halved
        frames = feed(PipeWireSpectrumAnalyzer(), noise * gain)
        before, settled = frames[int(7.5 * RATE / HOP)].mean(), frames[int(15 * RATE / HOP)].mean()
        self.assertAlmostEqual(before, settled, delta=0.06)

    def test_a_hit_between_two_redraws_is_not_missed(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer._smooth_bands(np.array([1.0, 0, 0, 0], dtype=np.float32))
        for _ in range(8):  # decays for several steps before the UI reads
            analyzer._smooth_bands(np.zeros(4, dtype=np.float32))
        self.assertAlmostEqual(analyzer.get_bands()[0][0], analyzer.ATTACK, places=5)
        self.assertLess(analyzer.get_bands()[0][0], analyzer.ATTACK * 0.7)  # the next read has moved on

    def test_input_too_short_for_a_step_is_ignored(self):
        analyzer = PipeWireSpectrumAnalyzer()
        analyzer._process_samples(np.ones(HOP - 1, dtype=np.int16))
        self.assertEqual(analyzer.bands.sum(), 0)


class TestResampleAndCapture(unittest.TestCase):
    def test_widening_interpolates_instead_of_repeating_bands(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=4)
        analyzer.bands[:] = [0.0, 1.0, 0.5, 1.0]
        wide = np.array(analyzer.get_bands(10)[0])
        self.assertEqual(len(wide), 10)
        self.assertGreater(len(set(np.round(wide, 3))), 4)  # nearest-neighbour would give exactly 4
        self.assertAlmostEqual(wide[0], 0.0)
        self.assertAlmostEqual(wide[-1], 1.0)

    def test_narrowing_keeps_the_loudest_band_of_each_group(self):
        analyzer = PipeWireSpectrumAnalyzer(num_bands=6)
        analyzer.bands[:] = [0.1, 0.9, 0.2, 0.3, 0.8, 0.0]
        self.assertEqual(analyzer.get_bands(3)[0], np.array([0.9, 0.3, 0.8], dtype=np.float32).tolist())
        self.assertEqual(analyzer.get_bands(0), ([], []))

    def test_capture_commands_request_low_latency(self):
        analyzer = PipeWireSpectrumAnalyzer(target_node="mpv")
        with patch("mpf_core.visualizer.shutil.which", side_effect=lambda n: n if n == "pw-record" else None):
            cmd = analyzer._get_capture_command()
        self.assertEqual(cmd[cmd.index("--latency") + 1], "20ms")
        with patch("mpf_core.visualizer.shutil.which", side_effect=lambda n: n if n == "parec" else None):
            self.assertIn("--latency-msec=20", analyzer._get_capture_command())
