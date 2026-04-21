"""Mulchy boot loop: capture frames from a VideoSource and publish them to the web layer.

Boot setup (run once on Pi): bash scripts/install.sh
Dev machine:                 uv run mulchy --source test
"""

import logging
import signal
import sys
import time

from mulchy import config as cfg
from mulchy import web
from mulchy.camera import Camera
from mulchy.sources import make_source

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
    def __init__(self, source):
        self._running = False
        self._cam = Camera(source)
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        web.run()

    def _handle_signal(self, sig, frame):
        log.info("Signal %d received — shutting down.", sig)
        self._running = False

    def run(self):
        self._running = True
        log.info("Mulchy running. Ctrl+C to stop.")

        frame_interval = 1.0 / cfg.CAMERA_FPS
        frames = 0

        while self._running:
            t0 = time.monotonic()

            _raw, frame = self._cam.capture_blended()
            web.update(frame)

            frames += 1
            if frames % 50 == 0:
                log.info("frames=%d", frames)

            elapsed = time.monotonic() - t0
            sleep_for = frame_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

        self._shutdown()

    def _shutdown(self):
        log.info("Shutting down...")
        self._cam.close()
        log.info("Done.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Mulchy - camera to web viewer")
    parser.add_argument(
        "--source", default=None,
        help=(
            "Video source: pi | webcam | test | "
            "<path/to/video.mp4> | <path/to/image.jpg> | <path/to/dir/>"
        ),
    )
    args = parser.parse_args()

    app = Mulchy(source=make_source(args.source))
    app.run()


if __name__ == "__main__":
    main()
