"""
Mulchy - Image Analyzer
Converts a numpy image array into a feature dict that the synthesizer understands.
No audio here — pure image analysis.
"""

from typing import TypedDict

import numpy as np
from scipy import ndimage

from mulchy import config as cfg


class ImageFeatures(TypedDict):
    """
    Everything the synthesizer needs, derived from one blended frame.
    All values are normalized 0.0–1.0 unless noted.
    """
    # Raw scanline data: list of 1-D float arrays (one per sampled row), values 0–1
    scanlines: list

    # Hue cluster centers (0–360) and their relative weights (sum to 1.0)
    hue_centers: list   # list of floats, len = TONAL_NUM_VOICES
    hue_weights: list   # list of floats, same len

    # Texture repetition score per image quadrant (0 = smooth, 1 = highly repetitive)
    texture_scores: list  # list of 4 floats (TL, TR, BL, BR)

    # Overall brightness, saturation, edge density
    brightness: float
    saturation: float
    edge_density: float

    # DC offset / "heaviness" of the image (mean luminance)
    luminance_mean: float
    luminance_variance: float

    # Motion between this frame and the previous one
    motion_amount: float  # 0 = still, 1 = maximum change
    motion_cx: float      # weighted centroid of motion, -1 (left) to +1 (right)
    motion_cy: float      # weighted centroid of motion, -1 (top) to +1 (bottom)


_row_idx_cache: dict = {}   # h → row_indices array
_coord_cache: dict   = {}   # (h, w) → (xs, ys) for motion centroid


def analyze(frame_rgb: np.ndarray, prev_frame: np.ndarray = None) -> ImageFeatures:
    """
    frame_rgb: H×W×3 uint8 numpy array (RGB)
    prev_frame: previous frame (same shape) for motion detection, or None
    Returns an ImageFeatures dict.
    """
    h, w = frame_rgb.shape[:2]

    frame_float = frame_rgb.astype(np.float32) / 255.0
    gray = _to_gray(frame_float)
    hsv  = _rgb_to_hsv(frame_float)

    # ── Scanlines ─────────────────────────────────────────────────────────────
    if h not in _row_idx_cache:
        _row_idx_cache[h] = np.linspace(0, h - 1, cfg.GLITCH_SCANLINES, dtype=int)
    scanlines = [gray[_row_idx_cache[h][i], :].tolist() for i in range(cfg.GLITCH_SCANLINES)]

    # ── Hue clustering (simple histogram → top-N peaks) ───────────────────────
    hue_mask = hsv[..., 1] > 0.15   # ignore near-grey pixels
    hues = hsv[..., 0][hue_mask]    # 0–1 range (we'll work in 0–360 later)

    hue_centers, hue_weights = _cluster_hues(hues, cfg.TONAL_NUM_VOICES)

    # ── Texture repetition (FFT of each quadrant) ─────────────────────────────
    texture_scores = _texture_scores(gray)

    # ── Global stats ──────────────────────────────────────────────────────────
    brightness = float(np.mean(gray))
    saturation = float(np.mean(hsv[..., 1]))
    edge_density = _edge_density(gray)
    luminance_variance = float(np.var(gray))

    motion_amount, motion_cx, motion_cy = _motion_features(gray, h, w, prev_frame)

    return ImageFeatures(
        scanlines=scanlines,
        hue_centers=[c * 360.0 for c in hue_centers],  # convert 0–1 → 0–360°
        hue_weights=hue_weights,
        texture_scores=texture_scores,
        brightness=brightness,
        saturation=saturation,
        edge_density=edge_density,
        luminance_mean=brightness,
        luminance_variance=luminance_variance,
        motion_amount=motion_amount,
        motion_cx=motion_cx,
        motion_cy=motion_cy,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_gray(frame_float: np.ndarray) -> np.ndarray:
    """BT.601 luma from float32 RGB (H×W×3, values 0–1)."""
    return (0.2126 * frame_float[..., 0] +
            0.7152 * frame_float[..., 1] +
            0.0722 * frame_float[..., 2])


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorised RGB→HSV. rgb shape: H×W×3, values 0–1. Returns H×W×3."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.max(rgb, axis=-1)
    minc = np.min(rgb, axis=-1)
    v = maxc
    s = np.where(maxc != 0, (maxc - minc) / maxc, 0.0)

    delta = maxc - minc
    h = np.zeros_like(maxc)
    mask = delta != 0
    # hue calculation per channel
    mr = mask & (maxc == r)
    mg = mask & (maxc == g)
    mb = mask & (maxc == b)
    h[mr] = ((g[mr] - b[mr]) / delta[mr]) % 6
    h[mg] = (b[mg] - r[mg]) / delta[mg] + 2
    h[mb] = (r[mb] - g[mb]) / delta[mb] + 4
    h = h / 6.0  # normalise to 0–1

    return np.stack([h, s, v], axis=-1)


def _cluster_hues(hues_01: np.ndarray, n: int):
    """
    Simple histogram-based hue clustering.
    Returns (centers_01, weights) each of length n.
    """
    if len(hues_01) == 0:
        # No saturated pixels — return evenly spaced neutral centres
        centers = np.linspace(0, 1, n, endpoint=False).tolist()
        weights = [1.0 / n] * n
        return centers, weights

    bins = 72  # 5° buckets
    hist, edges = np.histogram(hues_01, bins=bins, range=(0, 1))
    hist = hist.astype(np.float32)

    centers = []
    weights = []
    hist_work = hist.copy()

    for _ in range(n):
        peak = int(np.argmax(hist_work))
        center = float((edges[peak] + edges[peak + 1]) / 2.0)
        weight = float(hist_work[peak])
        centers.append(center)
        weights.append(weight)

        # suppress neighbourhood (±15° = ±3 bins) so next peak is different
        lo = max(0, peak - 3)
        hi = min(bins, peak + 4)
        hist_work[lo:hi] = 0

    total = sum(weights) or 1.0
    weights = [w / total for w in weights]
    return centers, weights


def _texture_scores(gray: np.ndarray) -> list:
    """
    Measure repetitiveness in each quadrant using 2-D FFT.
    Score 0 = smooth/flat, 1 = highly repetitive pattern.
    """
    h, w = gray.shape
    mh, mw = h // 2, w // 2
    quadrants = [
        gray[:mh, :mw],
        gray[:mh, mw:],
        gray[mh:, :mw],
        gray[mh:, mw:],
    ]
    scores = []
    for q in quadrants:
        if q.size == 0:
            scores.append(0.0)
            continue
        fft = np.fft.fft2(q)
        mag = np.abs(np.fft.fftshift(fft))
        # Exclude DC component (centre)
        cy, cx = mag.shape[0] // 2, mag.shape[1] // 2
        mag[cy, cx] = 0
        # Repetition score: energy in discrete peaks vs total energy
        total = np.sum(mag) + 1e-9
        threshold = np.percentile(mag, 95)
        peak_energy = np.sum(mag[mag >= threshold])
        scores.append(float(np.clip(peak_energy / total, 0, 1)))
    return scores


def _edge_density(gray: np.ndarray) -> float:
    """Sobel edge density, normalised 0–1."""
    sx = ndimage.sobel(gray, axis=1)
    sy = ndimage.sobel(gray, axis=0)
    magnitude = np.hypot(sx, sy)
    return float(np.clip(np.mean(magnitude) * 4.0, 0.0, 1.0))


def _motion_features(gray: np.ndarray, h: int, w: int, prev_frame) -> tuple:
    """
    Compute motion amount and spatial centre of motion vs the previous frame.
    Returns (motion_amount, motion_cx, motion_cy).
      motion_amount : 0–1, scaled by MOTION_SENSITIVITY
      motion_cx     : -1 (left-heavy motion) to +1 (right-heavy)
      motion_cy     : -1 (top-heavy)  to +1 (bottom-heavy)
    """
    if prev_frame is None:
        return 0.0, 0.0, 0.0

    prev_gray = _to_gray(prev_frame.astype(np.float32) / 255.0)
    diff = np.abs(gray - prev_gray)

    raw    = float(np.mean(diff))
    amount = float(np.clip(raw * cfg.MOTION_SENSITIVITY * 8.0, 0.0, 1.0))

    # Weighted centroid gives direction of motion; coordinate arrays are cached
    if (h, w) not in _coord_cache:
        _coord_cache[(h, w)] = (np.linspace(0.0, 1.0, w), np.linspace(0.0, 1.0, h))
    xs, ys = _coord_cache[(h, w)]

    total = float(np.sum(diff)) + 1e-9
    cx = float(np.sum(diff * xs[np.newaxis, :]) / total) * 2.0 - 1.0
    cy = float(np.sum(diff * ys[:, np.newaxis]) / total) * 2.0 - 1.0

    return amount, cx, cy
