"""Mulchy — main boot loop.

Captures raw frames from the camera, hands them to the squiggle analyzer to
extract voice cycles + features, and feeds them to the synthesizer. The
synth owns its own continuous audio output via sounddevice — there's no
chunked buffer queueing in the main loop. New frames just append source
layers to the synth's per-voice queues; the audio thread keeps streaming.

Boot setup (Pi):
    bash scripts/install.sh

Local dev (no audio device):
    uv run mulchy --source test --no-audio
"""

import logging
import signal
import sys
import time

from mulchy import config as cfg
from mulchy import web
from mulchy.analyzer import analyze, get_filter_mode, set_filter_mode
from mulchy.sources import VideoSource, make_source
from mulchy.synthesizer import Synthesizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/mulchy.log"),
    ],
)
log = logging.getLogger("main")


class Mulchy:
    def __init__(
        self,
        source: VideoSource | None = None,
        audio_enabled: bool = True,
    ):
        self._running = False
        self._source = source if source is not None else make_source(None)
        self._synth = Synthesizer(
            sample_rate=cfg.SAMPLE_RATE,
            base_freq=cfg.SYNTH_BASE_HZ,
            audio_enabled=audio_enabled,
        )
        web.run()

        # Wire runtime control so the dashboard can flip the audio filter.
        web.register_controls(
            get_filter=get_filter_mode,
            set_filter=set_filter_mode,
        )

        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, sig, _frame):
        log.info("Signal %d received — shutting down.", sig)
        self._running = False

    def run(self) -> None:
        self._running = True
        log.info("Mulchy running. Ctrl+C to stop.")

        frame_interval = 1.0 / cfg.CAMERA_FPS
        stats = {"frames": 0, "analyze_ms": 0.0, "update_ms": 0.0}

        while self._running:
            t0 = time.monotonic()

            frame = self._source.read()

            t1 = time.monotonic()
            voices, features = analyze(frame)
            analyze_ms = (time.monotonic() - t1) * 1000

            t2 = time.monotonic()
            self._synth.update(voices, features)
            update_ms = (time.monotonic() - t2) * 1000

            web.update(frame, features)

            stats["frames"] += 1
            stats["analyze_ms"] += analyze_ms
            stats["update_ms"]  += update_ms
            if stats["frames"] % 10 == 0:
                n = stats["frames"]
                log.info(
                    "frame=%d  analyze=%.1fms  update=%.2fms  "
                    "bright=%.2f  sat=%.2f  edges=%.2f  hue=%.2f  motion=%.2f",
                    n,
                    stats["analyze_ms"] / n,
                    stats["update_ms"]  / n,
                    features["brightness"],
                    features["saturation"],
                    features["edge_density"],
                    features["hue"],
                    features["motion"],
                )

            elapsed = time.monotonic() - t0
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        self._shutdown()

    def _shutdown(self) -> None:
        log.info("Shutting down...")
        self._synth.close()
        self._source.close()
        log.info("Done.")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Mulchy — image to soundscape")
    parser.add_argument(
        "--source", default=None,
        help=(
            "Video source: pi | webcam | test | "
            "<path/to/video.mp4> | <path/to/image.jpg> | <path/to/dir/>"
        ),
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Disable audio output (useful on machines without a sound device).",
    )
    args = parser.parse_args()

    source = make_source(args.source)
    app = Mulchy(source=source, audio_enabled=not args.no_audio)
    app.run()


if __name__ == "__main__":
    main()
