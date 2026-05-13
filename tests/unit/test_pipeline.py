"""Smoke tests for the analyzer + synthesizer pipeline."""

import numpy as np

from mulchy.analyzer import CYCLE_SAMPLES, VOICES, analyze
from mulchy.synthesizer import SOURCE_LIFETIME_S, Synthesizer


def _synth():
    return Synthesizer(
        sample_rate=22050, base_freq=80.0,
        audio_enabled=False, record_debug_wav=False,
    )


def test_analyze_returns_voices_and_features(black_frame):
    voices, features = analyze(black_frame)
    assert voices.shape == (VOICES, CYCLE_SAMPLES)
    assert voices.dtype == np.float32
    assert set(features.keys()) >= {
        "brightness", "saturation", "edge_density", "hue", "motion",
    }
    for k, v in features.items():
        assert 0.0 <= v <= 1.0, f"feature {k} out of range: {v}"


def test_analyze_white_frame_is_bright(white_frame):
    _, features = analyze(white_frame)
    assert features["brightness"] > 0.9


def test_analyze_black_frame_is_dark(black_frame):
    _, features = analyze(black_frame)
    assert features["brightness"] < 0.1


def test_voices_in_unit_range(gradient_frame):
    voices, _ = analyze(gradient_frame)
    assert voices.min() >= -1.001
    assert voices.max() <= 1.001


def test_voice_endpoints_match(gradient_frame):
    voices, _ = analyze(gradient_frame)
    for i in range(VOICES):
        v = voices[i]
        assert abs(float(v[0] - v[-1])) < 1e-3


def test_update_mixes_into_ring(gradient_frame):
    """One update() call should put non-zero audio into the ring buffer."""
    synth = _synth()
    voices, features = analyze(gradient_frame)
    synth.update(voices, features)
    # Ring buffer should now contain some non-zero samples.
    assert np.abs(synth._ring).max() > 0.0
    # Should never exceed ±1 due to the tanh stage.
    assert np.abs(synth._ring).max() <= 1.0


def test_ring_buffer_contains_source_lifetime(gradient_frame):
    """After one frame, audio should extend roughly SOURCE_LIFETIME_S +
    REVERB_DURATION_S into the buffer ahead of the read cursor."""
    synth = _synth()
    voices, features = analyze(gradient_frame)
    synth.update(voices, features)
    # Find the run of non-zero samples after read_pos.
    arr = synth._ring
    nonzero = np.flatnonzero(np.abs(arr) > 1e-5)
    assert len(nonzero) > 0
    extent_samples = nonzero.max() - nonzero.min() + 1
    extent_seconds = extent_samples / synth.sr
    assert SOURCE_LIFETIME_S * 0.5 < extent_seconds < SOURCE_LIFETIME_S + 4.0


def test_repeated_updates_accumulate(gradient_frame):
    """Multiple frames should layer; second update raises energy compared to
    a single-frame baseline in regions where the two windows overlap."""
    synth_a = _synth()
    synth_b = _synth()
    voices, features = analyze(gradient_frame)
    synth_a.update(voices, features)
    energy_one = float(np.abs(synth_a._ring).sum())

    synth_b.update(voices, features)
    # Simulate the audio thread advancing by 0.2 s, then a second update.
    advance = int(0.2 * synth_b.sr)
    with synth_b._lock:
        synth_b._read_pos += advance
    synth_b.update(voices, features)
    energy_two = float(np.abs(synth_b._ring).sum())

    # Two overlapping frames should produce more total energy than one.
    assert energy_two > energy_one * 1.2
