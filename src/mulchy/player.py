"""
Mulchy - Audio Players
AudioPlayer protocol plus SoundDevicePlayer (real hardware) and NullPlayer (testing / --no-audio).
"""

import logging
import threading
from typing import Protocol

import numpy as np

from mulchy import config as cfg

log = logging.getLogger(__name__)


class AudioPlayer(Protocol):
    def queue(self, buffer: np.ndarray) -> None: ...
    def close(self) -> None: ...


class NullPlayer:
    """Discards all audio silently. Use with --no-audio or in tests."""

    def queue(self, buffer: np.ndarray) -> None:
        pass

    def close(self) -> None:
        pass


class SoundDevicePlayer:
    """
    Non-blocking streaming playback via sounddevice.
    Queues up to 2 buffers; fills seamlessly from the next buffer the moment
    the current one is exhausted so there is no zero-gap between chunks.
    """

    def __init__(self):
        import sounddevice as sd  # deferred: only needed when audio hardware is present
        self._sd = sd
        self._stream = None
        self._lock = threading.Lock()
        self._queue: list[np.ndarray] = []
        self._pos = 0
        self._current: np.ndarray | None = None
        self._start_stream()

    def _start_stream(self):
        self._stream = self._sd.OutputStream(
            samplerate=cfg.SAMPLE_RATE,
            channels=cfg.AUDIO_CHANNELS,
            dtype="float32",
            blocksize=2048,
            callback=self._callback,
        )
        self._stream.start()
        log.info("Audio stream started: %dHz, %dch", cfg.SAMPLE_RATE, cfg.AUDIO_CHANNELS)

    def _callback(self, outdata, frames, time_info, status):
        with self._lock:
            filled = 0
            while filled < frames:
                if self._current is None or self._pos >= len(self._current):
                    if self._queue:
                        self._current = self._queue.pop(0)
                        self._pos = 0
                    else:
                        outdata[filled:, 0] = 0   # silence only if queue is empty
                        return
                chunk = min(frames - filled, len(self._current) - self._pos)
                outdata[filled:filled + chunk, 0] = self._current[self._pos:self._pos + chunk]
                filled += chunk
                self._pos += chunk

    def queue(self, buffer: np.ndarray) -> None:
        with self._lock:
            # Keep queue shallow (max 2 buffers) to stay responsive
            if len(self._queue) < 2:
                self._queue.append(buffer)

    def close(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
