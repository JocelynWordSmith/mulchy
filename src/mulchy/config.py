"""Mulchy — runtime configuration.

The device has no user-facing knobs by design — it captures a frame, turns
it into sound, plays it. Everything tunable lives here so deployment-time
tweaks land in a single file."""

# ── Camera ──────────────────────────────────────────────────────────────
# 640x480 gives the squiggle analyzer enough pixels to extract a meaningful
# darkness pattern (320x240 was so coarse the resulting voices were nearly
# identical frame-to-frame). The web dashboard can still down-stream lower
# res for MJPEG if bandwidth matters.
CAMERA_WIDTH  = 640
CAMERA_HEIGHT = 480
CAMERA_FPS    = 5

# ── Audio ───────────────────────────────────────────────────────────────
# 22050 Hz is plenty for ambient drone material (most energy lives below
# 1–2 kHz) and halves the synthesizer's CPU cost vs 44.1k — important on
# the Pi where the pre-render runs in the main thread.
SAMPLE_RATE = 22050

# Synth base frequency — all six voices are ratios of this. Hue modulates
# it ±½ octave per frame.
SYNTH_BASE_HZ = 80.0
