"""Unit tests for mulchy.synthesizer."""

import numpy as np
import pytest

from mulchy import config as cfg
from mulchy.synthesizer import synthesize


@pytest.fixture
def nominal_features():
    """Minimal valid ImageFeatures for synthesizer tests."""
    return {
        "scanlines": [[0.5] * 320] * 8,
        "hue_centers": [0.0, 120.0, 240.0],
        "hue_weights": [0.4, 0.35, 0.25],
        "texture_scores": [0.3, 0.3, 0.3, 0.3],
        "brightness": 0.5,
        "saturation": 0.5,
        "edge_density": 0.5,
        "luminance_mean": 0.5,
        "luminance_variance": 0.1,
        "motion_amount": 0.0,
        "motion_cx": 0.0,
        "motion_cy": 0.0,
    }


# ── Output shape and type ─────────────────────────────────────────────────────

def test_returns_float32(nominal_features, clean_config):
    audio = synthesize(nominal_features)
    assert audio.dtype == np.float32


def test_output_length(nominal_features, clean_config):
    expected = int(cfg.SAMPLE_RATE * cfg.AUDIO_SECONDS)
    audio = synthesize(nominal_features)
    assert len(audio) == expected


def test_output_length_respects_audio_seconds(nominal_features, clean_config):
    cfg.AUDIO_SECONDS = 0.5
    expected = int(cfg.SAMPLE_RATE * 0.5)
    audio = synthesize(nominal_features)
    assert len(audio) == expected


# ── Amplitude bounds ──────────────────────────────────────────────────────────

def test_amplitude_bounded_by_master_volume(nominal_features, clean_config):
    audio = synthesize(nominal_features)
    assert np.max(np.abs(audio)) <= cfg.MASTER_VOLUME + 1e-5


def test_master_volume_zero_gives_silence(nominal_features, clean_config):
    cfg.MASTER_VOLUME = 0.0
    audio = synthesize(nominal_features)
    assert np.max(np.abs(audio)) == pytest.approx(0.0, abs=1e-6)


# ── Taper / boundary ──────────────────────────────────────────────────────────

def test_taper_applied_at_full_dip(nominal_features, clean_config):
    cfg.CROSSFADE_SMOOTHNESS = 0.0  # maximum taper (~40ms)
    audio = synthesize(nominal_features)
    # First and last samples are in the fade-in/out region; must be near zero
    assert abs(audio[0]) < 0.01
    assert abs(audio[-1]) < 0.01


def test_taper_shorter_at_high_smoothness(nominal_features, clean_config):
    # At smoothness=1.0 taper is only ~2ms; boundary samples non-zero for active audio
    cfg.CROSSFADE_SMOOTHNESS = 1.0
    cfg.LAYER_TONAL_LEVEL = 1.0
    cfg.LAYER_GLITCH_LEVEL = 0.0
    cfg.LAYER_RHYTHM_LEVEL = 0.0
    audio = synthesize(nominal_features)
    # With a 2ms taper the first sample is still near zero (it's the first taper sample)
    # but the first sample several ms in should have non-zero amplitude
    taper_2ms = int(cfg.SAMPLE_RATE * 0.002)
    mid = taper_2ms + 100
    assert np.max(np.abs(audio[mid:mid+100])) > 0.0


# ── Layers ────────────────────────────────────────────────────────────────────

def test_glitch_layer_in_isolation(clean_config):
    cfg.LAYER_GLITCH_LEVEL = 1.0
    cfg.LAYER_TONAL_LEVEL  = 0.0
    cfg.LAYER_RHYTHM_LEVEL = 0.0
    rng = np.random.default_rng(42)
    # Use varied scanlines so glitch layer produces non-zero output
    features = {
        "scanlines": [rng.random(320).tolist() for _ in range(8)],
        "hue_centers": [0.0, 120.0, 240.0],
        "hue_weights": [0.4, 0.35, 0.25],
        "texture_scores": [0.3, 0.3, 0.3, 0.3],
        "brightness": 0.5, "saturation": 0.5, "edge_density": 0.5,
        "luminance_mean": 0.5, "luminance_variance": 0.1,
        "motion_amount": 0.0, "motion_cx": 0.0, "motion_cy": 0.0,
    }
    audio = synthesize(features)
    assert audio.dtype == np.float32
    assert np.max(np.abs(audio)) > 0.0


def test_empty_scanlines_does_not_crash(nominal_features, clean_config):
    cfg.LAYER_GLITCH_LEVEL = 1.0
    cfg.LAYER_TONAL_LEVEL  = 0.0
    cfg.LAYER_RHYTHM_LEVEL = 0.0
    features = {**nominal_features, "scanlines": []}
    audio = synthesize(features)
    assert len(audio) == int(cfg.SAMPLE_RATE * cfg.AUDIO_SECONDS)


@pytest.mark.parametrize("waveform", ["sine", "triangle", "sawtooth", "square"])
def test_tonal_layer_all_waveforms(nominal_features, clean_config, waveform):
    cfg.LAYER_GLITCH_LEVEL = 0.0
    cfg.LAYER_TONAL_LEVEL  = 1.0
    cfg.LAYER_RHYTHM_LEVEL = 0.0
    cfg.TONAL_WAVEFORM = waveform
    audio = synthesize(nominal_features)
    assert audio.dtype == np.float32
    assert np.max(np.abs(audio)) > 0.0


def test_rhythm_layer_with_high_texture(nominal_features, clean_config):
    cfg.LAYER_GLITCH_LEVEL = 0.0
    cfg.LAYER_TONAL_LEVEL  = 0.0
    cfg.LAYER_RHYTHM_LEVEL = 1.0
    cfg.RHYTHM_TEXTURE_THRESH = 0.1
    features = {**nominal_features, "texture_scores": [0.9, 0.9, 0.9, 0.9]}
    audio = synthesize(features)
    assert np.max(np.abs(audio)) > 0.0


# ── Motion sensitivity ────────────────────────────────────────────────────────

def test_motion_changes_tonal_output(nominal_features, clean_config):
    cfg.LAYER_GLITCH_LEVEL = 0.0
    cfg.LAYER_TONAL_LEVEL  = 1.0
    cfg.LAYER_RHYTHM_LEVEL = 0.0
    still  = {**nominal_features, "motion_amount": 0.0, "motion_cx": 0.0}
    moving = {**nominal_features, "motion_amount": 1.0, "motion_cx": 1.0}
    audio_still  = synthesize(still)
    audio_moving = synthesize(moving)
    assert not np.allclose(audio_still, audio_moving)
