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
    """Small JSON peek at the latest features. Helpful for debugging without
    a screen — curl http://mulchy.local:5000/api/status."""
    with _lock:
        f = dict(_features)
    return jsonify(f)


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
    .stream { width: 100%; max-width: 720px; background: #000;
      border: 1px solid #2a2a2a; border-radius: 4px; aspect-ratio: 4 / 3; }
    .stream img { width: 100%; height: 100%; object-fit: contain; display: block; }
    nav { display: flex; gap: 12px; }
    nav a, nav button { font: inherit; color: #eee; text-decoration: none;
      background: #1c1c1c; border: 1px solid #2a2a2a; border-radius: 3px;
      padding: 8px 14px; cursor: pointer; }
    nav a:hover, nav button:hover { border-color: #c9a86a; color: #c9a86a; }
    .danger { border-color: #5a2a2a !important; color: #c98080 !important; }
    .danger:hover { border-color: #c95050 !important; color: #c95050 !important; }
    #status { font-size: 11px; color: #888; font-variant-numeric: tabular-nums; }
  </style>
</head>
<body>
  <main>
    <h1>mulchy</h1>
    <div class="stream"><img src="/stream/video" alt="live camera feed"></div>
    <nav>
      <a href="/wifi">wifi</a>
      <button id="shutdown" class="danger" type="button">shutdown</button>
    </nav>
    <div id="status"></div>
  </main>
  <script>
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
