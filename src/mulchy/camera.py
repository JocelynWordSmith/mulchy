"""
Mulchy - Camera
Thin adapter over a VideoSource: captures frames and applies EMA blending.
"""

import logging

import numpy as np

from mulchy import config as cfg
from mulchy.sources import VideoSource

log = logging.getLogger(__name__)


class Camera:
    """
    Wraps any VideoSource and applies exponential moving-average frame blending.
    Inject the source via the constructor; see sources.make_source() for factory.
    """

    def __init__(self, source: VideoSource):
        self._source = source
        self._blended: np.ndarray | None = None
        self._frame_count = 0

    def capture_blended(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Grab a new frame and blend it with the running EMA.
        Returns (raw_frame, blended_frame) — both H×W×3 uint8 RGB numpy arrays.
        """
        frame = self._source.read()

        if self._blended is None:
            self._blended = frame.astype(np.float32)
        else:
            self._blended = (
                cfg.BLEND_ALPHA * frame.astype(np.float32) +
                (1.0 - cfg.BLEND_ALPHA) * self._blended
            )

        self._frame_count += 1
        return frame.copy(), np.clip(self._blended, 0, 255).astype(np.uint8)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def close(self) -> None:
        self._source.close()
