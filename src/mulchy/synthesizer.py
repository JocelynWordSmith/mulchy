"""
Mulchy - Synthesizer
Takes an ImageFeatures dict and produces a numpy audio buffer.
Three layers: glitch (raw scanline), tonal (hue→pitch), rhythm (texture→drums).
"""

import numpy as np
from pedalboard import Chorus, Compressor, Pedalboard, Reverb
from scipy.signal import butter, sosfilt

from mulchy import config as cfg
from mulchy.analyzer import ImageFeatures

# ── Filter / envelope caches ──────────────────────────────────────────────────
# Butterworth design is expensive; cache by (rounded) cutoff frequency.
_lp_sos_cache: dict  = {}   # hz_key → sos coefficients
_adsr_cache: dict    = {}   # n_samples → envelope array
_time_cache: dict    = {}   # n_samples → time array
_arange_cache: dict  = {}   # n_samples → np.arange(n) int array
_taper_cache: dict   = {}   # taper_n → cosine taper array
_noise_buf: np.ndarray = np.random.default_rng(42).uniform(-1.0, 1.0, 44100)

# ── Synth state (persists across frames) ─────────────────────────────────────
_tonal_phases: dict[int, float] = {}   # voice_idx → phase in radians
_prev_tail: np.ndarray | None = None   # last overlap_n samples for crossfade


_fx_board: Pedalboard | None = None
_fx_config_hash: tuple | None = None


def reset_synth_state():
    """Clear all stateful synth data. Call between tests."""
    global _prev_tail, _fx_board, _fx_config_hash
    _tonal_phases.clear()
    _prev_tail = None
    _fx_board = None
    _fx_config_hash = None


def _get_fx_board() -> Pedalboard | None:
    """Build or return cached pedalboard effects chain. Returns None if all FX disabled."""
    global _fx_board, _fx_config_hash
    current_hash = (
        cfg.FX_REVERB_ENABLED, cfg.FX_REVERB_ROOM_SIZE, cfg.FX_REVERB_WET,
        cfg.FX_CHORUS_ENABLED, cfg.FX_CHORUS_RATE, cfg.FX_CHORUS_DEPTH, cfg.FX_CHORUS_MIX,
        cfg.FX_COMPRESSOR_ENABLED, cfg.FX_COMPRESSOR_THRESHOLD, cfg.FX_COMPRESSOR_RATIO,
    )
    if current_hash == _fx_config_hash:
        return _fx_board

    plugins = []
    if cfg.FX_CHORUS_ENABLED:
        plugins.append(Chorus(
            rate_hz=cfg.FX_CHORUS_RATE,
            depth=cfg.FX_CHORUS_DEPTH,
            mix=cfg.FX_CHORUS_MIX,
        ))
    if cfg.FX_REVERB_ENABLED:
        plugins.append(Reverb(
            room_size=cfg.FX_REVERB_ROOM_SIZE,
            wet_level=cfg.FX_REVERB_WET,
            dry_level=1.0 - cfg.FX_REVERB_WET,
        ))
    if cfg.FX_COMPRESSOR_ENABLED:
        plugins.append(Compressor(
            threshold_db=cfg.FX_COMPRESSOR_THRESHOLD,
            ratio=cfg.FX_COMPRESSOR_RATIO,
        ))

    _fx_board = Pedalboard(plugins) if plugins else None
    _fx_config_hash = current_hash
    return _fx_board


def _get_sos(hz: float, order: int = 2) -> np.ndarray:
    key = (round(hz / 100) * 100, order)
    if key not in _lp_sos_cache:
        _lp_sos_cache[key] = butter(order, key[0], fs=cfg.SAMPLE_RATE,
                                    btype="low", output="sos")
    return _lp_sos_cache[key]


def _get_adsr(n: int) -> np.ndarray:
    if n not in _adsr_cache:
        _adsr_cache[n] = _adsr(n, attack=0.20, decay=0.05, sustain=0.85, release=0.10)
    return _adsr_cache[n]


def _get_time(n: int) -> np.ndarray:
    if n not in _time_cache:
        _time_cache[n] = np.arange(n, dtype=np.float64) / cfg.SAMPLE_RATE
    return _time_cache[n]


def _get_arange(n: int) -> np.ndarray:
    if n not in _arange_cache:
        _arange_cache[n] = np.arange(n)
    return _arange_cache[n]


def _get_taper(taper_n: int) -> np.ndarray:
    if taper_n not in _taper_cache:
        _taper_cache[taper_n] = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, taper_n)))
    return _taper_cache[taper_n]


def synthesize(features: ImageFeatures) -> np.ndarray:
    """
    Render one audio chunk from image features.
    Returns float32 numpy array, shape (N,), values roughly -1..1.
    """
    n_samples = int(cfg.SAMPLE_RATE * cfg.AUDIO_SECONDS)

    glitch  = _layer_glitch(features, n_samples)
    tonal   = _layer_tonal(features, n_samples)
    rhythm  = _layer_rhythm(features, n_samples)

    # Mix layers
    mixed = (
        glitch  * cfg.LAYER_GLITCH_LEVEL +
        tonal   * cfg.LAYER_TONAL_LEVEL  +
        rhythm  * cfg.LAYER_RHYTHM_LEVEL
    )

    # Filter cutoff opens with edge density: smooth surfaces = muted, busy textures = bright
    edge_density = features.get("edge_density", 0.5)
    lp_hz = float(np.clip(cfg.MIX_LOWPASS_HZ * (0.3 + edge_density * 1.4),
                          300, cfg.SAMPLE_RATE // 2 - 100))
    mixed = sosfilt(_get_sos(lp_hz), mixed)

    # Apply pedalboard effects chain (reverb, chorus, compressor)
    fx = _get_fx_board()
    if fx is not None:
        mixed_f32 = mixed.astype(np.float32).reshape(1, -1)
        mixed_f32 = fx(mixed_f32, cfg.SAMPLE_RATE)
        mixed = mixed_f32[0].astype(np.float64)

    # Gentle peak normalise — no saturation distortion
    peak = np.max(np.abs(mixed)) + 1e-9
    if peak > 0.0:
        mixed = mixed / peak * 0.85
    mixed *= cfg.MASTER_VOLUME

    # Overlap-add crossfading — blends the tail of the previous chunk with
    # the head of the new one, eliminating amplitude dips at boundaries.
    global _prev_tail
    taper_ms = max(2.0, 40.0 * (1.0 - cfg.CROSSFADE_SMOOTHNESS))
    taper_n = min(int(cfg.SAMPLE_RATE * taper_ms / 1000.0), n_samples // 8)
    taper = _get_taper(taper_n)

    if _prev_tail is not None and len(_prev_tail) == taper_n:
        # Crossfade: blend previous tail (fading out) with current head (fading in)
        mixed[:taper_n] = _prev_tail * taper[::-1] + mixed[:taper_n] * taper
    else:
        # First chunk: just fade in
        mixed[:taper_n] *= taper

    # Store tail for next frame's crossfade (no fade-out applied to output)
    _prev_tail = mixed[-taper_n:].copy()

    return mixed.astype(np.float32)


# ── Layer 1: Glitch (raw scanline → waveform) ─────────────────────────────────

def _layer_glitch(features: ImageFeatures, n_samples: int) -> np.ndarray:
    """
    Each scanline becomes a tiled waveform. Rows are stacked and summed.
    This is the Wii-RAM-audio effect: raw pixel data played back as sound.
    """
    if not features["scanlines"]:
        return np.zeros(n_samples)

    result = np.zeros(n_samples, dtype=np.float64)
    weight = 1.0 / len(features["scanlines"])

    motion = features.get("motion_amount", 0.0)

    for i, row in enumerate(features["scanlines"]):
        row_arr = np.array(row, dtype=np.float64) * 2.0 - 1.0  # 0..1 → -1..1

        # Motion makes the glitch layer speed up / pitch-shift more
        pitch = cfg.GLITCH_PITCH_SHIFT * (1.0 + i * 0.03 + motion * 0.5)
        stretched_len = max(1, int(len(row_arr) / pitch))
        indices = (_get_arange(n_samples) % stretched_len).astype(int)
        tiled = row_arr[np.clip(indices, 0, len(row_arr) - 1)]

        # Modulate amplitude by image brightness variance
        amp = 0.5 + features["luminance_variance"] * 2.0
        result += tiled * weight * amp

    result = sosfilt(_get_sos(cfg.GLITCH_LOW_PASS_HZ, order=4), result)

    return result


# ── Layer 2: Tonal (hue clusters → pitched oscillators) ───────────────────────

def _layer_tonal(features: ImageFeatures, n_samples: int) -> np.ndarray:
    """
    Each dominant hue maps to a pitch on a pentatonic scale.
    Weight = how dominant that colour is → amplitude of that oscillator.
    """
    t = _get_time(n_samples)
    result = np.zeros(n_samples, dtype=np.float64)

    scale = cfg.TONAL_SCALE_SEMITONES

    # Brightness shifts the register: dark scene = deep bass, bright = high & airy
    brightness = features.get("brightness", 0.5)
    bright_shift = (brightness - 0.5) * 24.0  # ±12 semitones = ±1 octave
    base_freq = _midi_to_hz(12 * cfg.TONAL_OCTAVE_BASE) * _semitones_to_ratio(bright_shift)

    # Saturation gates voices: grey/desaturated = simple drone, colourful = full chord
    saturation = features.get("saturation", 0.5)
    n_voices_total = max(1, len(features["hue_centers"]))
    n_active_voices = max(1, round(0.5 + saturation * n_voices_total))

    # Motion: horizontal pan bends pitch like a theremin; vertical shifts octave
    motion = features.get("motion_amount", 0.0)
    motion_cx = features.get("motion_cx", 0.0)
    pitch_bend_semitones = motion_cx * cfg.MOTION_PITCH_SEMITONES * motion
    pitch_bend_ratio = _semitones_to_ratio(pitch_bend_semitones)

    # Vibrato LFO: rate and depth scale with motion_amount
    lfo_rate  = 2.0 + motion * 6.0    # 2–8 Hz
    lfo_depth = motion * 0.015        # up to ±1.5% freq wobble
    lfo = 1.0 + lfo_depth * np.sin(2.0 * np.pi * lfo_rate * t)

    for voice_idx, (hue_deg, weight) in enumerate(
        zip(features["hue_centers"][:n_active_voices], features["hue_weights"][:n_active_voices])
    ):
        # Map hue (0–360) → scale degree
        hue_norm = hue_deg / 360.0
        scale_idx = int(hue_norm * len(scale)) % len(scale)
        semitone = scale[scale_idx]

        # Spread voices across octave range; vertical motion nudges octave
        octave_offset = (voice_idx % cfg.TONAL_OCTAVE_RANGE) * 12
        semitone += octave_offset

        # Slight detune for richness
        detune_hz = _semitones_to_ratio(cfg.TONAL_DETUNE_CENTS / 100.0)
        freq = base_freq * _semitones_to_ratio(semitone) * pitch_bend_ratio

        # Phase-continuous oscillator — carry phase across frames
        main_phase_key = voice_idx
        detune_phase_key = voice_idx + 1000
        start_phase = _tonal_phases.get(main_phase_key, 0.0)
        detune_start = _tonal_phases.get(detune_phase_key, 0.0)

        if cfg.TONAL_WAVEFORM == "sine":
            # Vibrato via cumulative phase modulation (LFO-FM)
            phase = start_phase + 2.0 * np.pi * freq * np.cumsum(lfo) / cfg.SAMPLE_RATE
            wave = np.sin(phase)
            _tonal_phases[main_phase_key] = float(phase[-1] % (2.0 * np.pi))
        else:
            wave, end_phase = _oscillator(cfg.TONAL_WAVEFORM, freq, t, start_phase)
            _tonal_phases[main_phase_key] = end_phase

        # Add slightly detuned copy with its own phase continuity
        detune_wave, detune_end = _oscillator(cfg.TONAL_WAVEFORM, freq * detune_hz, t, detune_start)
        wave += detune_wave * 0.4
        wave /= 1.4  # normalise after detune
        _tonal_phases[detune_phase_key] = detune_end

        amp_env = _get_adsr(n_samples) * (weight * (0.4 + features["saturation"] * 0.6))

        result += wave * amp_env

    return result / n_active_voices


def _oscillator(shape: str, freq: float, t: np.ndarray,
                start_phase: float = 0.0) -> tuple[np.ndarray, float]:
    """Generate waveform with phase continuity. Returns (waveform, end_phase)."""
    phase = start_phase + 2.0 * np.pi * freq * t
    end_phase = float(phase[-1] % (2.0 * np.pi)) if len(phase) > 0 else start_phase
    if shape == "sine":
        return np.sin(phase), end_phase
    elif shape == "triangle":
        # Use phase-based triangle for continuity
        p_norm = (phase / (2.0 * np.pi)) % 1.0
        return 2.0 * np.abs(2.0 * p_norm - 1.0) - 1.0, end_phase
    elif shape == "sawtooth":
        p_norm = (phase / (2.0 * np.pi)) % 1.0
        return 2.0 * p_norm - 1.0, end_phase
    elif shape == "square":
        return np.sign(np.sin(phase)), end_phase
    return np.sin(phase), end_phase


def _adsr(n: int, attack: float, decay: float,
          sustain: float, release: float) -> np.ndarray:
    """Simple ADSR envelope, all times as fraction of total length."""
    env = np.ones(n)
    a = int(n * attack)
    d = int(n * decay)
    r = int(n * release)
    s_level = sustain
    if a > 0:
        env[:a] = np.linspace(0, 1, a)
    if d > 0:
        env[a:a+d] = np.linspace(1, s_level, d)
    env[a+d:n-r] = s_level
    if r > 0:
        env[n-r:] = np.linspace(s_level, 0, r)
    return env


# ── Layer 3: Rhythm (texture FFT → percussive hits) ───────────────────────────

def _layer_rhythm(features: ImageFeatures, n_samples: int) -> np.ndarray:
    """
    Texture repetition scores (per quadrant) determine what hits fire when.
    TL → kick, TR → snare, BL/BR → hi-hats.
    """
    result = np.zeros(n_samples, dtype=np.float64)
    scores = features["texture_scores"]  # [TL, TR, BL, BR]

    motion = features.get("motion_amount", 0.0)
    effective_bpm = cfg.RHYTHM_BPM * (1.0 + motion * cfg.MOTION_TEMPO_SCALE)
    beat_samples = int(cfg.SAMPLE_RATE * 60.0 / effective_bpm)
    step_samples = beat_samples // (cfg.RHYTHM_SUBDIVISIONS // 4)
    n_steps = n_samples // step_samples

    tl = scores[0] if len(scores) > 0 else 0.0
    tr = scores[1] if len(scores) > 1 else 0.0
    bl = scores[2] if len(scores) > 2 else 0.0
    br = scores[3] if len(scores) > 3 else 0.0

    thresh = cfg.RHYTHM_TEXTURE_THRESH

    for step in range(n_steps):
        t_start = step * step_samples
        quarter   = step % 4 == 0
        backbeat  = step % 4 == 2
        eighth    = step % 2 == 0

        # Kick: fires on quarters if bottom-left texture is repetitive
        if quarter and tl > thresh:
            vel = 0.5 + tl * 0.5
            result = _add_hit(result, t_start, n_samples, cfg.RHYTHM_KICK_HZ, vel,
                              cfg.RHYTHM_DECAY_MS * 3)

        # Snare: fires on backbeats if top-right is repetitive
        if backbeat and tr > thresh:
            vel = 0.4 + tr * 0.5
            result = _add_hit(result, t_start, n_samples, cfg.RHYTHM_SNARE_HZ, vel,
                              cfg.RHYTHM_DECAY_MS * 1.5, noise_mix=0.5)

        # Hi-hat: fires on eighths if either right quadrant is repetitive
        hat_score = (bl + br) / 2.0
        if eighth and hat_score > thresh * 0.7:
            vel = 0.2 + hat_score * 0.3
            result = _add_hit(result, t_start, n_samples, cfg.RHYTHM_HAT_HZ, vel,
                              cfg.RHYTHM_DECAY_MS * 0.5, noise_mix=0.85)

    return result


def _add_hit(buf: np.ndarray, start: int, total: int,
             freq: float, velocity: float, decay_ms: float,
             noise_mix: float = 0.0) -> np.ndarray:
    """
    Add a single percussive hit at sample position `start`.
    Blends a sine tone with noise for different drum sounds.
    """
    decay_samples = int(cfg.SAMPLE_RATE * decay_ms / 1000.0)
    end = min(start + decay_samples, total)
    n = end - start
    if n <= 0:
        return buf

    t = _get_time(n)
    env = np.exp(-t / (decay_ms / 1000.0 * 0.3))

    tone  = np.sin(2.0 * np.pi * freq * t)
    noise = _noise_buf[:n]

    hit = tone * (1.0 - noise_mix) + noise * noise_mix
    hit *= env * velocity

    buf[start:end] += hit
    return buf


# ── Utilities ─────────────────────────────────────────────────────────────────

def _midi_to_hz(midi_note: int) -> float:
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))

def _semitones_to_ratio(semitones: float) -> float:
    return 2.0 ** (semitones / 12.0)
