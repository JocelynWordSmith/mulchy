"""Shared pytest fixtures."""

import copy

import numpy as np
import pytest

from mulchy import config as cfg
from mulchy.analyzer import reset_motion_state
from mulchy.synthesizer import reset_synth_state


@pytest.fixture
def clean_config():
    """Snapshot and restore config module globals around each test."""
    snapshot = {
        k: copy.deepcopy(v)
        for k, v in vars(cfg).items()
        if not k.startswith("_") and not callable(v) and not isinstance(v, type)
    }
    yield
    for k, v in snapshot.items():
        setattr(cfg, k, v)


@pytest.fixture(autouse=True)
def clear_state():
    """Reset analyzer + synthesizer state before each test."""
    reset_motion_state()
    reset_synth_state()
    yield


@pytest.fixture
def black_frame() -> np.ndarray:
    return np.zeros((240, 320, 3), dtype=np.uint8)


@pytest.fixture
def white_frame() -> np.ndarray:
    return np.full((240, 320, 3), 255, dtype=np.uint8)


@pytest.fixture
def gradient_frame() -> np.ndarray:
    """Horizontal grayscale gradient — gives the analyzer something to work with."""
    f = np.zeros((240, 320, 3), dtype=np.uint8)
    ramp = np.linspace(0, 255, 320, dtype=np.uint8)
    f[..., 0] = ramp[None, :]
    f[..., 1] = ramp[None, :]
    f[..., 2] = ramp[None, :]
    return f
