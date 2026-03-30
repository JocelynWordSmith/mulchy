# Mulchy

**A standalone device that turns what the camera sees into sound.**

Inspired by the Wii RAM audio glitch — where raw memory data played back as audio revealed rhythmic structure in the underlying data — mulchy does the same with live camera frames. Repeating textures become drums. Dominant colors become pitched tones. Raw pixel rows become glitchy waveforms.

---

## Hardware

- Raspberry Pi 3B
- Raspberry Pi Camera Module 3 Wide Angle
- 3.5mm speaker or headphones (Pi audio jack)
- Optional: battery pack

## File Structure

```
mulchy/
├── config.py          ← all tunables live here, touch nothing else to tweak
├── analyzer.py        ← image → feature dict (no audio code)
├── synthesizer.py     ← feature dict → audio buffer (no image code)
├── camera.py          ← picamera2 wrapper with frame blending + test pattern
├── main.py            ← boot loop, orchestration, future hook points
├── web.py             ← Flask web dashboard and WiFi management UI
├── wifi_monitor.sh    ← watchdog script: activates AP when no client is connected
└── install.sh         ← dependency install + systemd service setup
```

## Quick Start

```bash
# Copy files to Pi
scp -r mulchy/ pi@mulchy.local:~/

# SSH in and install
ssh pi@mulchy.local
cd ~/mulchy
bash install.sh

# Run immediately (web dashboard starts automatically)
python3 main.py

# Or try a preset
python3 main.py --preset ambient
python3 main.py --preset glitchy
python3 main.py --preset percussive
```

---

## Using the Web Dashboard

Once mulchy is running, open a browser on any device on the same network and go to:

```
http://mulchy.local:5000
```

If mDNS isn't available on your device, use the Pi's IP address directly (e.g. `http://192.168.0.142:5000`).

The dashboard has three main areas:

**Video feed** — the blended camera frame with visual overlays: colored circles showing the dominant hues found in the frame, feature bars along the bottom edge (brightness, saturation, edges, motion), and a motion arrow when the scene is changing.

**Synth visualizer** — a spectrum display below the video. When audio is off it shows the predicted pitch of each voice based on image features. When audio is on it shows the live frequency spectrum and waveform of what's actually playing.

**Settings sheet** — tap ⚙ (top right) to slide up the controls panel. It has two tabs:

- **Features** — live meters for brightness, saturation, edge density, motion amount, and luminance variance. Useful for understanding how the scene is being read.
- **Settings** — preset selector and individual sliders (see below).

### Header buttons

| Button | What it does |
|---|---|
| **Overlays** | Toggles the hue circles, scanline ticks, feature bars, and motion arrow on/off |
| **▶ Audio** | Starts audio playback through your browser. Tap once to enable — required due to browser autoplay restrictions. On iOS, make sure the hardware silent switch is off. |
| **⚙** | Opens the settings/features sheet |
| **●** | Green dot = connected to the Pi and receiving data |

---

## Presets

Presets are full sound profiles. Switch between them in the Settings tab or at startup with `--preset <name>`.

| Preset | Character | Best for |
|---|---|---|
| **default** | All three layers active at moderate levels, sine wave, 90 BPM | General use, good starting point |
| **ambient** | Warm drone with no glitch, triangle wave, slow 50 BPM pulses, wide pitch range | Background listening, calm scenes, music |
| **glitchy** | Harsh and reactive, glitch layer dominant, sawtooth wave, snappy 130 BPM | Fast movement, high-contrast scenes, noise |
| **percussive** | Tight punchy drums driven by image texture, minimal glitch, narrow pitch range | Scenes with strong repeating patterns (grids, tiles, fabric) |

Switching presets resets all sliders to the preset's values. You can then tweak individual sliders from that starting point.

---

## Settings

### Volume & Mix

| Setting | What it does |
|---|---|
| **Volume** | Master output level (0–1). If audio is clipping or too quiet, adjust here first. |
| **Blend Speed** | How quickly the camera reacts to changes. Low = slow dreamy drift. High = snappy and reactive. |
| **Warmth** | Low-pass filter cutoff (300–15000 Hz). Lower = warmer, bassier sound. Higher = brighter, harsher. Scenes with sharp edges automatically push this up regardless of setting. |

### Layer Mix

Three independent audio layers are mixed together. Each slider controls how loud that layer is in the final mix.

| Setting | What it does |
|---|---|
| **Glitch Mix** | Volume of the raw pixel waveform layer. Adds harshness and grit. |
| **Tonal Mix** | Volume of the pitched oscillators (hue → melody). The main "musical" layer. |
| **Rhythm Mix** | Volume of the percussion layer (texture → drums). |

### Tonal

| Setting | What it does |
|---|---|
| **Detune** | How far apart the two copies of each voice are tuned (in cents, 100 = 1 semitone). Higher = richer, more chorus-like. Lower = tighter, more pure. |
| **Waveform** | Shape of the oscillator. **Sine** is pure and smooth. **Triangle** is soft with a slight edge. **Sawtooth** is bright and buzzy. **Square** is hollow and reedy. |

### Motion

| Setting | What it does |
|---|---|
| **Pitch Bend** | How many semitones the tonal layer can shift when the camera moves left/right (0–24). 0 = no pitch bend. 12 = up to a full octave of bend. |
| **Motion Sens** | How sensitive the motion detection is (0.5–5). Low = only reacts to large movements. High = reacts to subtle changes. |

---

## How It Works

```
┌──────────┐    ┌──────────────┐    ┌──────────────────────────────┐    ┌─────────┐
│  Camera  │───>│   Analyzer   │───>│         Synthesizer          │───>│ Speaker │
│ (frame)  │    │ (image→data) │    │  Glitch + Tonal + Rhythm     │    │         │
└──────────┘    └──────────────┘    └──────────────────────────────┘    └─────────┘
```

Every ~200ms, the camera captures a frame. The analyzer extracts a set of numbers describing its musical qualities (brightness, dominant colors, texture patterns). The synthesizer turns those numbers into a 2-second audio clip. Three independent audio layers are generated and mixed together. This repeats in a loop, creating sound that evolves continuously as the scene changes.

---

### Background: Sound as Numbers

Before diving into the layers, a few concepts that underpin all of this:

**Waveforms**

Sound is air pressure oscillating very fast. A *waveform* is a graph of that pressure over time. When audio is stored in a computer, it's a long list of numbers — each one the pressure at a specific instant, sampled tens of thousands of times per second.

```
  Pressure
     1 │    ╭──╮        ╭──╮        ╭──╮
       │   ╱    ╲      ╱    ╲      ╱    ╲
     0 │──╯      ╲    ╱      ╲    ╱      ╲──▶ Time
       │          ╲  ╱        ╲  ╱
    -1 │           ╰╯          ╰╯
       └───────────────────────────────────
          A sine wave — the simplest, "purest" tone
```

Mulchy uses a *sample rate* of 44,100 Hz (44,100 measurements per second — the same as a CD). Each 2-second audio chunk is therefore 88,200 individual numbers.

**Frequency and Pitch**

The *frequency* of a waveform — how many times per second it completes one cycle — is what you hear as *pitch*. The unit is Hertz (Hz). More cycles per second = higher pitch. Middle A is 440 Hz. Every time you double the frequency, you jump up one *octave* (the same note, but higher).

```
  Low pitch (low Hz)             High pitch (high Hz)
  ──────────────────             ────────────────────
  ╭────╮      ╭────╮             ╭─╮ ╭─╮ ╭─╮ ╭─╮ ╭─╮
  │    │      │    │             │ │ │ │ │ │ │ │ │ │ │
──╯    ╰──────╯    ╰──           ╯ ╰─╯ ╰─╯ ╰─╯ ╰─╯ ╰─
  (slow cycles = deep bass)      (fast cycles = high treble)
```

**Oscillators and Waveform Shape**

An *oscillator* is something that generates a repeating waveform at a given frequency. In hardware synths this is a circuit; in software it's a math function running in a loop. The *shape* of the wave determines the tone color (called *timbre*) — same pitch, very different sound:

```
  Sine           Triangle       Sawtooth        Square
    ╭──╮            ╱╲            ╱│  ╱│        ┌──┐  ┌──
   ╱    ╲          ╱  ╲          ╱ │ ╱ │        │  │  │
──╯      ╰──   ──╱    ╲──   ───╱  │╱  │──   ───┘  └──┘  └─
                        ╲╱

  Pure/smooth   Soft/hollow  Buzzy/bright   Hollow/reedy
```

**Low-pass Filters**

A *low-pass filter* removes frequencies above a cutoff point, letting only lower (bass) frequencies through — the same idea as the "bass boost" or tone knob on a stereo. Frequencies below the cutoff pass through unchanged; above it, they're attenuated. The steeper the filter, the sharper the cutoff.

```
  Volume
    │
  1 │████████████████╲___________
    │                 ╲
  0 │                  ╲_________
    └──────────────────────────── Frequency (Hz)
                      ↑
                   cutoff
```

---

### The Feature Dictionary

Before generating any audio, `analyzer.py` reads the image and produces a *feature dictionary* — a set of numbers describing its musical qualities. The synthesizer never touches the image directly; it only sees these numbers.

| Feature | What it measures |
|---|---|
| `scanlines` | Raw pixel brightness for 8 evenly-spaced rows |
| `hue_centers` | The dominant colors, as positions on the color wheel (0–360°) |
| `hue_weights` | How much of the image each color occupies (sum to 1.0) |
| `texture_scores` | How repetitive each image quadrant is (0 = smooth, 1 = highly patterned) |
| `brightness` | Overall image lightness |
| `saturation` | How colorful (vs. grey) the image is |
| `motion_amount` | How much changed since the last frame (0–1) |
| `motion_cx / cy` | Where in the frame the motion is happening (-1 to +1) |

---

### Layer 1: Glitch — Raw Pixel Data as Sound

This is the founding idea of the project, inspired directly by the Wii RAM audio glitch.

**The core idea:** Your computer stores images as rows of pixels. Each pixel's brightness is a number from 0–255. If you rescale those numbers to the audio range (-1 to +1) and tell a speaker "play these as pressure values," you get a tone. The data wasn't *meant* to be audio, but it has structure — and structure in a waveform produces pitch.

```
  Image pixel row (brightness 0–255):
  │ 180 │ 200 │ 210 │ 195 │ 160 │ 130 │ 110 │ 115 │ 140 │ 175 │ 200 │...

  Rescaled to audio range (-1 to +1):
  │ 0.41│ 0.57│ 0.65│ 0.53│ 0.25│-0.02│-0.16│-0.10│ 0.10│ 0.37│ 0.57│...

  Played as a waveform:
     ╭──╮       ╭──╮
    ╱    ╲     ╱
  ─╯      ╲   ╱
            ╲╱
```

**Tiling:** A single pixel row is only 320 samples wide, but the audio buffer needs 88,200 samples. The row is *tiled* — repeated over and over to fill the buffer. The length of the row determines the pitch (shorter row = fewer samples per cycle = higher pitch). Mulchy reads 8 evenly-spaced rows from the image, generates a tiled waveform from each, and averages them together.

**Amplitude:** The `luminance_variance` (how much light contrast varies across the image) scales the volume — a flat, low-contrast image produces a quiet glitch tone; a high-contrast image produces a louder, more complex one.

**Motion pitch-shift:** When `motion_amount` is high, the playback speed of each row increases slightly (different amounts per row), shifting all pitches upward and creating a more chaotic sound.

**Low-pass filter:** Raw pixel data tends to be very harsh. A 4th-order Butterworth low-pass filter (`GLITCH_LOW_PASS_HZ`, default 6,000 Hz) removes the scratchiest high-frequency content, leaving a warmer, more musical glitch tone.

```
  Before filter:               After low-pass filter:
  ╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲             ╭──╮    ╭──╮
 ╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱╲╱            ╱    ╲  ╱    ╲
                              ╯      ╰╯      ╰
  harsh/spiky                  smoother, warmer
```

---

### Layer 2: Tonal — Color as Melody

This layer reads the image's dominant colors and plays them as pitched musical notes.

**Step 1 — Find dominant colors**

The analyzer converts the image to HSV color space (Hue, Saturation, Value — a way of describing color as an angle on a color wheel plus brightness and intensity). Near-grey pixels are ignored. The remaining hues are counted in a histogram with 72 bins (5° each). The top N peaks become the "voices" (default N = 3).

```
  Hue histogram — how many pixels of each color are in the frame:

  Count
  ████                       ← lots of orange (hue ≈ 30°)
   ██
    ██                       ← some blue-green (hue ≈ 180°)
     █   ████
      ██    █                ← a little violet (hue ≈ 270°)
       ████  ████
  0°  60° 120° 180° 240° 300° 360°
  Red  Yel  Grn  Cyn  Blu  Pur  Red
```

**Step 2 — Map hue to pitch**

The color wheel (0–360°) is divided into zones. Each zone maps to a degree of a *pentatonic scale* — a 5-note scale used across folk, blues, and pop music worldwide (on a piano: the black keys only). The pentatonic scale was chosen because any combination of its notes sounds consonant together, so no matter what colors appear in the frame, the result won't be a jarring chord.

```
  Hue →   0°      72°     144°    216°    288°     360°
          Red    Yellow   Green    Blue   Violet    Red
           │       │        │       │       │
  Note →   C       D        E       G       A       (C)
                  pentatonic scale degrees
```

Two octaves are covered, so notes spread across a wider pitch range and distinct hues sound clearly different from each other.

**Step 3 — Generate oscillators**

Each dominant hue becomes an oscillator at the mapped frequency. The waveform shape is set by `TONAL_WAVEFORM`. A slightly detuned copy (offset by `TONAL_DETUNE_CENTS`, default 8 cents — about 1/12 of a semitone) is layered on top at 40% volume. This technique, called *chorus* or *unison detuning*, makes the sound feel richer and less "digital" because the slight misalignment between the two copies creates slow beating and phase variation.

**Step 4 — Image properties modulate the sound**

| Image property | Effect on the tonal layer |
|---|---|
| **Saturation** (colorfulness) | Colorful image → all 3 voices active; near-grey → single drone |
| **Brightness** | Dark scene → lower octave (bass); bright scene → higher octave (treble) |
| **Horizontal motion** | Motion right → pitch bends up; left → bends down (up to ±7 semitones) |
| **Motion amount** | More motion → faster and deeper vibrato (LFO rate scales 2–8 Hz) |

**Vibrato** is a regular pitch wobble added by a *low-frequency oscillator* (LFO) — a very slow sine wave (2–8 Hz) that slightly modulates the playback phase of each voice. It's the same technique used by vocalists and string players to add expressiveness.

**ADSR Envelope**

Each note fades in over the first 20% of the chunk (the *attack*), sits at sustain level through the middle, then hands off to the crossfade for its tail. This prevents a sharp "click" at the note onset. In synthesizer terminology this is an *ADSR envelope* — a four-stage volume shape applied to every note:

```
  Volume
    1.0 │      ╭─╮ ← peak (end of attack)
        │     ╱   ╲___________ ← sustain level
        │    ╱                 ╲
    0.0 │───╱                   ╰──
        └──────────────────────────▶ Time
             A     D   Sustain   R
           Attack Decay        Release
        (fade in) (settle)    (fade out)
```

---

### Layer 3: Rhythm — Texture as Drums

This layer detects *repeating spatial patterns* in the image and translates them into percussion.

**What is texture repetition?**

A brick wall, a tiled floor, or a woven fabric all share something: a unit shape that repeats. A blurry bokeh background or a clear blue sky does not. Mulchy measures this using the **2D Fast Fourier Transform (FFT)**.

**What is the FFT?**

The Fourier Transform is a mathematical operation that decomposes any signal into a sum of sine waves, revealing which frequencies are present and how strong each one is. When applied to an image, it reveals *spatial* frequencies — how quickly patterns repeat across pixels. A repetitive texture produces sharp, concentrated peaks in the FFT output. A smooth or random image produces a broad, flat spectrum with no clear peaks.

```
  Repetitive texture (brick grid):       Smooth gradient:
  ┌───┬───┬───┬───┐                      ░░░▒▒▒▓▓▓███
  │   │   │   │   │
  ├───┼───┼───┼───┤    2D FFT:              2D FFT:
  │   │   │   │   │
  └───┴───┴───┴───┘

  · · · · · · · · ·                     · · · · · · · · ·
  · · █ · · · █ · ·  ← strong peaks     · · · · · · · · ·
  · · · · · · · · ·    = pattern found  · · · · ● · · · ·  ← only DC
  · · █ · · · █ · ·                     · · · · · · · · ·
  · · · · · · · · ·                     · · · · · · · · ·
```

The texture score is computed as: *energy in the top 5% of FFT peaks ÷ total energy*. A score above `RHYTHM_TEXTURE_THRESH` (default 0.25) triggers a drum hit.

**Quadrant → drum mapping**

The image is split into four quadrants. Each gets its own texture score that controls a different drum voice:

```
  ┌──────────────────┬──────────────────┐
  │   Top-Left       │   Top-Right      │
  │                  │                  │
  │  → KICK DRUM     │  → SNARE DRUM    │
  │    60 Hz tone    │  200 Hz tone +   │
  │    fast decay    │  50% white noise │
  │    fires on      │  fires on beats  │
  │    beat 1 & 3    │  2 & 4 (backbeat)│
  ├──────────────────┼──────────────────┤
  │   Bottom-Left    │   Bottom-Right   │
  │                  │                  │
  │  → HI-HAT ────────────────────────▶ │
  │    8000 Hz + 85% noise              │
  │    fires on every 8th note          │
  └──────────────────┴──────────────────┘
```

**What makes a drum sound like a drum?**

Each hit is synthesized by mixing a sine tone with random (white) noise in different proportions, then applying an exponential *decay envelope* — the volume drops to near-zero in about 80ms. The tone-to-noise ratio is what gives each drum its character:

```
  Kick:  ~100% tone  (60 Hz, decays in ~240ms)  →  "thud"
  Snare:    50/50    (200 Hz + white noise)      →  "crack"
  Hi-hat: ~15% tone  (8kHz + 85% noise)         →  "tss"
```

The exponential decay envelope (`e^(-t/τ)`) is used rather than a linear one because it more closely mimics how physical drums and membranes decay in the real world.

**Motion and tempo:** `motion_amount` scales the BPM up by up to 50% (`MOTION_TEMPO_SCALE`). A busy, active scene plays faster; a still scene locks to the base BPM.

---

### Frame Blending and Crossfading

Two separate smoothing mechanisms prevent jarring audio transitions as the scene changes:

**Frame blending** (in `camera.py`) combines the latest camera frame with the previous blended frame using an *exponential moving average* before the analyzer ever sees it:

```
  blended = BLEND_ALPHA × new_frame + (1 − BLEND_ALPHA) × previous_blended

  BLEND_ALPHA = 0.35 (default) → 35% new + 65% old  (gradual drift)
  BLEND_ALPHA = 0.8            → 80% new + 20% old  (fast response)
  BLEND_ALPHA = 0.1            → 10% new + 90% old  (very sluggish, dreamy)
```

This is the same exponential moving average used in financial charts and sensor smoothing everywhere.

**Audio crossfading** (in `synthesizer.py`) handles the join between consecutive audio chunks. The first 0.5 seconds of each new chunk fades in as the tail of the previous chunk fades out:

```
  Previous chunk:  ████████████████╲____
  New chunk:       ____╱████████████████
                       ├─crossfade─┤
                          0.5 sec
```

---

### The Final Mix

The three layers are combined at their configured levels, passed through a global low-pass filter (its cutoff frequency widens proportionally to `edge_density` — a scene with lots of sharp edges sounds brighter than a soft/blurry one), then peak-normalized so the loudest moment reaches 85% of maximum volume:

```
  Glitch ──× 0.30──╮
  Tonal  ──× 0.45──┼──▶ Low-pass ──▶ Normalize ──▶ × MASTER_VOLUME ──▶ 🔊
  Rhythm ──× 0.35──╯    filter        to 0.85
                        (cutoff scales
                         with edge density)
```

---

### Further Reading

- **Waveforms and synthesis basics** — [The Pudding: Let's Talk About Waveforms](https://pudding.cool/2018/02/waveforms/) — interactive visual explainer, no prior knowledge needed; covers sine/square/sawtooth and how they combine.
- **The Fourier Transform** — [3Blue1Brown: But what is the Fourier Transform?](https://www.youtube.com/watch?v=spUNpyF58BY) — widely regarded as the best visual intuition-builder for FFT.
- **Music theory and scales** — [musictheory.net Lessons](https://www.musictheory.net/lessons) — covers notes, intervals, scales, and chords from scratch.
- **ADSR envelopes and oscillators** — search "ADSR envelope synthesizer explained" on YouTube; Ableton's free learning resources and any introductory synthesis course cover these in depth.
- **The original Wii RAM glitch** — search "Wii RAM audio glitch" on YouTube to hear what this project was directly inspired by.

---

## Configuration (`config.py`)

All parameters are in one file. Key knobs:

| Parameter | What it does |
|---|---|
| `BLEND_ALPHA` | How fast frames change (0.1 = slow/smooth, 0.8 = fast/reactive) |
| `AUDIO_SECONDS` | Length of each generated audio chunk |
| `LAYER_*_LEVEL` | Mix level of each synthesis layer |
| `TONAL_WAVEFORM` | Oscillator shape: sine / triangle / sawtooth / square |
| `RHYTHM_BPM` | Base tempo |
| `GLITCH_LOW_PASS_HZ` | Cutoff to tame glitch harshness |

### Presets
Three presets are included: `ambient`, `glitchy`, `percussive`. Add your own to the `PRESETS` dict in `config.py`.

---

## Extending

### Adding GPIO buttons
1. Wire button to GPIO pin (see commented pin assignments in `config.py`)
2. Uncomment `_setup_gpio()` in `main.py`
3. Hook into the main loop: freeze frame, cycle presets, save snapshot

### Adding a display
1. Wire SSD1306 / small TFT
2. Uncomment display config in `config.py`
3. Call `_update_display(features)` in the main loop

---

## WiFi Management

The web dashboard includes a `/wifi` page for managing network connections without needing SSH or a keyboard.

Navigate to `http://mulchy.local:5000/wifi`. A password (stored in the `.env` file) is required when accessing over a regular network.

- **Scan** — lists available networks
- **Connect** — connects to a new network or a saved one; the Pi will drop its current connection and reconnect, so you'll need to switch your device to the new network afterward
- **Disconnect** — drops the current connection and activates the fallback AP (`mulchywifi`) within ~30 seconds

### Fallback AP

When the Pi has no client WiFi connection, a watchdog service (`mulchy-wifi.service`) activates a hotspot named `mulchywifi` (password in `.env` file) at `10.42.0.1`. Connect to it and navigate to `http://10.42.0.1:5000/wifi` to configure a new network.

The watchdog waits 3 poll cycles (~30 seconds) after losing a client connection before activating the AP, so briefly disconnecting to switch networks in the desktop UI won't trigger it prematurely.

The watchdog service is separate from the main app and is currently **disabled** pending resolution of an AP broadcast issue on the BCM43438 chip. Re-enable it with:

```bash
sudo systemctl enable --now mulchy-wifi.service
```

---

## Systemd (runs on boot)

```bash
sudo systemctl start mulchy     # start now
sudo systemctl stop mulchy      # stop
sudo systemctl status mulchy    # check status
journalctl -u mulchy -f         # live logs
```

---

## Pi Hardware Setup

Everything below was configured directly on the Pi and is **not in the repo**. If the OS is re-flashed, these steps need to be repeated.

### 1. Hostname

```bash
sudo hostnamectl set-hostname mulchy
# Makes the Pi reachable at mulchy.local on the local network
```

### 2. Python dependencies

```bash
pip install flask picamera2 numpy scipy
# Installed to /home/pi/.local/lib/python3.x/site-packages/
```

### 3. Main app service

`/etc/systemd/system/mulchy.service`:

```ini
[Unit]
Description=Mulchy - camera to soundscape
After=sound.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/mulchy
ExecStartPre=/bin/sleep 3
ExecStartPre=/usr/bin/amixer -c 1 sset PCM 100%
ExecStart=/usr/bin/python3 /home/pi/mulchy/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mulchy.service
```

### 4. WiFi watchdog service

`/etc/systemd/system/mulchy-wifi.service`:

```ini
[Unit]
Description=Mulchy WiFi Monitor
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=/home/pi/mulchy/wifi_monitor.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
# Enable only once the AP broadcast issue is resolved:
# sudo systemctl enable --now mulchy-wifi.service
```

### 5. Sudoers — nmcli and iwlist

The app runs as `pi` but needs to control NetworkManager and run WiFi scans.

`/etc/sudoers.d/mulchy-nmcli`:
```
pi ALL=(ALL) NOPASSWD: /usr/bin/nmcli
```

`/etc/sudoers.d/mulchy-iwlist`:
```
pi ALL=(ALL) NOPASSWD: /usr/sbin/iwlist wlan0 scan
```

```bash
sudo visudo -f /etc/sudoers.d/mulchy-nmcli
sudo visudo -f /etc/sudoers.d/mulchy-iwlist
```

### 6. NetworkManager AP profile

Creates the `mulchywifi` hotspot used when no client connection is available.

```bash
sudo nmcli con add type wifi ifname wlan0 con-name mulchy-ap ssid mulchywifi
sudo nmcli con modify mulchy-ap 802-11-wireless.mode ap
sudo nmcli con modify mulchy-ap 802-11-wireless.band bg
sudo nmcli con modify mulchy-ap 802-11-wireless.channel 6
sudo nmcli con modify mulchy-ap wifi-sec.key-mgmt wpa-psk
sudo nmcli con modify mulchy-ap wifi-sec.psk mulchypassword
sudo nmcli con modify mulchy-ap wifi-sec.proto rsn
sudo nmcli con modify mulchy-ap wifi-sec.pairwise ccmp
sudo nmcli con modify mulchy-ap wifi-sec.group ccmp
sudo nmcli con modify mulchy-ap 802-11-wireless-security.pmf disable
sudo nmcli con modify mulchy-ap ipv4.method shared
sudo nmcli con modify mulchy-ap ipv4.addresses 10.42.0.1/24
sudo nmcli con modify mulchy-ap connection.autoconnect no
```

### 7. WiFi country code

```bash
sudo raspi-config nonint do_wifi_country US
# Adjust country code as needed. Required for the AP to use valid channels.
```
