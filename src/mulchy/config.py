"""Mulchy configuration — camera capture + frame blending."""

CAMERA_WIDTH  = 320   # capture resolution (keep low for speed)
CAMERA_HEIGHT = 240
CAMERA_FPS    = 5     # target capture rate

# Frame blending: new_frame = BLEND_ALPHA * new + (1-BLEND_ALPHA) * prev
# Lower = smoother/slower transitions, higher = more reactive
BLEND_ALPHA = 0.35
