"""Mulchy — web dashboard (stripped).

The device has no user-facing controls in the field — it captures, processes,
plays. This dashboard exists for a) showing the operator what the camera
sees (live MJPEG), b) managing Wi-Fi without SSH access, and c) powering
the Pi off cleanly. Nothing else.

Audio output goes to the 3.5 mm jack via PipeWire — not streamed here."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import subprocess
import threading

import numpy as np
from flask import Flask, Response, jsonify, redirect, request, session
from PIL import Image

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)
app = Flask(__name__)

# Stable secret key derived from machine-id.
try:
    _mid = open("/etc/machine-id").read().strip()
    app.secret_key = hashlib.sha256((_mid + "mulchy").encode()).hexdigest()
except Exception:
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mulchy-fallback-secret")


# ── Shared frame state ───────────────────────────────────────────────────

_lock: threading.Lock = threading.Lock()
_frame_jpeg: bytes | None = None
_features: dict = {}

# Callbacks injected by main.py so the UI can flip the audio filter mode
# at runtime without web.py importing analyzer directly.
_filter_callbacks: dict = {"get": None, "set": None}


def register_controls(*, get_filter=None, set_filter=None) -> None:
    """Wire up runtime-control hooks. Called once from main.py at startup."""
    if get_filter is not None: _filter_callbacks["get"] = get_filter
    if set_filter is not None: _filter_callbacks["set"] = set_filter


def _encode_jpeg(frame: np.ndarray, quality: int = 70) -> bytes:
    """Encode an H×W×3 uint8 RGB array to JPEG bytes."""
    img = Image.fromarray(frame.astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def update(frame, features=None) -> None:
    """Called once per main-loop iteration. Latest frame is held for the
    MJPEG stream; features can be peeked at via /api/status."""
    global _frame_jpeg, _features
    if frame is None:
        return
    fj = _encode_jpeg(frame)
    with _lock:
        _frame_jpeg = fj
        if features is not None:
            _features = dict(features)


def run(host: str = "0.0.0.0", port: int = 5000) -> None:
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
        daemon=True,
    )
    t.start()
    log.info("Web dashboard → http://%s:%d", host, port)


# ── MJPEG stream ─────────────────────────────────────────────────────────

def _mjpeg_generator():
    while True:
        with _lock:
            j = _frame_jpeg
        if j is None:
            # No frame yet — wait a bit; tiny 1×1 jpeg placeholder is overkill.
            import time as _t
            _t.sleep(0.05)
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(j)).encode() + b"\r\n\r\n"
            + j + b"\r\n"
        )


@app.route("/stream/video")
def stream_video():
    return Response(_mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def status_route():
    """Small JSON peek at the latest features + current control state.
    Helpful for debugging without a screen — curl http://mulchy.local:5000/api/status."""
    with _lock:
        f = dict(_features)
    if _filter_callbacks["get"]:
        f["_filter"] = _filter_callbacks["get"]()
    return jsonify(f)


@app.route("/api/filter", methods=["POST"])
def filter_route():
    mode = (request.get_json(force=True) or {}).get("mode")
    if mode not in ("squiggle", "spiral") or _filter_callbacks["set"] is None:
        return jsonify({"error": "invalid mode"}), 400
    _filter_callbacks["set"](mode)
    return jsonify({"ok": True, "mode": mode})


# ── Index page (live feed + wifi link + shutdown) ────────────────────────

_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mulchy</title>
  <style>
    html, body { margin: 0; padding: 0; background: #111; color: #eee;
      font: 14px/1.4 ui-sans-serif, system-ui, sans-serif; }
    main { display: flex; flex-direction: column; align-items: center;
      gap: 12px; padding: 12px; box-sizing: border-box; min-height: 100vh; }
    h1 { margin: 0; font-weight: 500; font-size: 16px; letter-spacing: 0.04em;
      text-transform: uppercase; color: #c9a86a; }
    .stream { position: relative; width: 100%; max-width: 720px; background: #000;
      border: 1px solid #2a2a2a; border-radius: 4px; aspect-ratio: 4 / 3; }
    .stream img, .stream canvas { width: 100%; height: 100%; object-fit: contain;
      display: block; }
    .stream canvas { position: absolute; inset: 0; display: none; }
    .controls { display: flex; flex-direction: column; gap: 6px;
      align-items: center; width: 100%; max-width: 720px; }
    .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
      justify-content: center; }
    .row .lbl { font-size: 11px; color: #888; text-transform: uppercase;
      letter-spacing: 0.05em; margin-right: 4px; }
    nav { display: flex; gap: 12px; margin-top: 4px; }
    nav a, nav button, .row button { font: inherit; color: #eee; text-decoration: none;
      background: #1c1c1c; border: 1px solid #2a2a2a; border-radius: 3px;
      padding: 6px 12px; cursor: pointer; font-size: 13px; }
    nav a:hover, nav button:hover, .row button:hover {
      border-color: #c9a86a; color: #c9a86a; }
    nav button.active, .row button.active {
      border-color: #c9a86a; color: #c9a86a; }
    .danger { border-color: #5a2a2a !important; color: #c98080 !important; }
    .danger:hover { border-color: #c95050 !important; color: #c95050 !important; }
    #status { font-size: 11px; color: #888; font-variant-numeric: tabular-nums; }
  </style>
</head>
<body>
  <main>
    <h1>mulchy</h1>
    <div class="stream">
      <img id="live" src="/stream/video" alt="live camera feed">
      <canvas id="squiggle"></canvas>
    </div>
    <div class="controls">
      <div class="row">
        <span class="lbl">view</span>
        <button class="viewBtn active" data-view="camera">camera</button>
        <button class="viewBtn" data-view="squiggle">squiggle</button>
        <button class="viewBtn" data-view="spiral">spiral</button>
      </div>
    </div>
    <nav>
      <a href="/wifi">wifi</a>
      <button id="shutdown" class="danger" type="button">shutdown</button>
    </nav>
    <div id="status"></div>
  </main>
  <script>
    // ── Filter visualizations ──────────────────────────────────────────
    // Two filter strategies mirror the Python analyzer: squiggle (overlay
    // on the live feed, highlights the one row that drives all six
    // voices) and spiral (replaces the feed entirely, draws the v2-style
    // colored Archimedean spiral with the longest contiguous bin-run —
    // the polyline that actually becomes the audio — rendered bright;
    // every other arc is dimmed). The camera <img> keeps streaming behind
    // the canvas so getImageData has fresh pixels even when spiral mode
    // is showing.

    // Squiggle constants. SQ_ROWS matches analyzer.py so the highlight is
    // the same row the audio uses; VIS_FREQ/AMP are larger than the
    // analyzer's so darkness variation reads at a glance.
    const SQ_ROWS = 100;
    const SQ_VIS_FREQ = 14;
    const SQ_VIS_AMP = 2.4;
    const SQ_SAMPLES = 480;

    // Spiral constants — must mirror SPIRAL_* in analyzer.py exactly so
    // the bright polyline drawn here is the same arc the audio uses.
    const SP_TURNS = 60;
    const SP_SAMPLES_PER_TURN = 200;
    const SP_LEVELS = 8;
    const SP_MARGIN = 0.02;
    const SP_INVERT = true;
    const SP_STROKE_MIN = 0.2;
    const SP_STROKE_MAX = 1.5;

    const liveImg = document.getElementById('live');
    const sqCanvas = document.getElementById('squiggle');
    const sqCtx = sqCanvas.getContext('2d', { willReadFrequently: true });

    let viewMode = 'camera';   // 'camera' | 'squiggle' | 'spiral'
    let renderTimer = null;

    function drawSquiggles(ctx, frameData, w, h) {
      const rowSpacing = h / SQ_ROWS;
      const halfBand = rowSpacing / 2;
      const data = frameData.data;

      // Per-row darkness sum (for picking the global longest row) AND
      // per-row y-trajectory (for drawing). Storing trajectories lets us
      // draw all rows lightly then redraw the chosen one bright without
      // recomputing.
      const ampSums = new Float32Array(SQ_ROWS);
      const rowYs = new Array(SQ_ROWS);
      for (let i = 0; i < SQ_ROWS; i++) {
        const yCenter = (i + 0.5) * rowSpacing;
        const top = Math.max(0, Math.floor(yCenter - halfBand));
        const bot = Math.min(h, Math.floor(yCenter + halfBand + 1));
        const bandRows = Math.max(1, bot - top);
        const ys = new Float32Array(SQ_SAMPLES);
        let darkRowSum = 0;
        for (let s = 0; s < SQ_SAMPLES; s++) {
          const x = (s / (SQ_SAMPLES - 1)) * w;
          const xi = Math.min(w - 1, Math.floor(x));
          let darkSum = 0;
          for (let y = top; y < bot; y++) {
            const idx = (y * w + xi) * 4;
            const luma = (0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2]) / 255;
            darkSum += 1 - luma;
          }
          const dark = darkSum / bandRows;
          darkRowSum += dark;
          const amp = dark * SQ_VIS_AMP * halfBand;
          const ph = 2 * Math.PI * SQ_VIS_FREQ * x / w;
          ys[s] = yCenter + Math.sin(ph) * amp;
        }
        ampSums[i] = darkRowSum;
        rowYs[i] = ys;
      }

      // Global winner — the one row the analyzer picks and splits into
      // six voice chunks.
      let bestAmp = -1, bestRow = -1;
      for (let i = 0; i < SQ_ROWS; i++) {
        if (ampSums[i] > bestAmp) { bestAmp = ampSums[i]; bestRow = i; }
      }

      // All non-chosen rows — soft gold so the chosen row stands out.
      ctx.strokeStyle = 'rgba(201, 168, 106, 0.35)';
      ctx.lineWidth = 1;
      for (let i = 0; i < SQ_ROWS; i++) {
        if (i === bestRow) continue;
        const ys = rowYs[i];
        ctx.beginPath();
        for (let s = 0; s < SQ_SAMPLES; s++) {
          const x = (s / (SQ_SAMPLES - 1)) * w;
          if (s === 0) ctx.moveTo(x, ys[s]); else ctx.lineTo(x, ys[s]);
        }
        ctx.stroke();
      }

      // Chosen row — the one row driving all six voices — bright + thicker.
      if (bestRow >= 0) {
        ctx.strokeStyle = 'rgba(255, 220, 130, 1.0)';
        ctx.lineWidth = 1.75;
        const ys = rowYs[bestRow];
        ctx.beginPath();
        for (let s = 0; s < SQ_SAMPLES; s++) {
          const x = (s / (SQ_SAMPLES - 1)) * w;
          if (s === 0) ctx.moveTo(x, ys[s]); else ctx.lineTo(x, ys[s]);
        }
        ctx.stroke();
      }
    }

    function drawSpiral(ctx, frameData, w, h) {
      // Black background — spiral replaces the live feed entirely.
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, w, h);

      const cx = w / 2, cy = h / 2;
      const maxR = Math.max(1, Math.min(w, h) / 2 * (1 - SP_MARGIN));
      const n = SP_TURNS * SP_SAMPLES_PER_TURN;
      const data = frameData.data;

      // Walk the spiral once, collecting (xi, yi, bin) per sample. Then
      // segment into runs of the same bin, exactly like analyzer.py's
      // _spiral_polyline_for_audio so the "longest run" identified here
      // is the same arc the Python side feeds to the audio engine.
      const xs = new Float32Array(n);
      const ys = new Float32Array(n);
      const bins = new Int8Array(n);
      const levelsM1 = SP_LEVELS - 1;
      for (let i = 0; i < n; i++) {
        const t = i / (n - 1);
        const theta = SP_TURNS * 2 * Math.PI * t;
        const r = maxR * t;
        const x = cx + r * Math.cos(theta);
        const y = cy + r * Math.sin(theta);
        xs[i] = x; ys[i] = y;
        const xi = Math.min(w - 1, Math.max(0, x | 0));
        const yi = Math.min(h - 1, Math.max(0, y | 0));
        const idx = (yi * w + xi) * 4;
        const luma = (0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2]) / 255;
        const dark = SP_INVERT ? luma : 1 - luma;
        let b = (dark * SP_LEVELS) | 0;
        if (b < 0) b = 0; else if (b > levelsM1) b = levelsM1;
        bins[i] = b;
      }

      // Run-length: each run is [start, end) of one bin. Track the longest.
      const starts = [0];
      const ends = [];
      for (let i = 1; i < n; i++) {
        if (bins[i] !== bins[i - 1]) {
          ends.push(i);
          starts.push(i);
        }
      }
      ends.push(n);
      let longestIdx = 0, longestLen = ends[0] - starts[0];
      for (let k = 1; k < starts.length; k++) {
        const len = ends[k] - starts[k];
        if (len > longestLen) { longestLen = len; longestIdx = k; }
      }
      const longestStart = starts[longestIdx];
      const longestEnd = ends[longestIdx];

      // Draw each run as a polyline. Stroke width derived from bin (matches
      // v2). Color sampled from the middle of the run. Non-longest runs
      // render dimmed; the longest renders at full brightness.
      const widthSpan = SP_STROKE_MAX - SP_STROKE_MIN;
      for (let k = 0; k < starts.length; k++) {
        const s = starts[k], e = ends[k];
        if (e - s < 2) continue;
        const bin = bins[s];
        const stroke = SP_STROKE_MIN + (bin / levelsM1) * widthSpan;
        const mid = (s + e) >> 1;
        const xi = Math.min(w - 1, Math.max(0, xs[mid] | 0));
        const yi = Math.min(h - 1, Math.max(0, ys[mid] | 0));
        const di = (yi * w + xi) * 4;
        const r = data[di], g = data[di + 1], bl = data[di + 2];
        const isLongest = (k === longestIdx);
        const alpha = isLongest ? 1.0 : 0.35;
        ctx.strokeStyle = `rgba(${r},${g},${bl},${alpha})`;
        ctx.lineWidth = isLongest ? Math.max(1.2, stroke * 1.4) : stroke;
        ctx.beginPath();
        ctx.moveTo(xs[s], ys[s]);
        // Overlap one point with the next run so the spiral reads continuous
        // (matches v2's _emit using stop + 1).
        const drawEnd = Math.min(n, e + 1);
        for (let i = s + 1; i < drawEnd; i++) ctx.lineTo(xs[i], ys[i]);
        ctx.stroke();
      }
      // Subtle band labelling the longest arc — pulled out of the loop so
      // it always lands on top of any overlapping faded segments.
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.18)';
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      ctx.moveTo(xs[longestStart], ys[longestStart]);
      for (let i = longestStart + 1; i < longestEnd; i++) ctx.lineTo(xs[i], ys[i]);
      ctx.stroke();
    }

    function renderFrame() {
      const w = liveImg.naturalWidth, h = liveImg.naturalHeight;
      if (w && h) {
        if (sqCanvas.width !== w) sqCanvas.width = w;
        if (sqCanvas.height !== h) sqCanvas.height = h;
        // Always pull a fresh pixel snapshot from the live <img>; both
        // visualizations read from it.
        sqCtx.drawImage(liveImg, 0, 0, w, h);
        const data = sqCtx.getImageData(0, 0, w, h);
        if (viewMode === 'squiggle') {
          sqCtx.fillStyle = 'rgba(0, 0, 0, 0.6)';
          sqCtx.fillRect(0, 0, w, h);
          drawSquiggles(sqCtx, data, w, h);
        } else if (viewMode === 'spiral') {
          drawSpiral(sqCtx, data, w, h);
        }
      }
      renderTimer = setTimeout(renderFrame, 200);
    }

    function setView(mode) {
      viewMode = mode;
      sqCanvas.style.display = (mode === 'camera') ? 'none' : 'block';
      document.querySelectorAll('.viewBtn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === mode);
      });
      // Audio filter follows the view: camera + squiggle both use
      // squiggle on the audio side, spiral uses spiral.
      const audioMode = (mode === 'spiral') ? 'spiral' : 'squiggle';
      fetch('/api/filter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: audioMode }),
      }).catch(() => {});
      if (mode !== 'camera' && renderTimer == null) renderFrame();
      if (mode === 'camera' && renderTimer != null) {
        clearTimeout(renderTimer); renderTimer = null;
      }
    }

    document.querySelectorAll('.viewBtn').forEach(b => {
      b.addEventListener('click', () => setView(b.dataset.view));
    });

    document.getElementById('shutdown').addEventListener('click', async () => {
      if (!confirm('Power off the Pi?')) return;
      try {
        await fetch('/api/shutdown', { method: 'POST' });
        document.getElementById('status').textContent = 'Powering off…';
      } catch (e) {
        document.getElementById('status').textContent = 'Error: ' + e.message;
      }
    });
    async function poll() {
      try {
        const r = await fetch('/api/status');
        if (r.ok) {
          const f = await r.json();
          if (Object.keys(f).length) {
            document.getElementById('status').textContent =
              `bright ${(f.brightness ?? 0).toFixed(2)}  ` +
              `sat ${(f.saturation ?? 0).toFixed(2)}  ` +
              `edges ${(f.edge_density ?? 0).toFixed(2)}  ` +
              `hue ${(f.hue ?? 0).toFixed(2)}  ` +
              `motion ${(f.motion ?? 0).toFixed(2)}`;
          }
        }
      } catch (_e) {}
      setTimeout(poll, 1000);
    }
    poll();
  </script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(_INDEX_HTML, mimetype="text/html")


@app.route("/api/shutdown", methods=["POST"])
def shutdown_route():
    """Powers the Pi off. Requires the sudoers drop-in from scripts/."""
    try:
        subprocess.Popen(["sudo", "/sbin/shutdown", "-h", "now"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Wi-Fi management (unchanged from the connectivity skeleton) ──────────
#
# Field deployment essential: lets the operator configure Wi-Fi from a phone
# when the Pi is hosting its fallback AP, no SSH or keyboard needed.

_AP_CON    = "mulchy-ap"
_AP_SUBNET = "10.42.0."
_WIFI_PASS = os.environ.get("WIFI_PASSWORD", "")
_FLAG_FILE = "/tmp/mulchy-connecting"


def _set_flag() -> None:
    try:
        open(_FLAG_FILE, "w").close()
    except Exception:
        pass


def _clear_flag() -> None:
    try:
        os.unlink(_FLAG_FILE)
    except FileNotFoundError:
        pass


def _nmcli(*args, timeout: int = 15):
    try:
        r = subprocess.run(
            ["sudo", "nmcli"] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def _active_client_con():
    out, _, _ = _nmcli("-t", "-f", "NAME,TYPE,STATE", "con", "show", "--active")
    for line in out.splitlines():
        parts = line.split(":")
        if (len(parts) >= 3 and parts[1] == "802-11-wireless"
                and parts[2] == "activated" and parts[0] != _AP_CON):
            return parts[0]
    return None


def _saved_networks():
    out, _, rc = _nmcli("-t", "-f", "NAME,TYPE", "con", "show")
    if rc != 0:
        return []
    active = _active_client_con()
    result = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless" and parts[0] != _AP_CON:
            result.append({"name": parts[0], "active": parts[0] == active})
    return result


def _scan_networks():
    try:
        r = subprocess.run(
            ["sudo", "/usr/sbin/iwlist", "wlan0", "scan"],
            capture_output=True, text=True, timeout=20,
        )
        raw = r.stdout
    except Exception as e:
        log.error("WiFi scan error: %s", e)
        return []
    return _parse_iwlist(raw)


def _parse_iwlist(raw: str) -> list:
    best: dict = {}
    cur: dict = {}

    def _commit():
        ssid = cur.get("ssid", "")
        if ssid and (ssid not in best or cur["signal"] > best[ssid]["signal"]):
            best[ssid] = dict(cur)

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("Cell "):
            _commit()
            cur = {"ssid": "", "signal": 0, "security": "--", "open": True}
        elif line.startswith("ESSID:"):
            cur["ssid"] = line[6:].strip().strip('"')
        elif line.startswith("Quality="):
            m = re.search(r"Signal level=(-?\d+)", line)
            if m:
                dbm = int(m.group(1))
                cur["signal"] = max(0, min(100, 2 * (dbm + 100)))
        elif line == "Encryption key:off":
            cur["open"] = True
            cur["security"] = "--"
        elif line == "Encryption key:on":
            cur["open"] = False
            cur["security"] = "WPA2"
        elif "WPA2" in line:
            cur["security"] = "WPA2"
        elif "WPA" in line and cur.get("security") != "WPA2":
            cur["security"] = "WPA"
        elif line.startswith("IE: WEP"):
            cur["security"] = "WEP"
    _commit()
    return sorted(best.values(), key=lambda x: -x["signal"])


def _connect_worker(ssid, password=None, con_name=None):
    import time
    try:
        time.sleep(1.5)
        if con_name:
            _nmcli("con", "up", con_name, timeout=30)
        else:
            cmd = ["dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            _nmcli(*cmd, timeout=30)
    finally:
        _clear_flag()


def _wifi_authed() -> bool:
    return bool(_WIFI_PASS) and bool(session.get("wifi_authed"))


_WIFI_AUTH_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>mulchy wifi</title>
<style>
body{background:#111;color:#eee;font:14px ui-sans-serif,system-ui,sans-serif;
margin:0;padding:24px;display:flex;justify-content:center}
form{display:flex;flex-direction:column;gap:12px;max-width:300px;width:100%}
input{font:inherit;padding:8px;background:#1c1c1c;color:#eee;
border:1px solid #2a2a2a;border-radius:3px}
button{font:inherit;padding:8px;background:#1c1c1c;color:#c9a86a;
border:1px solid #2a2a2a;border-radius:3px;cursor:pointer}
.err{color:#c98080;display:__ERR__}
</style></head><body>
<form method="post" action="/wifi/auth">
<div class="err">Wrong password.</div>
<input type="password" name="password" placeholder="wifi page password" autofocus>
<button type="submit">unlock</button>
</form></body></html>"""

_WIFI_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>mulchy wifi</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{background:#111;color:#eee;font:14px ui-sans-serif,system-ui,sans-serif;
margin:0;padding:16px;max-width:600px}
h2{font-size:14px;letter-spacing:.04em;text-transform:uppercase;color:#c9a86a;
margin:24px 0 8px}
ul{list-style:none;padding:0;margin:0}
li{display:flex;justify-content:space-between;align-items:center;
padding:8px 0;border-bottom:1px solid #2a2a2a}
button{font:inherit;background:#1c1c1c;color:#eee;border:1px solid #2a2a2a;
border-radius:3px;padding:5px 10px;cursor:pointer}
button:hover{border-color:#c9a86a;color:#c9a86a}
.del{color:#c98080;border-color:#5a2a2a}
.signal{color:#888;font-size:11px;margin-left:8px;font-variant-numeric:tabular-nums}
#status{color:#888;font-size:11px;margin-top:12px}
.active{color:#c9a86a}
a{color:#c9a86a;text-decoration:none}
</style></head>
<body>
<a href="/">← back</a>
<h2>status</h2>
<div id="status">loading…</div>
<h2>saved networks</h2>
<ul id="saved"></ul>
<h2>nearby networks</h2>
<button id="rescan">rescan</button>
<ul id="scan"></ul>
<script>
async function refresh() {
  const s = await (await fetch('/wifi/status')).json();
  document.getElementById('status').innerHTML = s.connected
    ? `connected: <span class="active">${s.connected}</span>`
    : (s.ap ? 'fallback AP <span class="active">mulchywifi</span> active' : 'no connection');
  const saved = await (await fetch('/wifi/saved')).json();
  document.getElementById('saved').innerHTML = saved.map(n =>
    `<li>${n.active ? '<span class="active">●</span> ' : ''}${n.name}
       <span>
         <button onclick="conn(null,'${n.name}')">connect</button>
         <button class="del" onclick="del('${n.name}')">delete</button>
       </span></li>`).join('') || '<li>(none)</li>';
}
async function rescan() {
  document.getElementById('scan').innerHTML = '<li>scanning…</li>';
  const list = await (await fetch('/wifi/scan')).json();
  document.getElementById('scan').innerHTML = list.map(n =>
    `<li>${n.ssid} <span class="signal">${n.signal}%</span>
       <button onclick="connectPrompt('${n.ssid}', ${n.open})">connect</button></li>`).join('');
}
async function connectPrompt(ssid, open) {
  const password = open ? null : prompt(`password for ${ssid}`);
  if (!open && password === null) return;
  await conn(ssid, null, password);
}
async function conn(ssid, name, password) {
  await fetch('/wifi/connect', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ssid, name, password }) });
  document.getElementById('status').textContent = 'connecting… (mulchy will drop offline briefly)';
}
async function del(name) {
  if (!confirm('delete ' + name + '?')) return;
  await fetch('/wifi/remove', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }) });
  refresh();
}
document.getElementById('rescan').addEventListener('click', rescan);
refresh();
setInterval(refresh, 5000);
</script></body></html>"""


@app.route("/wifi")
def wifi_page():
    if not _wifi_authed():
        err = "1" if request.args.get("err") else ""
        return Response(_WIFI_AUTH_HTML.replace("__ERR__", "block" if err else "none"),
                        mimetype="text/html")
    return Response(_WIFI_HTML, mimetype="text/html")


@app.route("/wifi/auth", methods=["POST"])
def wifi_auth():
    if request.form.get("password") == _WIFI_PASS:
        session["wifi_authed"] = True
        return redirect("/wifi")
    return redirect("/wifi?err=1")


@app.route("/wifi/status")
def wifi_status():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    con = _active_client_con()
    ap_out, _, _ = _nmcli("-t", "-f", "NAME,STATE", "con", "show", "--active")
    ap_up = any(f"{_AP_CON}:activated" in line for line in ap_out.splitlines())
    return jsonify({
        "connected":  con,
        "ap":         ap_up,
        "connecting": os.path.exists(_FLAG_FILE),
    })


@app.route("/wifi/saved")
def wifi_saved_route():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_saved_networks())


@app.route("/wifi/scan")
def wifi_scan_route():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_scan_networks())


@app.route("/wifi/connect", methods=["POST"])
def wifi_connect_route():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip() or None
    con_name = (data.get("name") or "").strip() or None
    if not ssid and not con_name:
        return jsonify({"error": "ssid or name required"}), 400
    _set_flag()
    threading.Thread(target=_connect_worker, args=(ssid, password, con_name),
                     daemon=True).start()
    return jsonify({"ok": True, "ssid": ssid or con_name})


@app.route("/wifi/remove", methods=["POST"])
def wifi_remove_route():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name or name == _AP_CON:
        return jsonify({"error": "invalid name"}), 400
    _, err, rc = _nmcli("con", "delete", name, timeout=10)
    return jsonify({"ok": rc == 0, "error": err.strip() if rc != 0 else None})
