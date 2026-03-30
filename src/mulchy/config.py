"""
Mulchy - Configuration
All tunables live here. No magic numbers elsewhere.
"""

# ─── Camera ───────────────────────────────────────────────────────────────────
CAMERA_WIDTH  = 320   # capture resolution (keep low for speed)
CAMERA_HEIGHT = 240
CAMERA_FPS    = 5     # target capture rate

# Frame blending: new_frame = BLEND_ALPHA * new + (1-BLEND_ALPHA) * prev
# Lower = smoother/slower transitions, higher = more reactive
BLEND_ALPHA = 0.35

# ─── Audio ────────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 44100
AUDIO_SECONDS = 2.0        # duration of each generated audio chunk
AUDIO_CHANNELS = 1         # mono for now; set to 2 for stereo later
MASTER_VOLUME = 0.7        # 0.0–1.0
CROSSFADE_SMOOTHNESS = 0.0 # 0.0 = 40ms taper dip at boundaries; 1.0 = ~2ms (seamless)
MIX_LOWPASS_HZ = 8000      # final low-pass on the whole mix (lower = warmer)

# Per-layer mix levels (0.0–1.0)
LAYER_GLITCH_LEVEL  = 0.30   # raw scanline / glitch layer
LAYER_TONAL_LEVEL   = 0.45   # pitched oscillators from hue clusters
LAYER_RHYTHM_LEVEL  = 0.35   # percussive layer from texture repetition

# ─── Glitch Layer ─────────────────────────────────────────────────────────────
GLITCH_SCANLINES    = 8      # how many pixel rows to use as raw waveform data
GLITCH_PITCH_SHIFT  = 1.0    # playback speed multiplier (1.0 = native)
GLITCH_LOW_PASS_HZ  = 6000   # low-pass cutoff to tame harshness

# ─── Tonal Layer ──────────────────────────────────────────────────────────────
TONAL_NUM_VOICES    = 3      # number of hue clusters → oscillators
TONAL_OCTAVE_BASE   = 3      # lowest octave (C3 = ~130 Hz)
TONAL_OCTAVE_RANGE  = 3      # voices spread across this many octaves
TONAL_WAVEFORM      = "sine" # "sine" | "triangle" | "sawtooth" | "square"
TONAL_DETUNE_CENTS  = 8      # slight detune between voices for richness

# Map hue (0–360°) to a pentatonic scale — two octaves so different hues
# land on more distinct pitches
TONAL_SCALE_SEMITONES = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24]

# ─── Rhythm Layer ─────────────────────────────────────────────────────────────
RHYTHM_BPM          = 90     # base tempo
RHYTHM_SUBDIVISIONS = 16     # steps per bar (16 = sixteenth notes)
RHYTHM_TEXTURE_THRESH = 0.25 # FFT repetition threshold to trigger a hit
RHYTHM_DECAY_MS     = 80     # percussive hit decay in milliseconds

# Kick / snare / hi-hat frequency bands (Hz)
RHYTHM_KICK_HZ  = 60
RHYTHM_SNARE_HZ = 200
RHYTHM_HAT_HZ   = 8000

# ─── Future: GPIO ─────────────────────────────────────────────────────────────
# Uncomment and wire up buttons when you're ready
# GPIO_BTN_FREEZE   = 17   # hold current frame
# GPIO_BTN_MODE     = 27   # cycle through presets
# GPIO_BTN_SNAPSHOT = 22   # save frame+audio to disk
# GPIO_LED_ACTIVE   = 18   # blink when processing

# ─── Future: Display ──────────────────────────────────────────────────────────
# DISPLAY_ENABLED = False
# DISPLAY_TYPE    = "ssd1306"  # "ssd1306" | "ili9341" | "hdmi"
# DISPLAY_WIDTH   = 128
# DISPLAY_HEIGHT  = 64

# ─── Motion ───────────────────────────────────────────────────────────────────
MOTION_SENSITIVITY     = 1.5  # scales raw frame-diff into 0–1 motion_amount
MOTION_PITCH_SEMITONES = 7    # max pitch bend (±semitones) from horizontal motion
MOTION_TEMPO_SCALE     = 0.5  # max fractional BPM increase from motion (0 = none)

# ─── Presets ──────────────────────────────────────────────────────────────────
# Each preset is a dict of config overrides. Load with load_preset(name).
PRESETS = {
    # Balanced starting point — all three layers active at moderate levels.
    "default": {
        "BLEND_ALPHA": 0.45,
        "AUDIO_SECONDS": 1.5,
        "LAYER_GLITCH_LEVEL": 0.15,
        "LAYER_TONAL_LEVEL": 0.55,
        "LAYER_RHYTHM_LEVEL": 0.30,
        "TONAL_WAVEFORM": "sine",
        "TONAL_NUM_VOICES": 3,
        "TONAL_OCTAVE_RANGE": 3,
        "TONAL_DETUNE_CENTS": 8,
        "GLITCH_PITCH_SHIFT": 1.0,
        "GLITCH_LOW_PASS_HZ": 6000,
        "RHYTHM_BPM": 90,
        "RHYTHM_DECAY_MS": 100,
        "RHYTHM_TEXTURE_THRESH": 0.25,
        "MIX_LOWPASS_HZ": 7000,
        "MOTION_SENSITIVITY": 1.5,
        "MOTION_PITCH_SEMITONES": 7,
        "MOTION_TEMPO_SCALE": 0.5,
        "CROSSFADE_SMOOTHNESS": 0.0,
    },
    # Slow, warm drone. Good for calm scenes or background listening.
    # Glitch off, tonal dominant, soft rhythm, warm low-pass, wide pitch range.
    "ambient": {
        "BLEND_ALPHA": 0.60,
        "AUDIO_SECONDS": 1.5,
        "LAYER_GLITCH_LEVEL": 0.0,
        "LAYER_TONAL_LEVEL": 0.75,
        "LAYER_RHYTHM_LEVEL": 0.10,
        "TONAL_WAVEFORM": "triangle",
        "TONAL_NUM_VOICES": 4,
        "TONAL_OCTAVE_RANGE": 4,
        "TONAL_DETUNE_CENTS": 14,
        "GLITCH_PITCH_SHIFT": 1.0,
        "GLITCH_LOW_PASS_HZ": 4000,
        "RHYTHM_BPM": 50,
        "RHYTHM_DECAY_MS": 300,
        "RHYTHM_TEXTURE_THRESH": 0.40,
        "MIX_LOWPASS_HZ": 5500,
        "MOTION_SENSITIVITY": 2.0,
        "MOTION_PITCH_SEMITONES": 12,
        "MOTION_TEMPO_SCALE": 0.3,
        "CROSSFADE_SMOOTHNESS": 0.7,
    },
    # Harsh, reactive, noisy. Glitch layer dominant, fast frames, bright mix.
    # Moving camera causes radical pitch and tempo shifts.
    "glitchy": {
        "BLEND_ALPHA": 0.85,
        "AUDIO_SECONDS": 0.75,
        "LAYER_GLITCH_LEVEL": 0.65,
        "LAYER_TONAL_LEVEL": 0.20,
        "LAYER_RHYTHM_LEVEL": 0.25,
        "TONAL_WAVEFORM": "sawtooth",
        "TONAL_NUM_VOICES": 2,
        "TONAL_OCTAVE_RANGE": 3,
        "TONAL_DETUNE_CENTS": 28,
        "GLITCH_PITCH_SHIFT": 2.5,
        "GLITCH_LOW_PASS_HZ": 12000,
        "RHYTHM_BPM": 130,
        "RHYTHM_DECAY_MS": 35,
        "RHYTHM_TEXTURE_THRESH": 0.15,
        "MIX_LOWPASS_HZ": 13000,
        "MOTION_SENSITIVITY": 3.0,
        "MOTION_PITCH_SEMITONES": 14,
        "MOTION_TEMPO_SCALE": 0.8,
        "CROSSFADE_SMOOTHNESS": 0.0,
    },
    # Tight, punchy drums driven by image texture. Minimal glitch, narrow tonal range.
    # Works best with high-contrast scenes with repeated patterns.
    "percussive": {
        "BLEND_ALPHA": 0.50,
        "AUDIO_SECONDS": 1.0,
        "LAYER_GLITCH_LEVEL": 0.08,
        "LAYER_TONAL_LEVEL": 0.28,
        "LAYER_RHYTHM_LEVEL": 0.78,
        "TONAL_WAVEFORM": "triangle",
        "TONAL_NUM_VOICES": 3,
        "TONAL_OCTAVE_RANGE": 2,
        "TONAL_DETUNE_CENTS": 5,
        "GLITCH_PITCH_SHIFT": 1.0,
        "GLITCH_LOW_PASS_HZ": 5000,
        "RHYTHM_BPM": 120,
        "RHYTHM_DECAY_MS": 55,
        "RHYTHM_TEXTURE_THRESH": 0.18,
        "MIX_LOWPASS_HZ": 9000,
        "MOTION_SENSITIVITY": 2.0,
        "MOTION_PITCH_SEMITONES": 5,
        "MOTION_TEMPO_SCALE": 0.7,
        "CROSSFADE_SMOOTHNESS": 0.0,
    },
}


def load_preset(name: str) -> None:
    """Apply a named preset by overwriting module-level vars. Call from main."""
    import sys
    cfg = sys.modules[__name__]
    preset = PRESETS.get(name, {})
    for k, v in preset.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
