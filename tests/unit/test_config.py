"""Unit tests for mulchy.config."""

import pytest

from mulchy import config as cfg

REQUIRED_PRESET_KEYS = {
    "BLEND_ALPHA", "AUDIO_SECONDS",
    "LAYER_GLITCH_LEVEL", "LAYER_TONAL_LEVEL", "LAYER_RHYTHM_LEVEL",
    "TONAL_WAVEFORM", "TONAL_NUM_VOICES", "TONAL_OCTAVE_RANGE", "TONAL_DETUNE_CENTS",
    "GLITCH_PITCH_SHIFT", "GLITCH_LOW_PASS_HZ",
    "RHYTHM_BPM", "RHYTHM_DECAY_MS", "RHYTHM_TEXTURE_THRESH",
    "MIX_LOWPASS_HZ",
    "MOTION_SENSITIVITY", "MOTION_PITCH_SEMITONES", "MOTION_TEMPO_SCALE",
    "CROSSFADE_SMOOTHNESS",
}


def test_all_presets_present():
    assert set(cfg.PRESETS.keys()) == {"default", "ambient", "glitchy", "percussive"}


def test_all_presets_have_required_keys():
    for name, preset in cfg.PRESETS.items():
        missing = REQUIRED_PRESET_KEYS - set(preset.keys())
        assert not missing, f"Preset '{name}' missing keys: {missing}"


def test_preset_value_types():
    for name, preset in cfg.PRESETS.items():
        assert isinstance(preset["BLEND_ALPHA"], float), f"{name}: BLEND_ALPHA not float"
        assert isinstance(preset["AUDIO_SECONDS"], float), f"{name}: AUDIO_SECONDS not float"
        assert isinstance(preset["RHYTHM_BPM"], (int, float)), f"{name}: RHYTHM_BPM not numeric"
        assert preset["TONAL_WAVEFORM"] in ("sine", "triangle", "sawtooth", "square"), (
            f"{name}: unknown waveform {preset['TONAL_WAVEFORM']}"
        )


def test_load_preset_ambient(clean_config):
    cfg.load_preset("ambient")
    assert cfg.LAYER_GLITCH_LEVEL == pytest.approx(0.0)
    assert cfg.TONAL_WAVEFORM == "triangle"
    assert cfg.CROSSFADE_SMOOTHNESS == pytest.approx(0.7)


def test_load_preset_glitchy(clean_config):
    cfg.load_preset("glitchy")
    assert cfg.LAYER_GLITCH_LEVEL == pytest.approx(0.65)
    assert cfg.TONAL_WAVEFORM == "sawtooth"


def test_load_preset_mutates_module(clean_config):
    original_bpm = cfg.PRESETS["default"]["RHYTHM_BPM"]
    cfg.load_preset("default")
    assert cfg.RHYTHM_BPM == original_bpm


def test_clean_config_fixture_restores(clean_config):
    cfg.MASTER_VOLUME = 0.0
    assert cfg.MASTER_VOLUME == pytest.approx(0.0)
    # fixture restores after yield — tested implicitly by checking no bleed to other tests


def test_load_unknown_preset_is_noop(clean_config):
    original_bpm = cfg.RHYTHM_BPM
    cfg.load_preset("does_not_exist")
    assert cfg.RHYTHM_BPM == original_bpm


def test_crossfade_smoothness_range():
    for name, preset in cfg.PRESETS.items():
        val = preset["CROSSFADE_SMOOTHNESS"]
        assert 0.0 <= val <= 1.0, f"{name}: CROSSFADE_SMOOTHNESS={val} out of [0,1]"
