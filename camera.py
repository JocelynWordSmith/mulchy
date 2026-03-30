"""
Mulchy - Camera
Wraps picamera2, handles frame blending between captures.
Falls back to a test pattern if no camera is found (useful for dev on desktop).
"""

import numpy as np
import logging
import config as cfg

log = logging.getLogger(__name__)


class Camera:
    def __init__(self):
        self._cam = None
        self._blended: np.ndarray = None
        self._use_test_pattern = False
        self._frame_count = 0
        self._init_camera()

    def _init_camera(self):
        try:
            from picamera2 import Picamera2
            self._cam = Picamera2()
            config = self._cam.create_still_configuration(
                main={"size": (cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT), "format": "RGB888"},
                controls={"FrameRate": cfg.CAMERA_FPS},
            )
            self._cam.configure(config)
            self._cam.start()
            log.info("Camera started: %dx%d @ %dfps",
                     cfg.CAMERA_WIDTH, cfg.CAMERA_HEIGHT, cfg.CAMERA_FPS)
        except Exception as e:
            log.warning("picamera2 not available (%s) — using test pattern.", e)
            self._use_test_pattern = True

    def capture_blended(self):
        """
        Grab a new frame and blend with previous.
        Returns (raw_frame, blended_frame) — both H×W×3 uint8 numpy arrays (RGB).
        """
        frame = self._get_frame()

        if self._blended is None:
            self._blended = frame.astype(np.float32)
        else:
            # Exponential moving average blend
            self._blended = (
                cfg.BLEND_ALPHA * frame.astype(np.float32) +
                (1.0 - cfg.BLEND_ALPHA) * self._blended
            )

        self._frame_count += 1
        return frame.copy(), np.clip(self._blended, 0, 255).astype(np.uint8)

    def _get_frame(self) -> np.ndarray:
        if self._use_test_pattern:
            return self._test_pattern()
        try:
            frame = self._cam.capture_array("main")
            return frame[..., ::-1]  # BGR→RGB: picamera2 "RGB888" delivers BGR byte order
        except Exception as e:
            log.error("Capture failed: %s — using test pattern frame.", e)
            return self._test_pattern()

    def _test_pattern(self) -> np.ndarray:
        """
        Animated test pattern: rotating hue gradient + moving texture.
        Useful for development without a physical camera.
        """
        h, w = cfg.CAMERA_HEIGHT, cfg.CAMERA_WIDTH
        t = self._frame_count * 0.1

        # Hue gradient
        x = np.linspace(0, 1, w)
        y = np.linspace(0, 1, h)
        xx, yy = np.meshgrid(x, y)

        hue = (xx + np.sin(yy * 6 + t) * 0.2 + t * 0.05) % 1.0
        sat = 0.7 + 0.3 * np.sin(yy * 4 - t)
        val = 0.5 + 0.5 * np.sin(xx * 8 + t * 0.7)

        # HSV → RGB
        rgb = _hsv_to_rgb_image(hue, sat, val)
        return (rgb * 255).astype(np.uint8)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def close(self):
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass


def _hsv_to_rgb_image(h, s, v) -> np.ndarray:
    """Vectorised HSV→RGB for numpy arrays."""
    i = (h * 6.0).astype(int) % 6
    f = h * 6.0 - np.floor(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))

    r = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [v,q,p,p,t,v])
    g = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [t,v,v,q,p,p])
    b = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [p,p,t,v,v,q])

    return np.stack([r, g, b], axis=-1)
