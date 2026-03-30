"""
Mulchy - Video Sources
VideoSource protocol and all implementations.
Select a source via make_source(spec).
"""

import logging
import os
from typing import Protocol, runtime_checkable

import numpy as np

from mulchy import config as cfg

log = logging.getLogger(__name__)


@runtime_checkable
class VideoSource(Protocol):
    """
    Minimal interface for anything that produces video frames.
    read() returns H×W×3 uint8 RGB numpy arrays.
    """

    def read(self) -> np.ndarray:
        """Return the next frame as H×W×3 uint8 RGB."""
        ...

    def close(self) -> None:
        """Release any resources held by this source."""
        ...


# ── Test Pattern ──────────────────────────────────────────────────────────────

class TestPatternSource:
    """
    Animated test pattern: rotating hue gradient + moving texture.
    No external dependencies. Used as the default fallback on dev machines.
    """

    def __init__(self):
        self._frame_count = 0

    def read(self) -> np.ndarray:
        h, w = cfg.CAMERA_HEIGHT, cfg.CAMERA_WIDTH
        t = self._frame_count * 0.1
        self._frame_count += 1

        x = np.linspace(0, 1, w)
        y = np.linspace(0, 1, h)
        xx, yy = np.meshgrid(x, y)

        hue = (xx + np.sin(yy * 6 + t) * 0.2 + t * 0.05) % 1.0
        sat = 0.7 + 0.3 * np.sin(yy * 4 - t)
        val = 0.5 + 0.5 * np.sin(xx * 8 + t * 0.7)

        rgb = _hsv_to_rgb_image(hue, sat, val)
        return (rgb * 255).astype(np.uint8)

    def close(self) -> None:
        pass


# ── Pi Camera ─────────────────────────────────────────────────────────────────

class PiCameraSource:
    """Uses picamera2 — Raspberry Pi only."""

    def __init__(self):
        from picamera2 import Picamera2  # deferred: only available on Pi
        self._cam = Picamera2()
        config = self._cam.create_still_configuration(
            main={"size": (cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT), "format": "RGB888"},
            controls={"FrameRate": cfg.CAMERA_FPS},
        )
        self._cam.configure(config)
        self._cam.start()
        log.info("PiCamera started: %dx%d @ %dfps",
                 cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT, cfg.CAMERA_FPS)

    def read(self) -> np.ndarray:
        frame = self._cam.capture_array("main")
        return frame[..., ::-1]  # BGR→RGB: picamera2 "RGB888" delivers BGR byte order

    def close(self) -> None:
        try:
            self._cam.stop()
            self._cam.close()
        except Exception:
            pass


# ── Webcam ────────────────────────────────────────────────────────────────────

class WebcamSource:
    """OpenCV webcam capture. Requires opencv-python-headless."""

    def __init__(self, device: int = 0):
        try:
            import cv2
            self._cv2 = cv2
        except ImportError as e:
            raise ImportError(
                "WebcamSource requires opencv-python-headless: uv sync --extra webcam"
            ) from e

        self._cap = cv2.VideoCapture(device)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open webcam device {device}")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.CAMERA_WIDTH)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.CAMERA_HEIGHT)
        log.info("Webcam opened: device %d", device)

    def read(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok:
            raise RuntimeError("Webcam read failed")
        frame_rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        return self._cv2.resize(frame_rgb, (cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT))

    def close(self) -> None:
        self._cap.release()


# ── Video File ────────────────────────────────────────────────────────────────

class VideoFileSource:
    """OpenCV video file playback, loops at EOF. Requires opencv-python-headless."""

    def __init__(self, path: str):
        try:
            import cv2
            self._cv2 = cv2
        except ImportError as e:
            raise ImportError(
                "VideoFileSource requires opencv-python-headless: uv sync --extra webcam"
            ) from e

        self._path = path
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise RuntimeError(f"Could not open video file: {path}")
        log.info("VideoFile opened: %s", path)

    def read(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok:
            # Loop: rewind to start
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        if not ok:
            raise RuntimeError(f"Could not read from video file: {self._path}")
        frame_rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        return self._cv2.resize(frame_rgb, (cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT))

    def close(self) -> None:
        self._cap.release()


# ── Still Image ───────────────────────────────────────────────────────────────

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}


class StillImageSource:
    """
    Serves one or more still images as a video stream.
    Accepts a single image path or a directory.
    Each image is held for cfg.CAMERA_FPS * 3 reads (~3 seconds) before advancing.
    Uses PIL — no cv2 dependency.
    """

    def __init__(self, path: str):
        from PIL import Image
        self._Image = Image

        if os.path.isdir(path):
            files = sorted(
                p for p in (os.path.join(path, f) for f in os.listdir(path))
                if os.path.splitext(p)[1].lower() in _IMAGE_EXTS
            )
            if not files:
                raise ValueError(f"No image files found in directory: {path}")
            self._paths = files
        else:
            self._paths = [path]

        self._idx = 0
        self._read_count = 0
        self._dwell = max(1, cfg.CAMERA_FPS * 3)
        self._current: np.ndarray = self._load(self._paths[0])
        log.info("StillImage source: %d image(s), dwell=%d reads", len(self._paths), self._dwell)

    def _load(self, path: str) -> np.ndarray:
        img = self._Image.open(path).convert("RGB")
        img = img.resize((cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT), self._Image.LANCZOS)
        return np.array(img, dtype=np.uint8)

    def read(self) -> np.ndarray:
        self._read_count += 1
        if len(self._paths) > 1 and self._read_count % self._dwell == 0:
            self._idx = (self._idx + 1) % len(self._paths)
            self._current = self._load(self._paths[self._idx])
        return self._current.copy()

    def close(self) -> None:
        pass


# ── Factory ───────────────────────────────────────────────────────────────────

def make_source(spec: str | None) -> VideoSource:
    """
    Build and return a VideoSource from a spec string.

    spec=None        → auto-detect: PiCameraSource if available, else TestPatternSource
    spec="pi"        → PiCameraSource
    spec="webcam"    → WebcamSource(0)
    spec="test"      → TestPatternSource
    spec=<path>      → VideoFileSource for video extensions, StillImageSource otherwise
    """
    if spec is None:
        try:
            return PiCameraSource()
        except Exception as e:
            log.warning("PiCamera unavailable (%s) — using test pattern.", e)
            return TestPatternSource()

    if spec == "pi":
        return PiCameraSource()

    if spec == "webcam":
        return WebcamSource(0)

    if spec == "test":
        return TestPatternSource()

    # Path-based: distinguish video files from images/directories
    ext = os.path.splitext(spec)[1].lower()
    if ext in _VIDEO_EXTS:
        return VideoFileSource(spec)

    return StillImageSource(spec)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hsv_to_rgb_image(h, s, v) -> np.ndarray:
    """Vectorised HSV→RGB for numpy arrays (used by TestPatternSource)."""
    i = (h * 6.0).astype(int) % 6
    f = h * 6.0 - np.floor(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))

    r = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [v,q,p,p,t,v])
    g = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [t,v,v,q,p,p])
    b = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [p,p,t,v,v,q])

    return np.stack([r, g, b], axis=-1)
