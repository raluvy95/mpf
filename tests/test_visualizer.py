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


def music_like(seconds, density_slope=4.5, drums=True, bpm=128, seed=3):
    """Noise bed whose spectral density falls ``density_slope`` dB/octave, plus kick and hi-hat bursts."""
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    gain = np.zeros_like(freqs)
    audible = freqs >= 30
    gain[audible] = (freqs[audible] / 1000.0) ** (-density_slope / 6.0206)
    bed = np.fft.irfft(spectrum * gain, n)
    signal = 0.12 * bed / bed.std()
    kicks = []
    if drums:
        beat = int(60 / bpm * RATE)
        t = np.arange(int(0.35 * RATE)) / RATE
        kick = 0.6 * np.sin(2 * np.pi * (45 * t + 35 / 40 * (1 - np.exp(-t * 40)))) * np.exp(-t * 14)
        for start in range(int(0.1 * RATE), n - len(t), beat):
            signal[start : start + len(t)] += kick
            kicks.append(start)
        th = np.arange(int(0.05 * RATE)) / RATE
        hat_spectrum = np.fft.rfft(rng.standard_normal(len(th) + 2000))
        hat_spectrum[np.fft.rfftfreq(len(th) + 2000, 1 / RATE) < 6000] = 0
        hat = np.fft.irfft(hat_spectrum)[: len(th)]
        hat = 0.07 * hat / hat.std() * np.exp(-th * 70)
        for start in range(int(0.1 * RATE) + beat // 2, n - len(th), beat):
            signal[start : start + len(th)] += hat
    return np.clip(signal, -1, 1), kicks


def band_groups(analyzer):
    edges = np.geomspace(analyzer.MIN_FREQ, analyzer.MAX_FREQ, analyzer.num_bands + 1)
    centres = np.sqrt(edges[:-1] * edges[1:])
    return centres < 250, (centres >= 250) & (centres < 4000), centres >= 4000


class TestSpectrumShape(unittest.TestCase):
    def test_a_tone_lights_the_band_that_contains_it(self):
        analyzer = PipeWireSpectrumAnalyzer()
        edges = np.geomspace(analyzer.MIN_FREQ, analyzer.MAX_FREQ, analyzer.num_bands + 1)
        seconds = np.arange(RATE * 2) / RATE
        for freq in (60, 100, 1000, 4000, 9000, 15000):
            with self.subTest(freq=freq):
                frame = feed(PipeWireSpectrumAnalyzer(), 0.3 * np.sin(2 * np.pi * freq * seconds))[-1]
                self.assertEqual(int(frame.argmax()), int(np.searchsorted(edges, freq)) - 1)

    def test_low_bands_are_distinct_not_copies_of_one_bin(self):
        frame = feed(PipeWireSpectrumAnalyzer(), 0.3 * np.sin(2 * np.pi * 60 * np.arange(RATE * 2) / RATE))[-1]
        hill = np.round(frame[2:8], 2)
        self.assertEqual(len(set(hill)), 6)  # a smooth hill of distinct bars, not runs of copies of one bin
        self.assertEqual(int(frame.argmax()), 5)
        self.assertLess(float(frame[10:].max()), 0.01)

    def test_range_covers_20_hz_to_20_khz_with_a_sensible_share_per_region(self):
        analyzer = PipeWireSpectrumAnalyzer()
        self.assertEqual((analyzer.MIN_FREQ, analyzer.MAX_FREQ, analyzer.SAMPLE_RATE), (20.0, 20_000.0, 44100))
        bass, mid, treble = (int(g.sum()) for g in band_groups(analyzer))
        self.assertTrue(9 <= bass <= 14, bass)
        self.assertTrue(10 <= mid <= 15, mid)
        self.assertGreaterEqual(treble, 6)

    def test_typical_music_reads_level_from_bass_to_treble(self):
        analyzer = PipeWireSpectrumAnalyzer()
        frames = feed(analyzer, music_like(10)[0])[100:]
        levels = [frames[:, g].mean() for g in band_groups(analyzer)]
        self.assertLess(max(levels) - min(levels), 0.1, levels)

    def test_pink_noise_leans_up_by_the_perceptual_tilt(self):
        analyzer = PipeWireSpectrumAnalyzer()
        means = feed(analyzer, 0.25 * pink_noise(12))[100:].mean(axis=0)
        octaves = np.log2(analyzer.MAX_FREQ / analyzer.MIN_FREQ)
        expected = analyzer.TILT_DB_PER_OCTAVE * octaves / (analyzer.DB_CEIL - analyzer.DB_FLOOR)
        slope = float(np.polyfit(np.linspace(0, 1, analyzer.num_bands), means, 1)[0])
        self.assertAlmostEqual(slope, -expected, delta=0.12) if slope < 0 else self.assertAlmostEqual(slope, expected, delta=0.12)

    def test_silence_empties_the_bars(self):
        audio = np.concatenate([0.3 * pink_noise(3), np.zeros(RATE * 2)])
        self.assertLess(feed(PipeWireSpectrumAnalyzer(), audio)[-1].max(), 0.01)

    def test_dc_and_sub_bass_do_not_mute_the_music(self):
        music = 0.2 * pink_noise(6)
        plain = feed(PipeWireSpectrumAnalyzer(), music)[-1]
        offset = feed(PipeWireSpectrumAnalyzer(), music + 0.5)[-1]
        np.testing.assert_allclose(offset, plain, atol=0.05)


class TestSpectrumDynamics(unittest.TestCase):
    def test_kicks_are_left_side_spikes(self):
        analyzer = PipeWireSpectrumAnalyzer()
        signal, kicks = music_like(12)
        frames = feed(analyzer, signal)
        bass, _mid, treble = band_groups(analyzer)
        onsets = [k // HOP for k in kicks][5:-2]
        rise = np.mean([frames[o : o + 6][:, bass].max() - frames[o - 3][bass].mean() for o in onsets])
        treble_rise = np.mean([abs(frames[o : o + 6][:, treble].mean() - frames[o - 3][treble].mean()) for o in onsets])
        self.assertGreater(rise, 0.3)
        self.assertLess(treble_rise, 0.1)
        peaks = [int(frames[o : o + 8][:, 3:8].mean(axis=1).argmax()) for o in onsets]
        self.assertLessEqual(max(peaks), 4)  # onset to peak within ~90 ms (window 46 ms + 23 ms hop)

    def test_every_band_responds_with_the_same_delay(self):
        """No frequency-dependent lag: a burst at any pitch peaks on its own bar at about the same time."""
        peak_hops = []
        for freq in (45, 150, 1000, 4000):
            analyzer = PipeWireSpectrumAnalyzer()
            edges = np.geomspace(analyzer.MIN_FREQ, analyzer.MAX_FREQ, analyzer.num_bands + 1)
            band = int(np.searchsorted(edges, freq)) - 1
            audio = 0.01 * np.random.default_rng(1).standard_normal(RATE * 6)
            t = np.arange(int(0.3 * RATE)) / RATE
            starts = list(range(RATE // 2, len(audio) - len(t), RATE // 2))
            for start in starts:
                audio[start : start + len(t)] += 0.5 * np.sin(2 * np.pi * freq * t) * np.exp(-t * 14)
            frames = feed(analyzer, audio)
            peak_hops.append(np.mean([int(frames[s // HOP : s // HOP + 8, band].argmax()) for s in starts[2:]]))
        self.assertLessEqual(max(peak_hops) - min(peak_hops), 1.5, peak_hops)
        self.assertLessEqual(max(peak_hops), 4.5, peak_hops)

    def test_a_tone_change_moves_bars_vertically_without_sliding_across(self):
        analyzer = PipeWireSpectrumAnalyzer()
        seconds = np.arange(RATE) / RATE
        audio = np.concatenate([0.2 * np.sin(2 * np.pi * 100 * seconds), 0.2 * np.sin(2 * np.pi * 5000 * seconds)])
        frames = feed(analyzer, audio)
        edges = np.geomspace(analyzer.MIN_FREQ, analyzer.MAX_FREQ, analyzer.num_bands + 1)
        low, high = int(np.searchsorted(edges, 100)) - 1, int(np.searchsorted(edges, 5000)) - 1
        switch = RATE // HOP
        for frame in frames[switch - 1 :]:
            self.assertTrue(np.all(frame[low + 5 : high - 4] < 0.15), "bars between the two tones must stay dark")
        self.assertGreater(frames[switch + 2][high], 0.8)  # the new tone is up within a few hops
        self.assertGreater(frames[switch + 4][low], frames[switch + 8][low])  # the old bar falls gradually
        self.assertGreater(frames[switch + 8][low], 0.2)

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
        self.assertEqual(cmd[cmd.index("--rate") + 1], "44100")
        with patch("mpf_core.visualizer.shutil.which", side_effect=lambda n: n if n == "parec" else None):
            self.assertIn("--latency-msec=20", analyzer._get_capture_command())
