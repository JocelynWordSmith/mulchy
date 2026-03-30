"""Unit tests for mulchy.analyzer."""

import numpy as np
import pytest

from mulchy import config as cfg
from mulchy.analyzer import analyze

REQUIRED_KEYS = {
    "scanlines", "hue_centers", "hue_weights", "texture_scores",
    "brightness", "saturation", "edge_density",
    "luminance_mean", "luminance_variance",
    "motion_amount", "motion_cx", "motion_cy",
}


# ── Structural ────────────────────────────────────────────────────────────────

def test_analyze_returns_all_keys(black_frame):
    features = analyze(black_frame)
    assert REQUIRED_KEYS.issubset(set(features.keys()))


def test_texture_scores_length_is_four(black_frame):
    f = analyze(black_frame)
    assert len(f["texture_scores"]) == 4


def test_hue_centers_and_weights_same_length(black_frame):
    f = analyze(black_frame)
    assert len(f["hue_centers"]) == len(f["hue_weights"])


def test_hue_weights_sum_to_one(red_frame):
    f = analyze(red_frame)
    assert sum(f["hue_weights"]) == pytest.approx(1.0, abs=1e-5)


def test_scanlines_length_matches_config(black_frame, clean_config):
    cfg.GLITCH_SCANLINES = 4
    f = analyze(black_frame)
    assert len(f["scanlines"]) == 4


def test_hue_centers_length_matches_tonal_num_voices(black_frame, clean_config):
    cfg.TONAL_NUM_VOICES = 2
    f = analyze(black_frame)
    assert len(f["hue_centers"]) == 2
    assert len(f["hue_weights"]) == 2


# ── Brightness ────────────────────────────────────────────────────────────────

def test_black_frame_brightness_is_zero(black_frame):
    f = analyze(black_frame)
    assert f["brightness"] == pytest.approx(0.0)


def test_white_frame_brightness_is_one(white_frame):
    f = analyze(white_frame)
    assert f["brightness"] == pytest.approx(1.0)


# ── Saturation ────────────────────────────────────────────────────────────────

def test_black_frame_saturation_is_zero(black_frame):
    f = analyze(black_frame)
    assert f["saturation"] == pytest.approx(0.0)


def test_white_frame_saturation_is_zero(white_frame):
    # Pure white has no chroma
    f = analyze(white_frame)
    assert f["saturation"] == pytest.approx(0.0)


# ── Hue ───────────────────────────────────────────────────────────────────────

def test_red_frame_dominant_hue_near_zero(red_frame):
    f = analyze(red_frame)
    dominant = f["hue_centers"][0]
    # Red hue wraps near 0° and 360°
    assert dominant < 30.0 or dominant > 330.0, (
        f"Expected red hue near 0/360, got {dominant:.1f}"
    )


# ── Motion ────────────────────────────────────────────────────────────────────

def test_no_motion_without_prev_frame(black_frame):
    f = analyze(black_frame, prev_frame=None)
    assert f["motion_amount"] == pytest.approx(0.0)
    assert f["motion_cx"] == pytest.approx(0.0)
    assert f["motion_cy"] == pytest.approx(0.0)


def test_motion_detected_between_frames(motion_frames):
    prev, curr = motion_frames
    f = analyze(curr, prev_frame=prev)
    assert f["motion_amount"] > 0.0


def test_motion_cx_negative_for_left_heavy_motion(motion_frames):
    prev, curr = motion_frames
    f = analyze(curr, prev_frame=prev)
    # Bright pixels on left → centroid should be left-of-centre (negative cx)
    assert f["motion_cx"] < 0.0


def test_identical_frames_have_zero_motion(black_frame):
    f = analyze(black_frame, prev_frame=black_frame)
    assert f["motion_amount"] == pytest.approx(0.0)


# ── Edge density ──────────────────────────────────────────────────────────────

def test_edge_density_higher_for_noisy_frame():
    rng = np.random.default_rng(42)
    noisy = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
    flat  = np.full((240, 320, 3), 128, dtype=np.uint8)
    assert analyze(noisy)["edge_density"] > analyze(flat)["edge_density"]


# ── Value bounds ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fixture_name", ["black_frame", "white_frame", "red_frame"])
def test_output_values_in_range(request, fixture_name):
    frame = request.getfixturevalue(fixture_name)
    f = analyze(frame)
    assert 0.0 <= f["brightness"]      <= 1.0
    assert 0.0 <= f["saturation"]      <= 1.0
    assert 0.0 <= f["edge_density"]    <= 1.0
    assert 0.0 <= f["motion_amount"]   <= 1.0
    assert -1.0 <= f["motion_cx"]      <= 1.0
    assert -1.0 <= f["motion_cy"]      <= 1.0
    assert 0.0 <= f["luminance_mean"]  <= 1.0
    assert f["luminance_variance"]     >= 0.0
