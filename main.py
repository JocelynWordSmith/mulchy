"""
Mulchy - Main
Runs on boot. Captures frames, analyzes them, synthesizes audio, plays it.

Boot setup (run once on Pi):
    sudo pip install picamera2 sounddevice scipy numpy
    # Add to /etc/rc.local or create a systemd service (see install.sh)
"""

import time
import logging
import signal
import sys
import threading
import numpy as np

import config as cfg
from camera import Camera
from analyzer import analyze
from synthesizer import synthesize
import web

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/mulchy.log"),
    ],
)
log = logging.getLogger("main")


# ── Optional: future GPIO hook ────────────────────────────────────────────────
# Uncomment and flesh out when buttons are wired up.
#
# def _setup_gpio():
#     import RPi.GPIO as GPIO
#     GPIO.setmode(GPIO.BCM)
#     GPIO.setup(cfg.GPIO_BTN_FREEZE, GPIO.IN, pull_up_down=GPIO.PUD_UP)
#     GPIO.setup(cfg.GPIO_BTN_MODE,   GPIO.IN, pull_up_down=GPIO.PUD_UP)
#     GPIO.setup(cfg.GPIO_LED_ACTIVE, GPIO.OUT)
#     return GPIO
#
# def _is_frozen(GPIO) -> bool:
#     return GPIO.input(cfg.GPIO_BTN_FREEZE) == GPIO.LOW


# ── Optional: future display hook ─────────────────────────────────────────────
# def _update_display(display, features):
#     display.clear()
#     display.text(f"B:{features['brightness']:.2f} S:{features['saturation']:.2f}", 0, 0)
#     display.show()


# ── Audio playback ────────────────────────────────────────────────────────────
class AudioPlayer:
    """
    Wraps sounddevice for non-blocking streaming playback.
    Queues the next buffer while the current one plays.
    """
    def __init__(self):
        import sounddevice as sd
        self._sd = sd
        self._stream = None
        self._lock = threading.Lock()
        self._queue = []
        self._pos = 0
        self._current = None
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
        log.info("Audio stream started: %dHz, %dch",
                 cfg.SAMPLE_RATE, cfg.AUDIO_CHANNELS)

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

    def queue(self, buffer: np.ndarray):
        with self._lock:
            # Keep queue shallow (max 2 buffers) to stay responsive
            if len(self._queue) < 2:
                self._queue.append(buffer)

    def close(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()


# ── Main loop ─────────────────────────────────────────────────────────────────
class Mulchy:
    def __init__(self, preset: str = "ambient"):
        if preset:
            cfg.load_preset(preset)
            log.info("Loaded preset: %s", preset)

        self._running = False
        self._cam = Camera()
        self._player = AudioPlayer()
        self._prev_frame = None
        web.run(preset=preset)

        # Future: GPIO, display init goes here

        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, sig, frame):
        log.info("Signal %d received — shutting down.", sig)
        self._running = False

    def run(self):
        self._running = True
        log.info("Mulchy running. Ctrl+C to stop.")

        frame_interval = 1.0 / cfg.CAMERA_FPS
        loop_stats = {"frames": 0, "analyze_ms": 0.0, "synth_ms": 0.0}

        while self._running:
            t0 = time.monotonic()

            # ── Capture ───────────────────────────────────────────────────
            raw_frame, frame = self._cam.capture_blended()

            # Future: if GPIO freeze button held, skip capture and reuse frame

            # ── Analyse ───────────────────────────────────────────────────
            t1 = time.monotonic()
            features = analyze(frame, prev_frame=self._prev_frame)
            analyze_ms = (time.monotonic() - t1) * 1000
            self._prev_frame = frame

            # ── Synthesise ────────────────────────────────────────────────
            t2 = time.monotonic()
            audio = synthesize(features)
            synth_ms = (time.monotonic() - t2) * 1000

            self._player.queue(audio)
            web.update(raw_frame, frame, features, audio)

            # ── Stats every 10 frames ─────────────────────────────────────
            loop_stats["frames"] += 1
            loop_stats["analyze_ms"] += analyze_ms
            loop_stats["synth_ms"]  += synth_ms
            if loop_stats["frames"] % 10 == 0:
                n = loop_stats["frames"]
                log.info(
                    "frame=%d  analyze=%.1fms  synth=%.1fms  "
                    "bright=%.2f  sat=%.2f  edges=%.2f  motion=%.2f  cx=%.2f",
                    n,
                    loop_stats["analyze_ms"] / n,
                    loop_stats["synth_ms"] / n,
                    features["brightness"],
                    features["saturation"],
                    features["edge_density"],
                    features["motion_amount"],
                    features["motion_cx"],
                )

            # Future: update display here
            # Future: check GPIO mode-cycle button here

            # ── Pace loop ─────────────────────────────────────────────────
            elapsed = time.monotonic() - t0
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        self._shutdown()

    def _shutdown(self):
        log.info("Shutting down...")
        self._player.close()
        self._cam.close()
        log.info("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mulchy - image to audio")
    parser.add_argument(
        "--preset", default="ambient",
        choices=list(cfg.PRESETS.keys()),
        help="Load a named preset (ambient | glitchy | percussive)",
    )
    args = parser.parse_args()

    app = Mulchy(preset=args.preset)
    app.run()
