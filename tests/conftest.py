"""
Shared pytest fixtures for all test suites.
"""

import copy
import pathlib

import numpy as np
import pytest

from mulchy import config as cfg

# ── Config management ─────────────────────────────────────────────────────────

@pytest.fixture
def clean_config():
    """Snapshot and restore all config module globals around each test."""
    snapshot = {
        k: copy.deepcopy(v)
        for k, v in vars(cfg).items()
        if not k.startswith("_") and not callable(v) and not isinstance(v, type)
    }
    yield
    for k, v in snapshot.items():
        setattr(cfg, k, v)


@pytest.fixture(autouse=True)
def clear_analyzer_caches():
    """Clear analyzer module-level caches before each test to avoid cross-test bleed."""
    import mulchy.analyzer as a
    a._row_idx_cache.clear()
    a._coord_cache.clear()
    a._prev_gray = None
    a._prev_features = None
    yield


@pytest.fixture(autouse=True)
def clear_synth_state():
    """Clear synthesizer stateful data before each test."""
    from mulchy.synthesizer import reset_synth_state
    reset_synth_state()
    yield


# ── Synthetic frames ──────────────────────────────────────────────────────────

@pytest.fixture
def black_frame() -> np.ndarray:
    return np.zeros((240, 320, 3), dtype=np.uint8)


@pytest.fixture
def white_frame() -> np.ndarray:
    return np.full((240, 320, 3), 255, dtype=np.uint8)


@pytest.fixture
def red_frame() -> np.ndarray:
    f = np.zeros((240, 320, 3), dtype=np.uint8)
    f[..., 0] = 255
    return f


@pytest.fixture
def motion_frames() -> tuple:
    """Two frames with a clear left-side motion."""
    prev = np.zeros((240, 320, 3), dtype=np.uint8)
    curr = np.zeros((240, 320, 3), dtype=np.uint8)
    curr[:, :160, :] = 200  # bright on the left half
    return prev, curr


# ── Web state helpers ─────────────────────────────────────────────────────────

@pytest.fixture
def tmp_state_file(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """Redirect web._STATE_FILE to a temp path so tests don't touch disk state."""
    import mulchy.web as web
    monkeypatch.setattr(web, "_STATE_FILE", tmp_path / "state.json")
    return tmp_path / "state.json"
