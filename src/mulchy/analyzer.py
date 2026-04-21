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
_prev_gray: np.ndarray | None = None  # cached gray from previous frame
_prev_features: dict | None = None    # cached features for EMA smoothing

# Features to smooth with EMA (excludes scanlines, texture_scores, motion_cx/cy)
_SMOOTH_KEYS = ("brightness", "saturation", "edge_density",
                "luminance_mean", "luminance_variance", "motion_amount")


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

    # ── Hue clustering ────────────────────────────────────────────────────────
    hue_mask = hsv[..., 1] > 0.15   # ignore near-grey pixels
    hue_centers, hue_weights = _cluster_colors_kmeans(
        frame_float, hue_mask, cfg.TONAL_NUM_VOICES
    )

    # ── Texture repetition (FFT of each quadrant) ─────────────────────────────
    texture_scores = _texture_scores(gray)

    # ── Global stats ──────────────────────────────────────────────────────────
    brightness = float(np.mean(gray))
    saturation = float(np.mean(hsv[..., 1]))
    edge_density = _edge_density(gray)
    luminance_variance = float(np.var(gray))

    global _prev_gray
    # Use cached prev_gray when prev_frame not explicitly provided
    if prev_frame is not None:
        prev_g = _to_gray(prev_frame.astype(np.float32) / 255.0)
    else:
        prev_g = _prev_gray
    motion_amount, motion_cx, motion_cy = _motion_features(gray, h, w, prev_g)
    _prev_gray = gray

    features = ImageFeatures(
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

    return _smooth_features(features)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _smooth_features(features: ImageFeatures) -> ImageFeatures:
    """Apply EMA smoothing to selected features for frame-to-frame consistency."""
    global _prev_features
    alpha = cfg.FEATURE_SMOOTHING
    if _prev_features is None or alpha >= 1.0:
        _prev_features = dict(features)
        return features

    smoothed = dict(features)

    # Smooth scalar features with standard EMA
    for key in _SMOOTH_KEYS:
        smoothed[key] = alpha * features[key] + (1.0 - alpha) * _prev_features[key]

    # Smooth hue_weights with standard EMA
    smoothed["hue_weights"] = [
        alpha * w + (1.0 - alpha) * pw
        for w, pw in zip(features["hue_weights"], _prev_features["hue_weights"])
    ]
    # Renormalize weights
    wt = sum(smoothed["hue_weights"]) or 1.0
    smoothed["hue_weights"] = [w / wt for w in smoothed["hue_weights"]]

    # Smooth hue_centers with circular averaging (avoid 0/360 wraparound)
    smoothed_hues = []
    for h_new, h_prev in zip(features["hue_centers"], _prev_features["hue_centers"]):
        rad_new = np.radians(h_new)
        rad_prev = np.radians(h_prev)
        cx = alpha * np.cos(rad_new) + (1.0 - alpha) * np.cos(rad_prev)
        cy = alpha * np.sin(rad_new) + (1.0 - alpha) * np.sin(rad_prev)
        smoothed_hues.append(float(np.degrees(np.arctan2(cy, cx)) % 360.0))
    smoothed["hue_centers"] = smoothed_hues

    _prev_features = dict(smoothed)
    return ImageFeatures(**smoothed)


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


_MiniBatchKMeans = None  # deferred import


def _cluster_colors_kmeans(frame_float: np.ndarray, hue_mask: np.ndarray,
                           n: int):
    """
    KMeans clustering on RGB pixels for perceptually better color detection.
    Falls back to histogram method when too few saturated pixels.
    Returns (centers_01, weights) each of length n — hue centers in 0–1 range.
    """
    global _MiniBatchKMeans
    pixels = frame_float[hue_mask]

    if len(pixels) < n:
        # Too few saturated pixels — fall back to histogram
        hsv = _rgb_to_hsv(frame_float)
        hues = hsv[..., 0][hue_mask]
        return _cluster_hues(hues, n)

    # Deferred import to avoid slow sklearn load at startup
    if _MiniBatchKMeans is None:
        from sklearn.cluster import MiniBatchKMeans
        _MiniBatchKMeans = MiniBatchKMeans

    # Subsample for speed
    max_samples = cfg.COLOR_CLUSTER_SAMPLES
    rng = np.random.default_rng(42)
    if len(pixels) > max_samples:
        idx = rng.choice(len(pixels), max_samples, replace=False)
        pixels = pixels[idx]

    km = _MiniBatchKMeans(n_clusters=n, batch_size=256, n_init=1,
                          max_iter=10, random_state=42)
    km.fit(pixels)

    # Convert RGB cluster centers to HSV hue
    centers_rgb = km.cluster_centers_.astype(np.float32)
    # Reshape to (1, n, 3) for _rgb_to_hsv, then extract hues
    centers_hsv = _rgb_to_hsv(centers_rgb.reshape(1, -1, 3))
    hue_centers = centers_hsv[0, :, 0].tolist()  # 0–1 range

    # Weights = proportion of pixels per cluster
    labels = km.labels_
    counts = np.bincount(labels, minlength=n).astype(float)
    total = counts.sum() or 1.0
    weights = (counts / total).tolist()

    # Sort by weight descending
    paired = sorted(zip(weights, hue_centers), reverse=True)
    weights = [p[0] for p in paired]
    hue_centers = [p[1] for p in paired]

    return hue_centers, weights


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
        # Downsample for speed, then use rfft2 (real input → ~half the output)
        q_ds = q[::2, ::2]
        fft = np.fft.rfft2(q_ds)
        mag = np.abs(np.fft.fftshift(fft, axes=0))
        # DC component in shifted rfft2 output
        mag[mag.shape[0] // 2, 0] = 0
        # Repetition score: energy in discrete peaks vs total energy
        total = np.sum(mag) + 1e-9
        threshold = np.percentile(mag, 95)
        peak_energy = np.sum(mag[mag >= threshold])
        scores.append(float(np.clip(peak_energy / total, 0, 1)))
    return scores


def _edge_density(gray: np.ndarray) -> float:
    """Sobel edge density, normalised 0–1."""
    gray_ds = gray[::2, ::2]
    sx = ndimage.sobel(gray_ds, axis=1)
    sy = ndimage.sobel(gray_ds, axis=0)
    magnitude = np.hypot(sx, sy)
    # Scale factor reduced from 4.0 to 3.0 to compensate for
    # higher per-pixel edge magnitudes in the downsampled image.
    return float(np.clip(np.mean(magnitude) * 3.0, 0.0, 1.0))


def _motion_features(gray: np.ndarray, h: int, w: int, prev_gray) -> tuple:
    """
    Compute motion amount and spatial centre of motion vs the previous frame.
    Returns (motion_amount, motion_cx, motion_cy).
      motion_amount : 0–1, scaled by MOTION_SENSITIVITY
      motion_cx     : -1 (left-heavy motion) to +1 (right-heavy)
      motion_cy     : -1 (top-heavy)  to +1 (bottom-heavy)
    """
    if prev_gray is None:
        return 0.0, 0.0, 0.0

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
