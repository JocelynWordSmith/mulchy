"""Mulchy — synthesizer (v3).

Pre-render + ring-buffer architecture. The expensive work (cycle resampling,
lifetime envelopes, role envelopes, convolution reverb) all happens in the
main thread, in the 200 ms window between camera frames. The audio thread
does nothing except a numpy memcpy from a continuous ring buffer to the
audio device — it cannot starve, cannot underrun, cannot click.

Why this shape: in real-time callbacks Python can't reliably synthesize
six voices with envelopes and filters inside a ~21 ms callback budget on
a Pi. The earlier sounddevice-callback version was constantly producing
underruns that sounded like "clipping in and out." Pre-rendering moves
the heavy math to a thread that *has* the time budget.

Each frame arrives, the analyzer hands us (6 voices × 1024-sample cycles)
plus features. We render 9 s of audio per voice — cycle samples × lifetime
envelope (1.5 s in / 3 s hold / 4.5 s out) × role envelope (drone LFO,
bowl swell, or pluck transient) — sum them, apply convolution reverb on
the mix, and additively mix the result into a 30-second ring buffer
starting at the current playback time. Multiple frames' contributions
overlap in the buffer; old contributions fade out via their lifetime
envelope and the buffer's read cursor zeroes everything as it consumes it.

A debug WAV tap captures the last N seconds of output to /tmp/mulchy_debug.wav
so the engine's actual output can be inspected without speakers."""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

from mulchy import config as cfg
from mulchy.analyzer import CYCLE_SAMPLES, VOICES

log = logging.getLogger(__name__)

# ── Compositional constants ──────────────────────────────────────────────
VOICE_ROLES   = ("drone", "bowl", "bowl", "bowl", "pluck", "pluck")
VOICE_RATIOS  = (1.0, 4.0 / 3.0, 1.5, 2.0, 2.5, 4.0)
# Per-voice gains. Multiple source layers per voice sum linearly (up to ~4
# overlapping fades), so these are deliberately set so a worst-case
# 4-overlap peak per voice is still ≤ ~1.0 before the master stage.
VOICE_GAINS   = (0.22, 0.18, 0.15, 0.13, 0.20, 0.16)
# Per-voice lowpass cutoffs. Squiggle cycle waveforms carry a high-frequency
# carrier (~60 sine periods baked into each 1024-sample cycle); without
# filtering the dominant audible energy lands at ~F_base × 60 (≈ 4.8 kHz
# at 80 Hz base), which is what made the unfiltered build sound like
# whistling/screech. These cutoffs tame the carrier and leave the slow
# darkness-envelope content, mirroring v2's BiquadFilterNode.
FILTER_CUTOFFS = {"drone": 350.0, "bowl": 1500.0, "pluck": 3000.0}

MASTER_GAIN = 0.18

DRONE_LFO_HZ    = 0.04
DRONE_LFO_DEPTH = 0.25

BOWL_PERIODS_S = (17.0, 23.0, 29.0)
BOWL_OFFSETS_S = (3.0,  8.0,  14.0)

PLUCK_MIN_S    = (10.0, 7.0)
PLUCK_MAX_S    = (22.0, 16.0)
PLUCK_DECAY_S  = (1.8,  1.1)
PLUCK_ATTACK_S = 0.02

SOURCE_FADE_IN_S  = 1.5
SOURCE_SUSTAIN_S  = 3.0
SOURCE_FADE_OUT_S = 4.5
SOURCE_LIFETIME_S = SOURCE_FADE_IN_S + SOURCE_SUSTAIN_S + SOURCE_FADE_OUT_S  # 9.0 s

HUE_PITCH_RANGE = 1.0   # ± half octave
ENERGY_MIN = 0.85
ENERGY_MAX = 1.15

REVERB_DURATION_S = 2.0
REVERB_DECAY      = 3.0
# Saturation modulates reverb wet between MIN (muted/grey frames feel dry and
# close) and MAX (vivid frames bloom into a larger space). The old fixed
# REVERB_WET=0.35 sits roughly in the middle of this range.
REVERB_WET_MIN    = 0.20
REVERB_WET_MAX    = 0.65

# ── Conditional layers ──────────────────────────────────────────────────
# Two extra sources that only fade in when the image crosses a threshold,
# so different scenes sound categorically different rather than just louder
# or quieter. Both render alongside the six per-frame voices and inherit
# the same per-frame lifetime envelope (so successive frames overlap into
# a continuous layer).
#
# Sub-bass: a single low sine that appears in dark frames. Fixed pitch (does
# NOT follow hue) so it stays as a stable foundation; otherwise overlapping
# frames at slightly different pitches would beat audibly at sub frequencies.
SUBBASS_RATIO        = 0.5     # half base_freq → ~40 Hz at 80 Hz base
SUBBASS_GAIN         = 0.12
SUBBASS_DARK_THRESH  = 0.25    # gate is full ON when brightness ≤ this
SUBBASS_FADE         = 0.15    # …and full OFF at thresh + fade
# Sub-bass routes around the reverb (dry only) — wet sub turns to mud.

# Shimmer: a handful of high partials, faintly detuned away from integer
# ratios for a glassier, less "stacked-third" timbre. Follows hue pitch
# with the other voices. Routed through the reverb tail.
SHIMMER_RATIOS         = (5.0, 7.07, 9.13)
SHIMMER_GAIN           = 0.08
SHIMMER_SAT_THRESH     = 0.55   # gate is full OFF when saturation ≤ this
SHIMMER_FADE           = 0.15   # …and full ON at thresh + fade
SHIMMER_TREMOLO_HZ     = 0.7
SHIMMER_TREMOLO_DEPTH  = 0.5

RING_BUFFER_SECONDS = 30   # generous; never overflows at 5 fps

DEBUG_WAV_PATH      = Path("/tmp/mulchy_debug.wav")
DEBUG_WAV_INTERVAL_S = 10.0  # rewrite the debug WAV every N seconds


# ── Envelope helpers (all pure numpy) ────────────────────────────────────

def _lifetime_envelope(t: np.ndarray) -> np.ndarray:
    """1.5 s fade-in → 3 s sustain → 4.5 s fade-out, sampled at times t."""
    env = np.zeros_like(t, dtype=np.float32)
    in_in  = (t >= 0)                            & (t < SOURCE_FADE_IN_S)
    in_sus = (t >= SOURCE_FADE_IN_S)             & (t < SOURCE_FADE_IN_S + SOURCE_SUSTAIN_S)
    in_out = (t >= SOURCE_FADE_IN_S + SOURCE_SUSTAIN_S) & (t < SOURCE_LIFETIME_S)
    env[in_in]  = t[in_in] / SOURCE_FADE_IN_S
    env[in_sus] = 1.0
    env[in_out] = 1.0 - (t[in_out] - (SOURCE_FADE_IN_S + SOURCE_SUSTAIN_S)) / SOURCE_FADE_OUT_S
    return env


def _bowl_envelope(t_abs: np.ndarray, period: float, offset: float) -> np.ndarray:
    attack  = period * 0.08
    sustain = period * 0.40
    decay   = period * 0.30
    phase = (t_abs - offset) % period
    env = np.zeros_like(phase, dtype=np.float32)
    a = (phase >= 0)          & (phase < attack)
    s = (phase >= attack)     & (phase < attack + sustain)
    d = (phase >= attack + sustain) & (phase < attack + sustain + decay)
    env[a] = phase[a] / attack
    env[s] = 1.0
    env[d] = 1.0 - (phase[d] - attack - sustain) / decay
    return env


def _pluck_event_env(t_abs: np.ndarray, trig: float, decay_s: float) -> np.ndarray:
    rel = t_abs - trig
    env = np.zeros_like(rel, dtype=np.float32)
    a = (rel >= 0) & (rel < PLUCK_ATTACK_S)
    d = (rel >= PLUCK_ATTACK_S) & (rel < PLUCK_ATTACK_S + decay_s)
    env[a] = rel[a] / PLUCK_ATTACK_S
    if d.any():
        env[d] = np.exp(-3.0 * (rel[d] - PLUCK_ATTACK_S) / decay_s)
    return env


def _make_lowpass_sos(cutoff_hz: float, sr: int) -> np.ndarray:
    """2nd-order Butterworth lowpass as SOS."""
    nyq = sr / 2.0
    norm = max(20.0, min(cutoff_hz, nyq * 0.95)) / nyq
    return butter(2, norm, btype="low", output="sos")


def _make_reverb_ir(sr: int, duration_s: float, decay: float) -> np.ndarray:
    n = max(1, int(duration_s * sr))
    rng = np.random.default_rng(42)
    ir = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    env = np.power(1.0 - np.arange(n) / n, decay).astype(np.float32)
    ir *= env
    # Normalise so reverb wet level is roughly in the same ballpark as the dry.
    ir /= max(1e-6, float(np.sqrt((ir * ir).sum())))
    return ir


# ── Synthesizer ──────────────────────────────────────────────────────────

class Synthesizer:
    def __init__(
        self,
        sample_rate: int | None = None,
        base_freq: float = 80.0,
        audio_enabled: bool = True,
        record_debug_wav: bool = True,
    ):
        self.sr = int(sample_rate or cfg.SAMPLE_RATE)
        self.base_freq = float(base_freq)

        self._buffer_size = RING_BUFFER_SECONDS * self.sr
        self._ring = np.zeros(self._buffer_size, dtype=np.float32)
        self._read_pos = 0   # audio thread advances
        self._lock = threading.Lock()

        self._reverb_ir = _make_reverb_ir(self.sr, REVERB_DURATION_S, REVERB_DECAY)

        # Pre-compute per-voice lowpass SOS. Applied to each frame's voice
        # contribution before mixing — kills the carrier-frequency energy
        # baked into the squiggle cycle so we hear the slow envelope
        # content (which is what the v2 sandbox sounds like).
        self._voice_sos = [
            _make_lowpass_sos(FILTER_CUTOFFS[r], self.sr) for r in VOICE_ROLES
        ]

        self._pluck_events: list[list[dict]] = [[], []]
        self._pluck_next_check = [3.0, 6.0]
        self._rng = random.Random(42)

        # Feature-derived state.
        self.hue_pitch_mult = 1.0
        self.energy_gain = 1.0
        self.pluck_rate_mult = 1.0

        self._record_debug_wav = record_debug_wav
        self._debug_wav_next_dump = DEBUG_WAV_INTERVAL_S

        # When audio is disabled (--no-audio / no sounddevice), the audio
        # thread isn't running and read_pos would stay at 0 forever, so
        # mixed frames would all stack on top of each other. Track wall
        # clock and advance read_pos manually in that mode.
        self._wallclock_last_t: float | None = None

        self._stream = None
        if audio_enabled:
            self._open_stream()
        log.info(
            "Synthesizer ready: %d Hz, base=%.1f Hz, ring=%d s, record_wav=%s",
            self.sr, self.base_freq, RING_BUFFER_SECONDS, self._record_debug_wav,
        )

    # ── Audio stream (trivial callback — never starves) ──────────────

    def _open_stream(self) -> None:
        try:
            import sounddevice as sd
        except Exception as e:
            log.warning("sounddevice unavailable (%s) — running silently", e)
            return
        # blocksize=2048 (~93 ms at 22.05 kHz) gives PortAudio enough grace
        # that a single slow main-thread frame (fftconvolve + sosfilt) or a
        # ~ms-scale lock hold doesn't underrun the output. blocksize=0 lets
        # PortAudio pick, which on a busy Pi 3B tends to land at 512–1024
        # frames — marginal under load and the source of audible crackle.
        self._stream = sd.OutputStream(
            samplerate=self.sr,
            channels=1,
            dtype="float32",
            blocksize=2048,
            callback=self._callback,
        )
        self._stream.start()
        log.info("Audio stream started")

    def _callback(self, outdata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        with self._lock:
            start = self._read_pos % self._buffer_size
            end = self._read_pos + frames
            if end <= self._read_pos + (self._buffer_size - start):
                outdata[:, 0] = self._ring[start:start + frames]
                self._ring[start:start + frames] = 0.0
            else:
                first = self._buffer_size - start
                outdata[:first, 0] = self._ring[start:]
                outdata[first:, 0] = self._ring[: frames - first]
                self._ring[start:] = 0.0
                self._ring[: frames - first] = 0.0
            self._read_pos += frames

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        # Always capture a final snapshot so smoke tests can inspect the
        # engine's output without needing speakers.
        if self._record_debug_wav:
            self._dump_debug_wav()

    def reset(self) -> None:
        with self._lock:
            self._ring.fill(0.0)
            self._read_pos = 0
        self._pluck_events = [[], []]
        self._pluck_next_check = [3.0, 6.0]

    # ── Main-thread API ──────────────────────────────────────────────

    def update(self, voices: np.ndarray, features: dict[str, float]) -> None:
        """Pre-render this frame's 9-second contribution and mix into the
        ring buffer. Called once per camera frame from the main loop."""
        if voices.shape != (VOICES, CYCLE_SAMPLES):
            return
        self._apply_features(features)

        # Headless mode: advance the read cursor by wall-clock delta so
        # successive updates land at successive time offsets in the ring
        # buffer (instead of all stacking on read_pos=0).
        if self._stream is None:
            now = time.monotonic()
            if self._wallclock_last_t is not None:
                advance = max(0, int((now - self._wallclock_last_t) * self.sr))
                with self._lock:
                    self._read_pos += advance
            self._wallclock_last_t = now

        with self._lock:
            t_now_samples = self._read_pos
        t_now = t_now_samples / self.sr

        n = int(SOURCE_LIFETIME_S * self.sr)
        t_rel = np.arange(n, dtype=np.float32) / self.sr
        t_abs = (t_now + t_rel).astype(np.float32)
        lifetime = _lifetime_envelope(t_rel)

        # Schedule any plucks that should fire in this window.
        self._schedule_plucks(t_now)

        master = np.zeros(n, dtype=np.float32)
        for v_idx in range(VOICES):
            master += self._render_voice(voices[v_idx], v_idx, t_abs, lifetime)

        # Shimmer joins master before reverb so it picks up the wet tail —
        # that's what makes high partials feel "shimmery" rather than just
        # bright. Sub-bass stays out of master and is mixed dry only.
        shim = self._render_shimmer(t_abs, lifetime, float(features.get("saturation", 0.5)))
        if shim is not None:
            master += shim
        sub = self._render_subbass(t_abs, lifetime, float(features.get("brightness", 0.5)))

        # Convolution reverb: produces a tail longer than `n` samples. Mix the
        # full tail into the ring buffer too so reverb continues past the
        # source's lifetime.
        wet = fftconvolve(master, self._reverb_ir, mode="full").astype(np.float32)
        wet_amount = REVERB_WET_MIN + (REVERB_WET_MAX - REVERB_WET_MIN) * float(
            features.get("saturation", 0.5)
        )
        mix = np.zeros(len(wet), dtype=np.float32)
        mix[:n] = master * (1.0 - wet_amount)
        if sub is not None:
            mix[:n] += sub
        mix += wet * wet_amount
        mix *= self.energy_gain * MASTER_GAIN

        # Tanh soft-saturation as a final safety. Cheap and prevents the
        # ring buffer from ever exceeding ±1 even when many frames stack.
        np.tanh(mix, out=mix)

        with self._lock:
            self._mix_into_ring(t_now_samples, mix)

        # Debug WAV: snapshot the past ~RING_BUFFER_SECONDS-ish every interval.
        if self._record_debug_wav and t_now >= self._debug_wav_next_dump:
            self._dump_debug_wav()
            self._debug_wav_next_dump = t_now + DEBUG_WAV_INTERVAL_S

    # ── Internals ────────────────────────────────────────────────────

    def _apply_features(self, f: dict[str, float]) -> None:
        b = float(f.get("brightness", 0.5))
        e = float(f.get("edge_density", 0.5))
        h = float(f.get("hue", 0.5))
        m = float(f.get("motion", 0.0))
        self.hue_pitch_mult = 2.0 ** ((h - 0.5) * HUE_PITCH_RANGE)
        self.energy_gain = ENERGY_MIN + (ENERGY_MAX - ENERGY_MIN) * b
        edge_factor = 2.0 - 1.6 * e
        motion_factor = 1.0 - 0.55 * m
        self.pluck_rate_mult = max(0.25, edge_factor * motion_factor)

    def _render_voice(
        self,
        cycle: np.ndarray,
        v_idx: int,
        t_abs: np.ndarray,
        lifetime: np.ndarray,
    ) -> np.ndarray:
        n = len(t_abs)
        sample_step = (
            self.base_freq * self.hue_pitch_mult * VOICE_RATIOS[v_idx]
            * CYCLE_SAMPLES / self.sr
        )
        positions = (np.arange(n, dtype=np.float32) * sample_step) % CYCLE_SAMPLES
        floor = positions.astype(np.int32)
        frac = positions - floor
        nxt = (floor + 1) % CYCLE_SAMPLES
        samples = cycle[floor] * (1.0 - frac) + cycle[nxt] * frac

        role = VOICE_ROLES[v_idx]
        if role == "drone":
            role_env = (1.0 + DRONE_LFO_DEPTH *
                        np.sin(2.0 * math.pi * DRONE_LFO_HZ * t_abs)).astype(np.float32)
        elif role == "bowl":
            b_idx = v_idx - 1
            role_env = _bowl_envelope(t_abs,
                                      BOWL_PERIODS_S[b_idx],
                                      BOWL_OFFSETS_S[b_idx])
        else:  # pluck
            p_idx = v_idx - 4
            role_env = self._pluck_envelope_in_window(p_idx, t_abs)

        # Lifetime envelope + cycle samples first, then through per-voice
        # lowpass (kills the squiggle carrier so what's audible is the
        # slow image-darkness envelope), then role envelope, then voice gain.
        # The lifetime envelope is 0 at t=0 so the filter sees a clean
        # zero start and doesn't ring.
        signal = (samples * lifetime).astype(np.float32)
        filtered = sosfilt(self._voice_sos[v_idx], signal).astype(np.float32)
        out = filtered * role_env * VOICE_GAINS[v_idx]
        return out.astype(np.float32)

    def _render_subbass(
        self, t_abs: np.ndarray, lifetime: np.ndarray, brightness: float,
    ) -> np.ndarray | None:
        """Low sine that fades in on dark frames. Returns None when the gate
        is fully closed so the caller can skip the dry-mix add."""
        gate = (SUBBASS_DARK_THRESH + SUBBASS_FADE - brightness) / SUBBASS_FADE
        gate = float(np.clip(gate, 0.0, 1.0))
        if gate <= 0.0:
            return None
        # Fixed pitch (NOT scaled by hue_pitch_mult): overlapping frames
        # from a wobbling pitch would beat at sub frequencies and sound bad.
        f = self.base_freq * SUBBASS_RATIO
        sig = np.sin(2.0 * math.pi * f * t_abs).astype(np.float32)
        return (sig * lifetime * (gate * SUBBASS_GAIN)).astype(np.float32)

    def _render_shimmer(
        self, t_abs: np.ndarray, lifetime: np.ndarray, saturation: float,
    ) -> np.ndarray | None:
        """High detuned partials that fade in on saturated frames. Follows
        hue pitch with the other voices and routes through the reverb."""
        gate = (saturation - SHIMMER_SAT_THRESH) / SHIMMER_FADE
        gate = float(np.clip(gate, 0.0, 1.0))
        if gate <= 0.0:
            return None
        sig = np.zeros(len(t_abs), dtype=np.float32)
        for ratio in SHIMMER_RATIOS:
            f = self.base_freq * self.hue_pitch_mult * ratio
            sig += np.sin(2.0 * math.pi * f * t_abs).astype(np.float32)
        sig /= float(len(SHIMMER_RATIOS))
        # Slow tremolo: depth=0.5 means amplitude swings between 0.5 and 1.0.
        tremolo = (
            1.0 - 0.5 * SHIMMER_TREMOLO_DEPTH
            * (1.0 - np.cos(2.0 * math.pi * SHIMMER_TREMOLO_HZ * t_abs))
        ).astype(np.float32)
        return (sig * tremolo * lifetime * (gate * SHIMMER_GAIN)).astype(np.float32)

    def _pluck_envelope_in_window(self, p_idx: int, t_abs: np.ndarray) -> np.ndarray:
        env = np.zeros_like(t_abs, dtype=np.float32)
        win_start = float(t_abs[0])
        win_end   = float(t_abs[-1])
        for ev in self._pluck_events[p_idx]:
            trig = ev["trigger_time"]
            decay = ev["decay"]
            if trig > win_end or trig + PLUCK_ATTACK_S + decay < win_start:
                continue
            np.maximum(env, _pluck_event_env(t_abs, trig, decay), out=env)
        return env

    def _schedule_plucks(self, t_now: float) -> None:
        """Make sure each pluck voice has one future scheduled event."""
        for p_idx in range(2):
            if t_now + SOURCE_LIFETIME_S >= self._pluck_next_check[p_idx]:
                lo = PLUCK_MIN_S[p_idx]
                hi = PLUCK_MAX_S[p_idx]
                delay = self._rng.uniform(lo, hi) * self.pluck_rate_mult
                trig = self._pluck_next_check[p_idx] + max(0.1, delay)
                self._pluck_events[p_idx].append(
                    {"trigger_time": trig, "decay": PLUCK_DECAY_S[p_idx]}
                )
                self._pluck_next_check[p_idx] = trig
            # Drop pluck events whose tail is fully in the past.
            self._pluck_events[p_idx] = [
                ev for ev in self._pluck_events[p_idx]
                if ev["trigger_time"] + PLUCK_ATTACK_S + ev["decay"] > t_now - 1.0
            ]

    def _mix_into_ring(self, start_samples: int, audio: np.ndarray) -> None:
        n = len(audio)
        if n > self._buffer_size:
            # Should never happen — but truncate instead of crashing if it does.
            audio = audio[: self._buffer_size]
            n = self._buffer_size
        start = start_samples % self._buffer_size
        if start + n <= self._buffer_size:
            self._ring[start:start + n] += audio
            return
        first = self._buffer_size - start
        self._ring[start:] += audio[:first]
        self._ring[: n - first] += audio[first:]

    def _dump_debug_wav(self) -> None:
        """Capture roughly RING_BUFFER_SECONDS of recent audio to a WAV
        file. Reads from the ring buffer's "future" region (past read_pos)
        plus a snapshot of what we just mixed."""
        try:
            # Hold the lock only long enough for a contiguous memcpy of the
            # ring (~2.6 MB at 22.05 kHz × 30 s × float32 ≈ sub-millisecond
            # on a Pi 3B). The earlier version did a fancy-indexed gather
            # with a 5 MB int64 arange under the lock; that block ran tens
            # of ms and starved the audio callback every DEBUG_WAV_INTERVAL_S
            # seconds — which is exactly what the ~10 s skip sounded like.
            with self._lock:
                read_pos_now = self._read_pos
                ring_copy = self._ring.copy()
            # Reorder into playback order outside the lock. Starting a
            # quarter-buffer behind read_pos gives the most recent audio in
            # the order it played; positions past read_pos haven't played
            # yet (future-mixed content).
            base = (read_pos_now - self._buffer_size // 4) % self._buffer_size
            snap = np.concatenate([ring_copy[base:], ring_copy[:base]])
            # int16 WAV at sr.
            clipped = np.clip(snap, -1.0, 1.0)
            ints = (clipped * 32767).astype(np.int16)
            with wave.open(str(DEBUG_WAV_PATH), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sr)
                wf.writeframes(ints.tobytes())
            log.info("Debug WAV updated: %s (%d s)", DEBUG_WAV_PATH, RING_BUFFER_SECONDS)
        except Exception as e:  # pragma: no cover - diagnostic, must not crash audio
            log.warning("debug WAV dump failed: %s", e)


__all__ = ["Synthesizer", "VOICES", "CYCLE_SAMPLES"]
