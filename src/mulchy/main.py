"""
Mulchy - Main
Runs on boot. Captures frames, analyzes them, synthesizes audio, plays it.

Boot setup (run once on Pi):
    bash scripts/install.sh

Dev machine (no camera, no audio):
    uv run mulchy --source test --no-audio
"""

import logging
import signal
import sys
import time

from mulchy import config as cfg
from mulchy import web
from mulchy.analyzer import analyze
from mulchy.camera import Camera
from mulchy.player import AudioPlayer, NullPlayer, SoundDevicePlayer
from mulchy.sources import VideoSource, make_source
from mulchy.synthesizer import synthesize

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


# ── Main loop ─────────────────────────────────────────────────────────────────
class Mulchy:
    def __init__(
        self,
        preset: str = "ambient",
        source: VideoSource | None = None,
        player: AudioPlayer | None = None,
    ):
        if preset:
            cfg.load_preset(preset)
            log.info("Loaded preset: %s", preset)

        self._running = False
        self._cam = Camera(source if source is not None else make_source(None))
        self._player = player if player is not None else SoundDevicePlayer()
        web.run(preset=preset)

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

            # ── Analyse ───────────────────────────────────────────────────
            t1 = time.monotonic()
            features = analyze(frame)
            analyze_ms = (time.monotonic() - t1) * 1000

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
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Mulchy - image to audio")
    parser.add_argument(
        "--preset", default="ambient",
        choices=list(cfg.PRESETS.keys()),
        help="Load a named preset (ambient | glitchy | percussive | default)",
    )
    parser.add_argument(
        "--source", default=None,
        help=(
            "Video source: pi | webcam | test | "
            "<path/to/video.mp4> | <path/to/image.jpg> | <path/to/dir/>"
        ),
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Disable audio output (useful on machines without a sound device)",
    )
    args = parser.parse_args()

    source = make_source(args.source)
    player: AudioPlayer = NullPlayer() if args.no_audio else SoundDevicePlayer()

    app = Mulchy(preset=args.preset, source=source, player=player)
    app.run()


if __name__ == "__main__":
    main()
