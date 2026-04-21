"""
Mulchy - Audio Players
AudioPlayer protocol plus SoundDevicePlayer (real hardware) and NullPlayer (testing / --no-audio).
"""

import logging
import re
import subprocess
import threading
from typing import Protocol

import numpy as np

from mulchy import config as cfg

log = logging.getLogger(__name__)


def _wpctl_env() -> dict:
    """Build environment for wpctl — ensure XDG_RUNTIME_DIR is set for PipeWire access."""
    import os
    env = os.environ.copy()
    if "XDG_RUNTIME_DIR" not in env:
        # Default to uid 1000 (pi user) if not set
        uid = os.getuid()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    return env


def list_output_devices() -> list[dict]:
    """Return PipeWire audio sinks via wpctl. Falls back to sounddevice if wpctl unavailable."""
    try:
        r = subprocess.run(
            ["wpctl", "status"], capture_output=True, text=True, timeout=5,
            env=_wpctl_env(),
        )
        if r.returncode == 0:
            return _parse_wpctl_sinks(r.stdout)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("wpctl failed: %s", e)
    # Fallback: sounddevice device list
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        default_out = sd.default.device[1]
        return [
            {"id": i, "name": d["name"], "is_default": i == default_out}
            for i, d in enumerate(devices)
            if d["max_output_channels"] > 0
        ]
    except Exception as e:
        log.error("Failed to list audio devices: %s", e)
        return []


def _parse_wpctl_sinks(output: str) -> list[dict]:
    """Parse the Audio > Sinks section from wpctl status output."""
    sinks = []
    in_audio = False
    in_sinks = False
    for line in output.splitlines():
        # Strip box-drawing characters (│ ├ └ ─) and whitespace
        clean = line.replace("│", " ").replace("├", " ").replace("└", " ").replace("─", " ").strip()
        if clean == "Audio":
            in_audio = True
            continue
        if clean == "Video":
            in_audio = False
            in_sinks = False
            continue
        if not in_audio:
            continue
        if clean == "Sinks:":
            in_sinks = True
            continue
        if in_sinks:
            # End of sinks: next section header (Sources:, Filters:, etc.) or empty
            if not clean or (clean.endswith(":") and not any(c.isdigit() for c in clean)):
                in_sinks = False
                continue
            # Parse: " *  89. XLeader A8  [vol: 0.40]" or "56. Built-in Audio Stereo  [vol: 1.00]"
            m = re.match(r"(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[.*\])?\s*$", clean)
            if m:
                sinks.append({
                    "id": int(m.group(2)),
                    "name": m.group(3).strip(),
                    "is_default": m.group(1) == "*",
                })
    return sinks


def set_default_sink(sink_id: int) -> dict:
    """Set the PipeWire default audio sink via wpctl. Returns {ok, device, error?}."""
    try:
        r = subprocess.run(
            ["wpctl", "set-default", str(sink_id)],
            capture_output=True, text=True, timeout=5,
            env=_wpctl_env(),
        )
        if r.returncode == 0:
            log.info("PipeWire default sink set to %d", sink_id)
            return {"ok": True, "device": sink_id}
        err = r.stderr.strip() or f"wpctl exit code {r.returncode}"
        log.error("wpctl set-default failed: %s", err)
        return {"ok": False, "device": sink_id, "error": err}
    except FileNotFoundError:
        return {"ok": False, "device": sink_id, "error": "wpctl not found"}
    except Exception as e:
        return {"ok": False, "device": sink_id, "error": str(e)}


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

    Audio routing is handled by PipeWire (via pipewire-alsa). Use wpctl
    set-default to switch sinks — this player always opens the system default.
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
        try:
            self._stream = self._sd.OutputStream(
                samplerate=cfg.SAMPLE_RATE,
                channels=cfg.AUDIO_CHANNELS,
                dtype="float32",
                blocksize=2048,
                callback=self._callback,
            )
            self._stream.start()
            log.info("Audio stream started: %dHz, %dch", cfg.SAMPLE_RATE, cfg.AUDIO_CHANNELS)
        except Exception as e:
            log.error("Failed to open audio stream: %s", e)

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
            # Queue depth of 1 keeps setting changes audible within ~one chunk.
            # Any deeper and recent tweaks sit behind stale audio.
            if len(self._queue) < 1:
                self._queue.append(buffer)

    def close(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
