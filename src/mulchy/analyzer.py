"""Mulchy — image analyzer.

Single-pass image → (features, voices) extraction. There is no strategy
selection: the device only runs the squiggle drawer and the polyline-arc
sampler. This is by design — the field-recorder use case wants the device
to do exactly one thing, deterministically, with no controls to fiddle.

Output:
- ``voices``  — (6, CYCLE_SAMPLES) float32 array in [-1, 1]. Six single-cycle
                waveforms the synthesizer plays as a layered JI soundscape.
- ``features`` — small dict of 0–1 floats the synthesizer uses to modulate
                playback (hue → pitch, brightness → energy + filter, edges
                + motion → pluck rate, saturation → reverb wet).

The voices come from splitting the squiggle drawer's longest polyline into
six equal-arc-length chunks; each chunk's vertical (y) trajectory becomes
one voice. The longest polyline is whichever squiggle row had the most
darkness-modulated content."""

from __future__ import annotations

import math
import threading

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter1d

# === Drawing / sampling constants (locked-in defaults from the sandbox) ===
CYCLE_SAMPLES = 1024
VOICES = 6

# Squiggle drawer params (the v2 defaults that produced the best results).
SQUIGGLE_ROWS = 100
SQUIGGLE_FREQ = 60.0
SQUIGGLE_AMPLITUDE = 0.6
SQUIGGLE_SAMPLES_PER_CYCLE = 10
SQUIGGLE_ALTERNATE_PHASE = True

# Motion is computed against the previous frame's RGB array. Module-level
# state since the analyzer is called serially from the main loop.
_lock = threading.Lock()
_last_array: np.ndarray | None = None


def reset_motion_state() -> None:
    """Drop the cached previous frame. Useful between tests."""
    global _last_array
    with _lock:
        _last_array = None


# ── Squiggle drawer (longest-row variant) ────────────────────────────────

def _squiggle_longest_polyline(image: np.ndarray) -> np.ndarray:
    """Run the squiggle strategy on a grayscale image, return the longest
    row's (xs, ys) sample array of shape (N, 2). "Longest" here means
    "most darkness-modulated content" — the row whose summed amplitude is
    highest, since on a uniform image all rows have the same length."""
    h, w = image.shape[:2]
    gray = image.astype(np.float32) / 255.0
    darkness = 1.0 - gray  # ink-on-paper convention

    row_spacing = h / SQUIGGLE_ROWS
    half_band = row_spacing / 2.0
    n_samples = max(16, int(SQUIGGLE_SAMPLES_PER_CYCLE * SQUIGGLE_FREQ))
    xs = np.linspace(0.0, float(w), n_samples)
    xs_idx = np.clip(xs.astype(int), 0, w - 1)
    phases = 2.0 * math.pi * SQUIGGLE_FREQ * xs / w

    best_row_amp = -1.0
    best_xs: np.ndarray | None = None
    best_ys: np.ndarray | None = None

    for i in range(SQUIGGLE_ROWS):
        y_center = (i + 0.5) * row_spacing
        top = max(0, int(y_center - half_band))
        bot = min(h, int(y_center + half_band + 1))
        band = darkness[top:bot].mean(axis=0)
        d = band[xs_idx]
        amp = d * SQUIGGLE_AMPLITUDE * half_band
        amp_sum = float(amp.sum())  # crude "row darkness" measure
        phase_shift = math.pi if (SQUIGGLE_ALTERNATE_PHASE and i % 2) else 0.0
        ys = y_center + np.sin(phases + phase_shift) * amp
        if amp_sum > best_row_amp:
            best_row_amp = amp_sum
            best_xs = xs
            best_ys = ys

    if best_xs is None or best_ys is None:
        # Image had no darkness anywhere — return a zero row at image centre.
        best_xs = xs
        best_ys = np.full_like(xs, h / 2.0)
    return np.stack([best_xs, best_ys], axis=-1).astype(np.float32)


# ── Polyline arc-length sampler ──────────────────────────────────────────

def _normalize_voice(signal: np.ndarray) -> np.ndarray:
    """Condition a 1-D signal for seamless looping.

    Resample to CYCLE_SAMPLES, gentle wrap-mode Gaussian smoothing,
    linear detrend so endpoints meet, scale to [-1, 1]."""
    sig = np.asarray(signal, dtype=np.float32)
    if len(sig) < 2:
        return np.zeros(CYCLE_SAMPLES, dtype=np.float32)
    if len(sig) != CYCLE_SAMPLES:
        old_idx = np.linspace(0.0, 1.0, len(sig))
        new_idx = np.linspace(0.0, 1.0, CYCLE_SAMPLES)
        sig = np.interp(new_idx, old_idx, sig).astype(np.float32)
    sig = gaussian_filter1d(sig, sigma=2.0, mode="wrap").astype(np.float32)
    n = len(sig)
    trend = np.linspace(0.0, float(sig[-1] - sig[0]), n).astype(np.float32)
    sig = sig - trend
    lo, hi = float(sig.min()), float(sig.max())
    span = hi - lo
    if span < 1e-9:
        return np.zeros(CYCLE_SAMPLES, dtype=np.float32)
    return (2.0 * (sig - lo) / span - 1.0).astype(np.float32)


def _polyline_to_voices(polyline: np.ndarray) -> np.ndarray:
    """Split a polyline into VOICES equal-arc-length chunks, projecting each
    chunk's y onto a normalised cycle. Returns (VOICES, CYCLE_SAMPLES)."""
    voices = np.zeros((VOICES, CYCLE_SAMPLES), dtype=np.float32)
    if polyline.shape[0] < VOICES + 1:
        return voices
    diffs = np.diff(polyline, axis=0)
    seg_lens = np.linalg.norm(diffs, axis=1)
    cumlen = np.concatenate([[0.0], np.cumsum(seg_lens)]).astype(np.float32)
    total = float(cumlen[-1])
    if total < 1e-6:
        return voices
    bounds = np.linspace(0.0, total, VOICES + 1)
    for v in range(VOICES):
        a0, a1 = float(bounds[v]), float(bounds[v + 1])
        if a1 - a0 < 1e-6:
            continue
        n_query = max(64, polyline.shape[0] // VOICES)
        target_arc = np.linspace(a0, a1, n_query, dtype=np.float32)
        sample_y = np.interp(target_arc, cumlen, polyline[:, 1])
        voices[v] = _normalize_voice(sample_y)
    return voices


# ── Source-image features ────────────────────────────────────────────────

DEFAULT_FEATURES: dict[str, float] = {
    "brightness": 0.5,
    "saturation": 0.5,
    "edge_density": 0.5,
    "hue": 0.5,
    "motion": 0.0,
}


def _compute_features(rgb: np.ndarray) -> dict[str, float]:
    """Compute the small feature set the synthesizer uses. ``rgb`` is float32
    in [0, 1] of shape (H, W, 3). Updates module motion cache as a side
    effect."""
    global _last_array

    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    brightness = float(np.clip(luma.mean(), 0.0, 1.0))

    max_c = rgb.max(axis=2)
    chroma = max_c - rgb.min(axis=2)
    saturation = float(np.clip((chroma / (max_c + 1e-6)).mean(), 0.0, 1.0))

    dy = np.abs(np.diff(luma, axis=0))
    dx = np.abs(np.diff(luma, axis=1))
    edge_density = float(np.clip((dy.mean() + dx.mean()) / 2.0 * 8.0, 0.0, 1.0))

    # Saturation/value-weighted circular-mean hue.
    delta_safe = np.where(chroma > 1e-6, chroma, 1.0)
    h_raw = np.zeros_like(max_c, dtype=np.float32)
    r_dom = (max_c == r) & (chroma > 1e-6)
    g_dom = (max_c == g) & (chroma > 1e-6) & ~r_dom
    b_dom = (max_c == b) & (chroma > 1e-6) & ~r_dom & ~g_dom
    h_raw[r_dom] = ((g[r_dom] - b[r_dom]) / delta_safe[r_dom]) % 6.0
    h_raw[g_dom] = (b[g_dom] - r[g_dom]) / delta_safe[g_dom] + 2.0
    h_raw[b_dom] = (r[b_dom] - g[b_dom]) / delta_safe[b_dom] + 4.0
    hue_rad = h_raw * (np.pi / 3.0)
    weights = chroma * max_c
    mean_sin = float((np.sin(hue_rad) * weights).sum())
    mean_cos = float((np.cos(hue_rad) * weights).sum())
    total_w = float(weights.sum()) + 1e-9
    hue_mag = math.sqrt(mean_sin * mean_sin + mean_cos * mean_cos) / total_w
    if hue_mag < 0.04:
        hue = 0.5
    else:
        hue = float((math.atan2(mean_sin, mean_cos) / (2.0 * math.pi)) % 1.0)

    with _lock:
        motion = 0.0
        if _last_array is not None and _last_array.shape == rgb.shape:
            motion = float(np.clip(np.abs(rgb - _last_array).mean() * 6.0, 0.0, 1.0))
        _last_array = rgb
    return {
        "brightness": brightness,
        "saturation": saturation,
        "edge_density": edge_density,
        "hue": hue,
        "motion": motion,
    }


# ── Public entry point ───────────────────────────────────────────────────

def analyze(frame: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    """Run the full image-analysis pipeline.

    ``frame`` is an H×W×3 uint8 RGB numpy array fresh from the camera.
    Returns ``(voices, features)`` where voices is (6, 1024) float32 in
    [-1, 1] and features is a dict of 0–1 floats."""
    if frame.size == 0 or frame.ndim != 3 or frame.shape[2] != 3:
        return (
            np.zeros((VOICES, CYCLE_SAMPLES), dtype=np.float32),
            dict(DEFAULT_FEATURES),
        )
    rgb_f = frame.astype(np.float32) / 255.0
    features = _compute_features(rgb_f)
    # Run squiggle on a luminance image for speed.
    luma = (0.299 * frame[..., 0] + 0.587 * frame[..., 1] + 0.114 * frame[..., 2]).astype(np.uint8)
    polyline = _squiggle_longest_polyline(luma)
    voices = _polyline_to_voices(polyline)
    return voices, features


# Backwards-compatible thin wrapper used by older callers.
def analyze_features_only(frame: np.ndarray) -> dict[str, float]:
    _, features = analyze(frame)
    return features


__all__ = [
    "VOICES",
    "CYCLE_SAMPLES",
    "DEFAULT_FEATURES",
    "analyze",
    "analyze_features_only",
    "reset_motion_state",
]


# Convenience for use via PIL.Image (e.g. file-based sources).
def analyze_image(image: Image.Image) -> tuple[np.ndarray, dict[str, float]]:
    return analyze(np.asarray(image.convert("RGB"), dtype=np.uint8))
