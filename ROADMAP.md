# Roadmap — things worth investigating

Parking lot for follow-ups that aren't blocking but are worth picking up.

## Audio cutouts / pops — remaining hypothesis

The ~10 s skip and most of the crackling were addressed by moving the
`_dump_debug_wav` work outside `self._lock` and setting an explicit
`blocksize=2048` on the output stream. If pops or skips persist after
that, the remaining suspect is:

### Main-thread pre-render running long

The synth's invariant is that `update()` finishes inside the camera frame
budget (200 ms at 5 fps). If it ever overruns, the ring buffer's "future"
ahead of the read cursor decays toward silence at the head and the next
frame is written further in the past than intended.

This doesn't directly cause an audible cutout (the ring is 30 s deep), but
prolonged overruns could starve the audible region. Worth measuring with
the capture tooling below before assuming it's the cause.

### Diagnosis aids that already exist

- The audio callback logs `audio status` at `DEBUG` whenever PortAudio
  reports a status flag (output_underflow etc.) — see
  [src/mulchy/synthesizer.py:_callback](src/mulchy/synthesizer.py).
  Run with `-vv` or bump that line to `WARNING` to see underruns in
  `journalctl -u mulchy`.
- The debug WAV at `/tmp/mulchy_debug.wav` captures the engine's own
  output. If the WAV is clean but the speaker pops, the dropout is
  downstream of the synth (PipeWire / ALSA / analog) — see the next
  section for a stronger version of this check.

## Programmatic capture off the Pi for offline analysis

To pin down whether the pops are inside Python, between Python and the
DAC, or analog only, we need to capture audio at two points and compare:

1. **What the synth produced** (already covered by `/tmp/mulchy_debug.wav`).
2. **What actually came out of the 3.5 mm jack**, looped back into the
   capture chain.

If (1) is clean and (2) has pops at the same wall-clock timestamps, the
problem is between PipeWire and the analog output. If (2) is clean and the
external speaker still pops, it's analog/cable. If both have pops, the
synth itself is dropping samples.

### Capture option A — `parec` / `pw-record` from the monitor source

PipeWire exposes a monitor of the default sink. From the Pi:
```
pw-record --target @DEFAULT_AUDIO_SINK@.monitor /tmp/mulchy_loopback.wav
# or, via the pulse compat shim:
parec --device=@DEFAULT_AUDIO_SINK@.monitor --file-format=wav /tmp/mulchy_loopback.wav
```
This captures the bitstream PipeWire is handing to ALSA — so it'll show
pops that are introduced inside PipeWire (xruns, suspend/resume hitches)
but NOT pops introduced by the kernel ALSA driver or the analog stage.

### Capture option B — analog loopback via USB audio interface

The truthful version. Plug a USB audio interface into the Pi (or a second
Pi/laptop), patch a cable from the Pi's 3.5 mm jack into the interface's
line-in, and record. Anything audible at the speaker is in this capture.

Build an `mulchy capture` subcommand that:
- Takes a duration (default 60 s) and an output path.
- Lists available input devices via `sounddevice.query_devices()` and
  either auto-picks one matching a pattern or accepts `--device`.
- Records mono float32 to WAV at the synth's sample rate.
- Optionally also captures the monitor source (option A above) in parallel
  so the same wall-clock window is recorded at both points.

### Programmatic pop detection

Once we have WAVs, we can detect pops without listening to them:
- High-pass the signal (cutoff ~5 kHz) and look for samples above a
  threshold — clicks have broadband energy that survives the HPF; tonal
  content does not.
- Or: compute first-difference (`np.diff`) and flag samples whose
  abs-diff exceeds `k × stddev`. Cheap, no dependencies.
- Emit timestamps so the synth's own log (audio status, frame timings)
  can be correlated against detected events.

A small `mulchy analyze-pops <wav>` subcommand that prints timestamps
plus a histogram of inter-pop intervals would let us answer the
"is it every 10 s?" question deterministically.

### Getting the recordings off the Pi

`scp pi@<host>:/tmp/mulchy_*.wav .` is fine for one-offs. For repeated
use, an `--upload` flag on the capture subcommand that POSTs to a tiny
endpoint on a laptop, or just writes to a smb/sshfs mount, avoids the
manual step.
