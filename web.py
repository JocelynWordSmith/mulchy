"""
Mulchy - Web Dashboard
Lightweight Flask server for monitoring and control.
Access at http://<pi-ip>:5000
"""

import hashlib
import io
import json
import os
import subprocess
import base64
import threading
import logging
import numpy as np
from flask import Flask, Response, jsonify, redirect, request, session

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; fall back to environment variables only

import config as cfg

log = logging.getLogger(__name__)
app = Flask(__name__)

# Stable secret key derived from machine-id — persists across restarts without a keyfile
try:
    _mid = open("/etc/machine-id").read().strip()
    app.secret_key = hashlib.sha256((_mid + "mulchy").encode()).hexdigest()
except Exception:
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mulchy-fallback-secret")

# ── Shared state ───────────────────────────────────────────────────────────────

_cond           = threading.Condition(threading.Lock())
_frame_jpeg     = None   # clean blended frame, no overlays
_features       = {}
_audio_b64      = None   # base64 int16 PCM at 22050 Hz
_seq            = 0
_audio_seq      = 0
_client_count   = 0      # active SSE clients; audio only encoded when > 0
_active_preset  = "ambient"  # tracks which preset is loaded
_custom_presets  = {}   # user-cloned presets; persisted to state.json
_preset_settings = {}   # per-preset slider overrides; keyed by preset name
_STATE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def update(raw_frame, blended_frame, features, audio_chunk=None):
    """Call once per main-loop iteration."""
    global _frame_jpeg, _features, _audio_b64, _seq, _audio_seq

    fj = _encode_jpeg(blended_frame) if _client_count > 0 else None

    ab = None
    if audio_chunk is not None and _client_count > 0 and (_seq - _audio_seq) >= 3:
        ds  = audio_chunk[::2]         # 44100 → 22050 Hz
        raw = (np.clip(ds, -1, 1) * 32767).astype(np.int16).tobytes()
        ab  = base64.b64encode(raw).decode()
        _audio_seq = _seq

    with _cond:
        _frame_jpeg = fj
        _features   = _serialise(features)
        if ab is not None:
            _audio_b64 = ab
        _seq += 1
        _cond.notify_all()


def run(host="0.0.0.0", port=5000, preset="ambient"):
    global _active_preset
    _active_preset = preset
    _load_state()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
        daemon=True,
    )
    t.start()
    log.info("Web dashboard → http://%s:%d", host, port)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(_HTML, mimetype="text/html")


@app.route("/stream/video")
def stream_video():
    return Response(_mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/events")
def events():
    return Response(
        _sse_gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "GET":
        return jsonify({k: getattr(cfg, k) for k, *_ in _SETTINGS_META})
    data = request.get_json(force=True)
    for name, typ, lo, hi in _SETTINGS_META:
        if name not in data:
            continue
        val = typ(data[name])
        if lo is not None:
            val = max(lo, min(hi, val))
        setattr(cfg, name, val)
    _save_state()
    return jsonify({"ok": True})


@app.route("/api/preset", methods=["POST"])
def preset():
    global _active_preset
    data = request.get_json(force=True)
    name = data.get("preset", "")
    if name not in cfg.PRESETS:
        return jsonify({"error": f"unknown preset: {name}"}), 400
    cfg.load_preset(name)
    # Re-apply any saved overrides for this preset on top of factory defaults
    for k, v in _preset_settings.get(name, {}).items():
        if hasattr(cfg, k):
            try:
                setattr(cfg, k, type(getattr(cfg, k))(v))
            except Exception:
                pass
    _active_preset = name
    _save_state()
    return jsonify({
        "ok": True,
        "preset": name,
        "settings": {k: getattr(cfg, k) for k, *_ in _SETTINGS_META},
    })


@app.route("/api/preset/reset", methods=["POST"])
def preset_reset():
    """Discard saved overrides for the active preset, restore factory defaults."""
    _preset_settings.pop(_active_preset, None)
    cfg.load_preset(_active_preset)
    _save_state()
    return jsonify({
        "ok": True,
        "preset": _active_preset,
        "settings": {k: getattr(cfg, k) for k, *_ in _SETTINGS_META},
    })


@app.route("/api/preset", methods=["GET"])
def preset_get():
    return jsonify({
        "preset": _active_preset,
        "presets": list(cfg.PRESETS.keys()),
        "custom": list(_custom_presets.keys()),
    })


@app.route("/api/preset/clone", methods=["POST"])
def preset_clone():
    global _active_preset, _custom_presets
    data = request.get_json(force=True)
    source = data.get("source", _active_preset)
    if source not in cfg.PRESETS:
        return jsonify({"error": "unknown source preset"}), 400
    base = source.replace("_", " ").title()
    n = 2
    while f"{base} {n}" in cfg.PRESETS:
        n += 1
    name = f"{base} {n}"
    clone_vals = cfg.PRESETS[source].copy()
    for k, *_ in _SETTINGS_META:
        if k in clone_vals:
            clone_vals[k] = getattr(cfg, k)
    _custom_presets[name] = clone_vals
    cfg.PRESETS[name] = clone_vals
    cfg.load_preset(name)
    _active_preset = name
    _save_state()
    return jsonify({
        "ok": True,
        "name": name,
        "presets": list(cfg.PRESETS.keys()),
        "custom": list(_custom_presets.keys()),
        "settings": {k: getattr(cfg, k) for k, *_ in _SETTINGS_META},
    })


# ── Generators ─────────────────────────────────────────────────────────────────

def _mjpeg():
    last = -1
    while True:
        with _cond:
            _cond.wait_for(lambda: _seq != last, timeout=1.5)
            last  = _seq
            jpeg  = _frame_jpeg
        if not jpeg:
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"


def _sse_gen():
    global _client_count
    _client_count += 1
    last       = -1
    last_audio = None
    try:
        while True:
            with _cond:
                _cond.wait_for(lambda: _seq != last, timeout=1.5)
                last  = _seq
                feats = dict(_features)
                audio = _audio_b64
            yield f"event: features\ndata: {json.dumps(feats)}\n\n"
            if audio and audio != last_audio:
                last_audio = audio
                yield f"event: audio\ndata: {json.dumps({'pcm': audio, 'sr': 22050})}\n\n"
    except GeneratorExit:
        pass
    finally:
        _client_count -= 1


# ── Helpers ────────────────────────────────────────────────────────────────────

def _encode_jpeg(frame, quality=70):
    if frame is None:
        return None
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return None


def _serialise(features):
    out = {}
    for k, v in features.items():
        if k == "scanlines":
            continue
        if isinstance(v, float):
            out[k] = round(v, 4)
        elif isinstance(v, (list, tuple)):
            out[k] = [round(x, 4) if isinstance(x, float) else x for x in v]
        else:
            out[k] = v
    return out


_SETTINGS_META = [
    ("MASTER_VOLUME",          float, 0.0,  1.0),
    ("BLEND_ALPHA",            float, 0.05, 1.0),
    ("MIX_LOWPASS_HZ",         int,   300,  15000),
    ("LAYER_GLITCH_LEVEL",     float, 0.0,  1.0),
    ("LAYER_TONAL_LEVEL",      float, 0.0,  1.0),
    ("LAYER_RHYTHM_LEVEL",     float, 0.0,  1.0),
    ("TONAL_DETUNE_CENTS",     int,   0,    50),
    ("MOTION_PITCH_SEMITONES", int,   0,    24),
    ("MOTION_SENSITIVITY",     float, 0.5,  5.0),
    ("TONAL_WAVEFORM",         str,   None, None),
]


def _load_state():
    """Load persisted preset, custom presets, and per-preset overrides from disk."""
    global _active_preset, _custom_presets, _preset_settings
    try:
        with open(_STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    _custom_presets  = state.get("custom_presets", {})
    _preset_settings = state.get("preset_settings", {})
    cfg.PRESETS.update(_custom_presets)
    name = state.get("active_preset", _active_preset)
    if name in cfg.PRESETS:
        cfg.load_preset(name)
        _active_preset = name
    # Re-apply any saved overrides for the restored preset
    for k, v in _preset_settings.get(_active_preset, {}).items():
        if hasattr(cfg, k):
            try:
                setattr(cfg, k, type(getattr(cfg, k))(v))
            except Exception:
                pass
    log.info("State restored: preset=%s, %d custom preset(s)", _active_preset, len(_custom_presets))


def _save_state():
    """Persist active preset, custom presets, and per-preset slider overrides to disk."""
    # Snapshot current slider values as overrides for the active preset
    _preset_settings[_active_preset] = {k: getattr(cfg, k) for k, *_ in _SETTINGS_META}
    state = {
        "active_preset": _active_preset,
        "custom_presets": _custom_presets,
        "preset_settings": _preset_settings,
    }
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error("Failed to save state: %s", e)


# ── WiFi management ───────────────────────────────────────────────────────────

_AP_CON    = "mulchy-ap"
_AP_SUBNET = "10.42.0."
_WIFI_PASS = os.environ.get("WIFI_PASSWORD", "")
# /tmp is tmpfs — this file is wiped on every reboot, so a crash can never
# leave the Pi stuck between states. The finally: block handles runtime cleanup.
_FLAG_FILE = "/tmp/mulchy-connecting"


def _set_flag():
    try:
        open(_FLAG_FILE, "w").close()
    except Exception:
        pass


def _clear_flag():
    try:
        os.unlink(_FLAG_FILE)
    except FileNotFoundError:
        pass


def _nmcli(*args, timeout=15):
    # sudo required — service runs as pi but nmcli needs root to modify
    # system connections. NOPASSWD rule in /etc/sudoers.d/mulchy-nmcli covers this.
    try:
        r = subprocess.run(["sudo", "nmcli"] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def _active_client_con():
    """Return name of the active non-AP wifi connection, or None."""
    out, _, _ = _nmcli("-t", "-f", "NAME,TYPE,STATE", "con", "show", "--active")
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] == "802-11-wireless" \
                and parts[2] == "activated" and parts[0] != _AP_CON:
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
    # nmcli only returns the currently-associated AP when connected — a known NM
    # limitation with this driver. iwlist does a proper blocking scan regardless.
    # Requires root; pi user has a targeted NOPASSWD sudoers entry for this command.
    try:
        r = subprocess.run(["sudo", "/usr/sbin/iwlist", "wlan0", "scan"],
                           capture_output=True, text=True, timeout=20)
        raw = r.stdout
    except Exception as e:
        log.error("WiFi scan error: %s", e)
        return []
    return _parse_iwlist(raw)


def _parse_iwlist(raw: str) -> list:
    import re
    best: dict = {}   # ssid → entry (keep strongest signal per SSID)
    cur: dict  = {}

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
                # Convert dBm to 0-100 percentage: -100dBm→0, -50dBm→100
                cur["signal"] = max(0, min(100, 2 * (dbm + 100)))
        elif line == "Encryption key:off":
            cur["open"]     = True
            cur["security"] = "--"
        elif line == "Encryption key:on":
            cur["open"]     = False
            cur["security"] = "WPA2"  # refined below if IE found
        elif "WPA2" in line:
            cur["security"] = "WPA2"
        elif "WPA" in line and cur.get("security") != "WPA2":
            cur["security"] = "WPA"
        elif line.startswith("IE: WEP"):
            cur["security"] = "WEP"

    _commit()
    return sorted(best.values(), key=lambda x: -x["signal"])


def _connect_worker(ssid, password=None, con_name=None):
    """Background thread — connects to a network. Always clears flag in finally."""
    import time
    try:
        time.sleep(1.5)  # let the HTTP response reach the client before network drops
        if con_name:
            _nmcli("con", "up", con_name, timeout=30)
        else:
            cmd = ["dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            _nmcli(*cmd, timeout=30)
            # 'dev wifi connect' creates a new profile with autoconnect=yes by default
    except Exception as e:
        log.error("WiFi connect worker: %s", e)
    finally:
        _clear_flag()  # guaranteed cleanup: success, failure, exception, or timeout


def _wifi_authed():
    """AP-subnet requests are pre-authenticated (AP password == WiFi page password)."""
    return request.remote_addr.startswith(_AP_SUBNET) or session.get("wifi_authed", False)


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
    con    = _active_client_con()
    ap_out, _, _ = _nmcli("-t", "-f", "NAME,STATE", "con", "show", "--active")
    ap_up  = any(f"{_AP_CON}:activated" in l for l in ap_out.splitlines())
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
    data     = request.get_json(force=True)
    ssid     = (data.get("ssid")     or "").strip()
    password = (data.get("password") or "").strip() or None
    con_name = (data.get("name")     or "").strip() or None
    if not ssid and not con_name:
        return jsonify({"error": "ssid or name required"}), 400
    _set_flag()
    threading.Thread(target=_connect_worker,
                     args=(ssid, password, con_name), daemon=True).start()
    return jsonify({"ok": True, "ssid": ssid or con_name})


@app.route("/wifi/disconnect", methods=["POST"])
def wifi_disconnect_route():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    _nmcli("dev", "disconnect", "wlan0", timeout=10)
    # Immediately bring up the AP so NM can't auto-reconnect the client.
    # wlan0 can only hold one connection at a time, so the AP being active
    # blocks client autoconnect entirely — no profile modification needed.
    _nmcli("con", "up", "mulchy-ap", timeout=15)
    return jsonify({"ok": True})


@app.route("/wifi/remove", methods=["POST"])
def wifi_remove_route():
    if not _wifi_authed():
        return jsonify({"error": "unauthorized"}), 401
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name or name == _AP_CON:
        return jsonify({"error": "invalid name"}), 400
    _, err, rc = _nmcli("con", "delete", name, timeout=10)
    return jsonify({"ok": rc == 0, "error": err.strip() if rc != 0 else None})


# ── HTML ───────────────────────────────────────────────────────────────────────
# Mobile-first layout. Settings/features in a slide-up sheet reachable on any screen.
# iOS audio: toggleAudio() is synchronous (no async/await) and uses no sampleRate
# override — both required for WebAudio to work on iOS Safari and Chrome.

_WIFI_AUTH_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mulchy \xb7 WiFi</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#09090f;color:#bbb;font:13px/1.5 'SF Mono',ui-monospace,monospace;
     display:flex;align-items:center;justify-content:center;min-height:100dvh;padding:20px}
.card{background:#0d0d1a;border:1px solid #2a2a3e;border-radius:8px;padding:28px 24px;width:100%;max-width:320px}
h1{font-size:.75rem;letter-spacing:.2em;color:#fff;margin-bottom:24px}
label{font-size:.65rem;color:#666;display:block;margin-bottom:6px}
input{width:100%;background:#141420;color:#aaa;border:1px solid #2a2a3e;padding:9px 10px;
      font:inherit;border-radius:4px;margin-bottom:14px}
input:focus{outline:none;border-color:#7c5cfc}
.btn{width:100%;background:#7c5cfc;color:#fff;border:none;padding:10px;border-radius:4px;font:inherit;cursor:pointer}
.btn:active{opacity:.8}
.err{color:#ff6464;font-size:.65rem;margin-top:10px;display:__ERR__}
</style>
</head>
<body>
<div class="card">
  <h1>MULCHY \xb7 WIFI</h1>
  <form method="POST" action="/wifi/auth">
    <label>Password</label>
    <input type="password" name="password" autofocus autocomplete="current-password">
    <button class="btn" type="submit">Unlock</button>
    <p class="err">Incorrect password.</p>
  </form>
</div>
</body>
</html>"""

_WIFI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mulchy · WiFi</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#09090f;color:#bbb;font:12px/1.5 'SF Mono',ui-monospace,monospace;
     min-height:100dvh;padding:0 0 40px}
header{display:flex;align-items:center;gap:12px;padding:12px 16px;
       border-bottom:1px solid #1e1e2e;position:sticky;top:0;z-index:5;background:#09090f}
header a{color:#555;text-decoration:none;font-size:.7rem}
header a:hover{color:#aaa}
h1{font-size:.75rem;letter-spacing:.2em;color:#fff;flex:1}
.page{padding:14px 16px;display:flex;flex-direction:column;gap:12px}
.section{background:#0d0d1a;border:1px solid #2a2a3e;border-radius:6px;overflow:hidden}
.sec-hdr{display:flex;align-items:center;justify-content:space-between;
         padding:8px 12px;border-bottom:1px solid #1a1a28}
.sec-title{font-size:.6rem;color:#555;text-transform:uppercase;letter-spacing:.1em}
.sec-body{padding:4px 0}
.row{display:flex;align-items:center;justify-content:space-between;
     padding:8px 12px;gap:8px;border-bottom:1px solid #111120}
.row:last-child{border-bottom:none}
.row-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem}
.row-meta{font-size:.6rem;color:#555;white-space:nowrap}
.row-actions{display:flex;gap:6px;flex-shrink:0}
.empty{padding:12px;color:#444;font-size:.65rem;text-align:center}
.status-row{padding:10px 12px;display:flex;align-items:center;gap:8px;font-size:.72rem}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.green{background:#50ff96}
.dot.blue{background:#5cc8ff}
.dot.yellow{background:#ffd940;animation:pulse 1s infinite}
.dot.grey{background:#333}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.badge{font-size:.6rem;color:#50ff96;border:1px solid #50ff9630;padding:2px 7px;border-radius:3px}
.btn{background:#7c5cfc;color:#fff;border:none;padding:6px 14px;border-radius:4px;
     font:inherit;cursor:pointer;font-size:.65rem;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.btn:active{opacity:.8}
.btn.sm{padding:4px 10px}
.btn.danger{background:none;border:1px solid #3a2a2a;color:#ff6464}
.btn.danger:active{background:#ff646420}
.btn.ghost{background:none;border:1px solid #2a2a3e;color:#666}
.btn.ghost:active{background:#ffffff08}
/* Connect modal */
#modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);
          z-index:20;align-items:flex-end;justify-content:center;padding:0}
#modal-bg.open{display:flex}
#modal{background:#0d0d1a;border:1px solid #2a2a3e;border-radius:12px 12px 0 0;
       width:100%;max-width:480px;padding:20px 18px 28px;
       padding-bottom:max(28px,env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:12px}
.modal-title{font-size:.75rem;color:#fff}
.modal-warn{background:#1a1200;border:1px solid #3a2e00;border-radius:4px;
            padding:9px 11px;font-size:.65rem;color:#ffd940;line-height:1.5}
.field-label{font-size:.6rem;color:#666;margin-bottom:4px}
input.pw{width:100%;background:#141420;color:#aaa;border:1px solid #2a2a3e;
         padding:9px 10px;font:inherit;border-radius:4px;font-size:.75rem}
input.pw:focus{outline:none;border-color:#7c5cfc}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:4px}
/* Connecting overlay */
#overlay{display:none;position:fixed;inset:0;background:#09090fef;z-index:30;
         flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:32px;
         text-align:center}
#overlay.open{display:flex}
.spinner{width:28px;height:28px;border:2px solid #2a2a3e;border-top-color:#7c5cfc;
         border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.overlay-title{color:#fff;font-size:.8rem;letter-spacing:.1em}
.overlay-msg{color:#666;font-size:.65rem;line-height:1.7;max-width:280px}
.overlay-ssid{color:#9c7cff}
</style>
</head>
<body>
<header>
  <h1>MULCHY · WIFI</h1>
  <a href="/">← Dashboard</a>
</header>

<div class="page">
  <!-- Status -->
  <div class="section">
    <div class="sec-hdr"><span class="sec-title">Status</span></div>
    <div id="status" class="status-row"><span class="dot grey"></span> Loading...</div>
  </div>

  <!-- Saved networks -->
  <div class="section">
    <div class="sec-hdr">
      <span class="sec-title">Saved Networks</span>
    </div>
    <div id="saved-list" class="sec-body"><div class="empty">Loading...</div></div>
  </div>

  <!-- Scan -->
  <div class="section">
    <div class="sec-hdr">
      <span class="sec-title">Nearby Networks</span>
      <button class="btn sm ghost" id="scan-btn" onclick="doScan()">Scan</button>
    </div>
    <div id="scan-list" class="sec-body"><div class="empty">Press Scan to search</div></div>
  </div>
</div>

<!-- Connect modal -->
<div id="modal-bg">
  <div id="modal">
    <div class="modal-title" id="modal-title">Connect to <span id="modal-ssid"></span></div>
    <div class="modal-warn" id="modal-warn"></div>
    <div id="modal-pw-row" style="display:none">
      <div class="field-label">Password</div>
      <input class="pw" type="password" id="modal-pw" autocomplete="current-password"
             onkeydown="if(event.key==='Enter')submitConnect()">
    </div>
    <div class="modal-actions">
      <button class="btn ghost" onclick="closeModal()">Cancel</button>
      <button class="btn" onclick="submitConnect()">Connect</button>
    </div>
  </div>
</div>

<!-- Connecting overlay -->
<div id="overlay">
  <div class="spinner"></div>
  <div class="overlay-title">Connecting...</div>
  <div class="overlay-msg">
    The Pi is switching to <span class="overlay-ssid" id="overlay-ssid"></span>.<br>
    Join <span class="overlay-ssid" id="overlay-ssid2"></span> on your device,
    then visit <strong>mulchy.local:5000</strong> to continue.
  </div>
  <button class="btn ghost" style="margin-top:8px" onclick="document.getElementById('overlay').classList.remove('open')">Dismiss</button>
</div>

<script>
function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Load page ──────────────────────────────────────────────────────────────────
let savedNets=[];

async function init(){
  const [st, saved] = await Promise.all([
    fetch('/wifi/status').then(r=>r.json()).catch(()=>({})),
    fetch('/wifi/saved').then(r=>r.json()).catch(()=>[]),
  ]);
  renderStatus(st);
  savedNets = saved;
  renderSaved(saved);
}

function renderStatus(s){
  const el=document.getElementById('status');
  if(s.connecting){
    el.innerHTML='<span class="dot yellow"></span> Connecting...';
  } else if(s.connected){
    el.innerHTML=`<span class="dot green"></span>&nbsp;Connected to <strong style="color:#ccc;margin:0 6px">${esc(s.connected)}</strong>
      <button class="btn sm danger" style="margin-left:auto" onclick="doDisconnect()">Disconnect</button>`;
  } else if(s.ap){
    el.innerHTML='<span class="dot blue"></span>&nbsp;Broadcasting <strong style="color:#ccc">mulchywifi</strong>';
  } else {
    el.innerHTML='<span class="dot grey"></span>&nbsp;Not connected';
  }
}

function renderSaved(nets){
  const el=document.getElementById('saved-list');
  if(!nets.length){el.innerHTML='<div class="empty">No saved networks</div>';return;}
  el.innerHTML=nets.map(n=>`
    <div class="row">
      <span class="row-name">${esc(n.name)}</span>
      <div class="row-actions">
        ${n.active
          ? '<span class="badge">Active</span>'
          : `<button class="btn sm" onclick='connectSaved(${JSON.stringify(n.name)})'>Connect</button>`}
        <button class="btn sm danger" onclick='removeSaved(${JSON.stringify(n.name)})'>✕</button>
      </div>
    </div>`).join('');
}

function renderScan(nets){
  const el=document.getElementById('scan-list');
  if(!nets.length){el.innerHTML='<div class="empty">No networks found</div>';return;}
  const savedNames=new Set(savedNets.map(n=>n.name));
  el.innerHTML=nets.map(n=>`
    <div class="row">
      <div style="min-width:0;flex:1;overflow:hidden">
        <div class="row-name">${esc(n.ssid)}</div>
        <div class="row-meta">${sigBars(n.signal)}&nbsp;&nbsp;${esc(n.security||'Open')}</div>
      </div>
      <button class="btn sm" onclick='connectNew(${JSON.stringify(n.ssid)},${n.open})'>
        ${savedNames.has(n.ssid)?'Reconnect':'Connect'}
      </button>
    </div>`).join('');
}

function sigBars(pct){
  const n=Math.round(pct/25);
  return ['▂','▄','▆','█'].map((b,i)=>
    `<span style="color:${i<n?'#50ff96':'#333'}">${b}</span>`).join('');
}

// ── Scan ───────────────────────────────────────────────────────────────────────
async function doScan(){
  const btn=document.getElementById('scan-btn');
  const el=document.getElementById('scan-list');
  btn.disabled=true; btn.textContent='Scanning...';
  el.innerHTML='<div class="empty">Scanning — this takes 5–10 seconds...</div>';
  try{
    const nets=await fetch('/wifi/scan').then(r=>r.json());
    renderScan(nets);
  }catch(e){
    el.innerHTML='<div class="empty">Scan failed</div>';
  }
  btn.disabled=false; btn.textContent='Scan';
}

// ── Connect modal ──────────────────────────────────────────────────────────────
let _pendingConnect={};

function connectSaved(name){
  _pendingConnect={name, ssid:name};
  openModal(name, false);
}

function connectNew(ssid, isOpen){
  _pendingConnect={ssid};
  openModal(ssid, !isOpen);
}

function openModal(ssid, needsPw){
  document.getElementById('modal-ssid').textContent=ssid;
  document.getElementById('modal-warn').textContent=
    `The Pi will switch to "${ssid}". Connect your device to "${ssid}" after submitting, then visit mulchy.local:5000.`;
  const pwRow=document.getElementById('modal-pw-row');
  pwRow.style.display=needsPw?'block':'none';
  if(needsPw) setTimeout(()=>document.getElementById('modal-pw').focus(),80);
  document.getElementById('modal-bg').classList.add('open');
}

function closeModal(){
  document.getElementById('modal-bg').classList.remove('open');
  document.getElementById('modal-pw').value='';
}

async function submitConnect(){
  const body={..._pendingConnect};
  const pwEl=document.getElementById('modal-pw');
  if(document.getElementById('modal-pw-row').style.display!=='none'){
    body.password=pwEl.value;
  }
  closeModal();
  showOverlay(body.ssid||body.name);
  try{
    await fetch('/wifi/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  }catch(e){}  // expected if network drops
}

function showOverlay(ssid){
  document.getElementById('overlay-ssid').textContent=ssid;
  document.getElementById('overlay-ssid2').textContent=ssid;
  document.getElementById('overlay').classList.add('open');
}

// ── Disconnect / Remove ────────────────────────────────────────────────────────
async function doDisconnect(){
  if(!confirm('Disconnect from current network?\nThe Pi will return to AP mode.')) return;
  await fetch('/wifi/disconnect',{method:'POST'});
  location.reload();
}

async function removeSaved(name){
  if(!confirm(`Remove saved network "${name}"?`)) return;
  const r=await fetch('/wifi/remove',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}).then(r=>r.json());
  if(r.ok) location.reload();
  else alert('Remove failed: '+(r.error||'unknown error'));
}

init();
</script>
</body>
</html>"""

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Mulchy</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#09090f;color:#bbb;font:12px/1.5 'SF Mono',ui-monospace,monospace;display:flex;flex-direction:column;min-height:100dvh}
header{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;padding-top:max(10px,env(safe-area-inset-top));border-bottom:1px solid #1e1e2e;flex-shrink:0;position:sticky;top:0;z-index:5;background:#09090f}
header h1{font-size:.8rem;letter-spacing:.2em;color:#fff}
.hbtns{display:flex;gap:8px;align-items:center}
.hbtn{background:none;border:1px solid #2a2a3e;color:#666;padding:5px 12px;border-radius:4px;cursor:pointer;font:inherit;font-size:.7rem;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
.hbtn:active{opacity:.7}
.hbtn.on{border-color:#7c5cfc;color:#9c7cff;background:#7c5cfc18}
.dot{width:7px;height:7px;border-radius:50%;background:#333;flex-shrink:0}
.dot.live{background:#50ff96}
/* Main content */
.content{display:flex;flex-direction:column;flex:1}
.vid-wrap{position:relative;background:#000;width:100%}
.vid-wrap img{width:100%;display:block}
#overlay{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}
.viz-section{display:flex;flex-direction:column;border-top:1px solid #1e1e2e}
.sec-title{font-size:.6rem;color:#555;padding:4px 10px;border-bottom:1px solid #1a1a28;text-transform:uppercase;letter-spacing:.1em}
#viz{display:block;width:100%;height:180px;background:#07070e}

/* ── Bottom sheet ── */
#sheet-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;backdrop-filter:blur(2px)}
#sheet{
  position:fixed;bottom:0;left:0;right:0;z-index:21;
  background:#0d0d1a;border-top:1px solid #2a2a3e;
  border-radius:16px 16px 0 0;
  max-height:80dvh;display:flex;flex-direction:column;
  transform:translateY(100%);transition:transform .28s cubic-bezier(.32,1,.46,1);
  padding-bottom:env(safe-area-inset-bottom);
}
#sheet.open{transform:translateY(0)}
.sheet-handle{width:36px;height:4px;background:#2a2a3e;border-radius:2px;margin:10px auto 6px}
.sheet-tabs{display:flex;border-bottom:1px solid #1e1e2e;flex-shrink:0}
.stab{flex:1;padding:8px;text-align:center;font-size:.65rem;color:#555;cursor:pointer;border-bottom:2px solid transparent;-webkit-tap-highlight-color:transparent}
.stab.active{color:#9c7cff;border-bottom-color:#7c5cfc}
.sheet-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch}
.tab-pane{display:none;padding:12px 16px}
.tab-pane.active{display:flex;flex-direction:column;gap:10px}
/* Meters in sheet */
.mrow{display:flex;align-items:center;gap:8px}
.mlabel{width:60px;font-size:.65rem;color:#666;text-align:right;flex-shrink:0}
.mbar{flex:1;height:6px;background:#141420;border-radius:3px;overflow:hidden}
.mfill{height:100%;border-radius:3px;transition:width .2s ease}
.mval{width:32px;font-size:.65rem;color:#555;text-align:right}
/* Settings in sheet */
.srow{display:flex;align-items:center;gap:8px;min-height:32px}
.slabel{width:80px;font-size:.65rem;color:#666;flex-shrink:0}
input[type=range]{flex:1;height:4px;accent-color:#7c5cfc;cursor:pointer;-webkit-appearance:none;touch-action:pan-y}
.sval{width:38px;font-size:.65rem;color:#666;text-align:right}
select{background:#141420;color:#aaa;border:1px solid #2a2a3e;padding:4px 6px;font:inherit;border-radius:4px;flex:1;font-size:.7rem}
.pbtn{background:none;border:1px solid #2a2a3e;color:#666;padding:5px 10px;border-radius:4px;cursor:pointer;font:inherit;font-size:.65rem;-webkit-tap-highlight-color:transparent;touch-action:manipulation;text-transform:capitalize}
.pbtn:active{opacity:.7}
.pbtn.on{border-color:#7c5cfc;color:#9c7cff;background:#7c5cfc18}
</style>
</head>
<body>
<header>
  <h1>MULCHY</h1>
  <div class="hbtns">
    <button class="hbtn" id="overlay-btn" onclick="toggleOverlay()">Overlays</button>
    <button class="hbtn" id="audio-btn"   onclick="toggleAudio()">▶ Browser Audio</button>
    <button class="hbtn"                  onclick="openSheet()">⚙</button>
    <div class="dot" id="dot"></div>
  </div>
</header>

<div class="content">
  <div class="vid-wrap">
    <img id="vid" src="/stream/video" alt="camera">
    <canvas id="overlay"></canvas>
  </div>
  <div class="viz-section">
    <div class="sec-title">Synth</div>
    <canvas id="viz"></canvas>
  </div>
</div>

<!-- Settings / Features sheet -->
<div id="sheet-bg" onclick="closeSheet()"></div>
<div id="sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-tabs">
    <div class="stab active" onclick="switchTab('features')">Features</div>
    <div class="stab"        onclick="switchTab('settings')">Settings</div>
  </div>
  <div class="sheet-body">
    <div class="tab-pane active" id="tab-features">
      <div class="mrow"><span class="mlabel">brightness</span><div class="mbar"><div class="mfill" id="m-brightness" style="background:#ffd940"></div></div><span class="mval" id="v-brightness">–</span></div>
      <div class="mrow"><span class="mlabel">saturation</span><div class="mbar"><div class="mfill" id="m-saturation" style="background:#64c8ff"></div></div><span class="mval" id="v-saturation">–</span></div>
      <div class="mrow"><span class="mlabel">edges</span>     <div class="mbar"><div class="mfill" id="m-edges"      style="background:#c864ff"></div></div><span class="mval" id="v-edges">–</span></div>
      <div class="mrow"><span class="mlabel">motion</span>    <div class="mbar"><div class="mfill" id="m-motion"     style="background:#50ff96"></div></div><span class="mval" id="v-motion">–</span></div>
      <div class="mrow"><span class="mlabel">lum var</span>   <div class="mbar"><div class="mfill" id="m-lumvar"     style="background:#ff9050"></div></div><span class="mval" id="v-lumvar">–</span></div>
    </div>
    <div class="tab-pane" id="tab-settings"></div>
  </div>
</div>

<script>
// ── SSE ────────────────────────────────────────────────────────────────────────
const es = new EventSource('/events');
es.addEventListener('features', e => onFeatures(JSON.parse(e.data)));
es.addEventListener('audio',    e => onAudio(JSON.parse(e.data)));
es.onopen  = () => { document.getElementById('dot').classList.add('live'); };
es.onerror = () => { document.getElementById('dot').classList.remove('live'); };

// ── Bottom sheet ───────────────────────────────────────────────────────────────
function openSheet(){
  document.getElementById('sheet-bg').style.display='block';
  requestAnimationFrame(()=>document.getElementById('sheet').classList.add('open'));
}
function closeSheet(){
  document.getElementById('sheet').classList.remove('open');
  setTimeout(()=>document.getElementById('sheet-bg').style.display='none', 300);
}
function switchTab(name){
  document.querySelectorAll('.stab').forEach((t,i)=>t.classList.toggle('active',['features','settings'][i]===name));
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));
}

// ── Feature meters ─────────────────────────────────────────────────────────────
let lastFeatures = {};
function onFeatures(f){
  lastFeatures = f;
  setMeter('brightness', f.brightness);
  setMeter('saturation', f.saturation);
  setMeter('edges',      f.edge_density);
  setMeter('motion',     f.motion_amount);
  setMeter('lumvar',     Math.min(1,(f.luminance_variance||0)*10));
  drawOverlayCanvas(f);
  if(!audioEnabled) drawFeatureViz(f);
}
function setMeter(k,v){
  v=Math.max(0,Math.min(1,v||0));
  const f=document.getElementById('m-'+k),s=document.getElementById('v-'+k);
  if(f) f.style.width=(v*100).toFixed(1)+'%';
  if(s) s.textContent=v.toFixed(2);
}

// ── Video overlay canvas ───────────────────────────────────────────────────────
const vid     = document.getElementById('vid');
const overlay = document.getElementById('overlay');
const oc      = overlay.getContext('2d');
let overlaysOn = true;
const SCALE    = [0,2,4,7,9,12,14,16,19,21,24];

function syncOverlay(){ overlay.width=vid.offsetWidth; overlay.height=vid.offsetHeight; }
new ResizeObserver(syncOverlay).observe(vid);
vid.addEventListener('load', syncOverlay);

function toggleOverlay(){
  overlaysOn=!overlaysOn;
  document.getElementById('overlay-btn').classList.toggle('on',overlaysOn);
  if(!overlaysOn) oc.clearRect(0,0,overlay.width,overlay.height);
  else drawOverlayCanvas(lastFeatures);
}

function drawOverlayCanvas(f){
  if(!overlaysOn) return;
  const W=overlay.width, H=overlay.height;
  if(!W||!H) return;
  oc.clearRect(0,0,W,H);
  // Hue swatches
  (f.hue_centers||[]).forEach((hue_deg,i)=>{
    const w=(f.hue_weights||[])[i]||0, sz=Math.max(8,10+w*14), cx=16+i*36;
    oc.beginPath(); oc.arc(cx,18,sz,0,Math.PI*2);
    oc.fillStyle=`hsla(${hue_deg.toFixed(0)},80%,60%,.85)`; oc.fill();
    oc.strokeStyle='rgba(255,255,255,.4)'; oc.lineWidth=1; oc.stroke();
  });
  // Scanline ticks
  for(let i=0;i<8;i++){
    const y=(i/7)*H;
    oc.strokeStyle='rgba(255,255,80,.5)'; oc.lineWidth=1;
    oc.beginPath(); oc.moveTo(0,y); oc.lineTo(10,y); oc.stroke();
  }
  // Feature bars
  const metrics=[[f.brightness,'#ffd940'],[f.saturation,'#64c8ff'],[f.edge_density,'#c864ff'],[f.motion_amount,'#50ff96']];
  const bh=7,bp=3,bpad=10;
  let by=H-(bh+bp)*metrics.length-4;
  metrics.forEach(([val,col])=>{
    const v=Math.max(0,Math.min(1,val||0));
    oc.fillStyle='rgba(10,10,20,.6)'; oc.fillRect(bpad,by,W-bpad*2,bh);
    oc.fillStyle=col; oc.fillRect(bpad,by,(W-bpad*2)*v,bh);
    by+=bh+bp;
  });
  // Motion arrow
  const motion=f.motion_amount||0;
  if(motion>0.12){
    const mx=f.motion_cx||0,my=f.motion_cy||0,cx=W/2,cy=H/2;
    const ex=cx+mx*motion*60,ey=cy+my*motion*60;
    oc.strokeStyle='rgba(255,80,80,.85)'; oc.lineWidth=2;
    oc.beginPath(); oc.moveTo(cx,cy); oc.lineTo(ex,ey); oc.stroke();
    oc.fillStyle='rgba(255,80,80,.85)';
    oc.beginPath(); oc.arc(ex,ey,4,0,Math.PI*2); oc.fill();
  }
}

// ── Synth visualizer ──────────────────────────────────────────────────────────
const vizCanvas=document.getElementById('viz'), vctx=vizCanvas.getContext('2d');

function drawFeatureViz(f){
  const W=vizCanvas.clientWidth, H=vizCanvas.clientHeight||180;
  vizCanvas.width=W; vizCanvas.height=H;
  vctx.fillStyle='#07070e'; vctx.fillRect(0,0,W,H);
  const bright=f.brightness||.5,sat=f.saturation||.5,motion=f.motion_amount||0;
  const hues=f.hue_centers||[],weights=f.hue_weights||[];
  vctx.strokeStyle='rgba(255,217,64,.1)'; vctx.lineWidth=1;
  vctx.beginPath(); vctx.moveTo(0,H*(1-bright)); vctx.lineTo(W,H*(1-bright)); vctx.stroke();
  const baseHz=130.81*Math.pow(2,((bright-.5)*24)/12);
  const pitchBend=(f.motion_cx||0)*12*motion;
  const nActive=Math.max(1,Math.round(.5+sat*hues.length));
  hues.slice(0,nActive).forEach((hue_deg,i)=>{
    const w=weights[i]||0,si=Math.floor((hue_deg/360)*SCALE.length)%SCALE.length;
    const semi=SCALE[si]+(i%4)*12+pitchBend,freq=baseHz*Math.pow(2,semi/12);
    const xn=Math.log(Math.max(freq,40)/40)/Math.log(8000/40),x=xn*W,barH=Math.max(10,w*H*.75);
    vctx.fillStyle=`hsla(${hue_deg.toFixed(0)},65%,60%,${.45+w*.55})`;
    vctx.fillRect(x-5,H-barH,10,barH);
    vctx.fillStyle=`hsla(${hue_deg.toFixed(0)},55%,78%,.75)`;
    vctx.font='9px monospace';
    vctx.fillText(freq<1000?freq.toFixed(0)+'Hz':(freq/1000).toFixed(1)+'k',Math.min(x-10,W-38),H-barH-4);
  });
}

// ── Audio (iOS-safe: synchronous, no sampleRate override) ─────────────────────
let audioCtx=null, analyser=null, audioEnabled=false, nextPlayTime=0;

function toggleAudio(){
  const btn=document.getElementById('audio-btn');
  if(!audioEnabled){
    // Must stay synchronous — iOS Safari revokes the user-gesture token on await
    const AC=window.AudioContext||window.webkitAudioContext;
    audioCtx=new AC();          // no sampleRate: let device use its native rate
    audioCtx.resume();          // un-suspend (iOS starts contexts suspended)
    analyser=audioCtx.createAnalyser();
    analyser.fftSize=2048; analyser.smoothingTimeConstant=0.8;
    analyser.connect(audioCtx.destination);
    audioEnabled=true;
    nextPlayTime=audioCtx.currentTime+0.4;
    btn.textContent='■ Browser Audio'; btn.classList.add('on');
    requestAnimationFrame(drawAnalyser);
  } else {
    audioCtx.close(); audioCtx=null; analyser=null; audioEnabled=false;
    btn.textContent='▶ Browser Audio'; btn.classList.remove('on');
  }
}

function onAudio(data){
  if(!audioEnabled||!audioCtx||audioCtx.state==='closed') return;
  const raw=atob(data.pcm), b8=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) b8[i]=raw.charCodeAt(i);
  const i16=new Int16Array(b8.buffer), floats=new Float32Array(i16.length);
  for(let i=0;i<i16.length;i++) floats[i]=i16[i]/32767;

  // Create buffer at the source sample rate; AudioContext resamples automatically
  const buf=audioCtx.createBuffer(1,floats.length,data.sr);
  buf.copyToChannel(floats,0);
  const src=audioCtx.createBufferSource();
  src.buffer=buf; src.connect(analyser);

  const now=audioCtx.currentTime;
  // Reset if behind (tab backgrounded) or too far ahead (stalled chunks)
  if(nextPlayTime<now+0.05 || nextPlayTime>now+3.0) nextPlayTime=now+0.05;
  src.start(nextPlayTime);
  nextPlayTime+=buf.duration;
}

function drawAnalyser(){
  if(!audioEnabled||!analyser) return;
  requestAnimationFrame(drawAnalyser);
  const W=vizCanvas.clientWidth, H=vizCanvas.clientHeight||180;
  vizCanvas.width=W; vizCanvas.height=H;
  vctx.fillStyle='#07070e'; vctx.fillRect(0,0,W,H);
  const fd=new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(fd);
  const step=W/fd.length;
  for(let i=0;i<fd.length;i++){
    const v=fd[i]/255;
    vctx.fillStyle=`hsla(${190+i/fd.length*160},70%,55%,.9)`;
    vctx.fillRect(i*step,H-v*H*.5,Math.max(1,step),v*H*.5);
  }
  const td=new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(td);
  vctx.strokeStyle='rgba(124,92,252,.85)'; vctx.lineWidth=1.5; vctx.beginPath();
  for(let i=0;i<td.length;i++){
    const x=(i/td.length)*W, y=(1-(td[i]*.5+.5))*(H*.48);
    i===0?vctx.moveTo(x,y):vctx.lineTo(x,y);
  }
  vctx.stroke();
}

// ── Settings ───────────────────────────────────────────────────────────────────
const SMETA=[
  {k:'MASTER_VOLUME',         l:'Volume',      t:'r',min:0,  max:1,    s:.01},
  {k:'BLEND_ALPHA',           l:'Blend Speed', t:'r',min:.05,max:1,    s:.01},
  {k:'MIX_LOWPASS_HZ',        l:'Warmth',      t:'r',min:300,max:15000,s:100},
  {k:'LAYER_GLITCH_LEVEL',    l:'Glitch Mix',  t:'r',min:0,  max:1,    s:.01},
  {k:'LAYER_TONAL_LEVEL',     l:'Tonal Mix',   t:'r',min:0,  max:1,    s:.01},
  {k:'LAYER_RHYTHM_LEVEL',    l:'Rhythm Mix',  t:'r',min:0,  max:1,    s:.01},
  {k:'TONAL_DETUNE_CENTS',    l:'Detune',      t:'r',min:0,  max:50,   s:1  },
  {k:'MOTION_PITCH_SEMITONES',l:'Pitch Bend',  t:'r',min:0,  max:24,   s:1  },
  {k:'MOTION_SENSITIVITY',    l:'Motion Sens', t:'r',min:.5, max:5,    s:.1 },
  {k:'TONAL_WAVEFORM',        l:'Waveform',    t:'s',opts:['sine','triangle','sawtooth','square']},
];

// sliderMap: key → {inp, sp} for updating on preset switch
const sliderMap={};

function buildSettings(vals){
  const presets=vals._presets||['default','ambient','glitchy','percussive'];
  const panel=document.getElementById('tab-settings');
  panel.innerHTML='';
  // Preset row
  const prow=document.createElement('div'); prow.className='srow'; prow.style.cssText='flex-wrap:wrap;gap:4px;align-items:center';
  const plbl=document.createElement('span'); plbl.className='slabel'; plbl.textContent='Preset';
  prow.appendChild(plbl);
  const pbg=document.createElement('div'); pbg.id='preset-buttons'; pbg.style.cssText='display:flex;gap:4px;flex-wrap:wrap;flex:1;align-items:center';
  buildPresetButtons(pbg,presets,vals._preset||'');
  prow.appendChild(pbg);
  panel.appendChild(prow);
  // Reset row — single button, hidden for custom presets
  const rrow=document.createElement('div'); rrow.id='reset-row';
  rrow.style.cssText='display:flex;padding:2px 0 4px 88px';
  const rbtn=document.createElement('button'); rbtn.className='pbtn';
  rbtn.style.cssText='font-size:.6rem;opacity:.5';
  rbtn.textContent='↺ Reset to factory defaults'; rbtn.onclick=()=>resetPreset();
  rrow.appendChild(rbtn);
  panel.appendChild(rrow);
  // Divider
  const div=document.createElement('div'); div.style.cssText='border-top:1px solid #1e1e2e;margin:4px 0';
  panel.appendChild(div);
  SMETA.forEach(m=>{
    const row=document.createElement('div'); row.className='srow';
    const lbl=document.createElement('span'); lbl.className='slabel'; lbl.textContent=m.l;
    row.appendChild(lbl);
    if(m.t==='s'){
      const sel=document.createElement('select');
      m.opts.forEach(o=>{const opt=document.createElement('option');opt.value=opt.textContent=o;if(vals[m.k]===o)opt.selected=true;sel.appendChild(opt);});
      sel.onchange=()=>save(m.k,sel.value);
      row.appendChild(sel);
      sliderMap[m.k]={sel};
    } else {
      const v=vals[m.k]??0;
      const inp=document.createElement('input'); inp.type='range'; inp.min=m.min; inp.max=m.max; inp.step=m.s; inp.value=v;
      const sp=document.createElement('span'); sp.className='sval'; sp.textContent=fmt(v,m.s);
      inp.oninput=()=>{const n=parseFloat(inp.value);sp.textContent=fmt(n,m.s);save(m.k,n);};
      row.appendChild(inp); row.appendChild(sp);
      sliderMap[m.k]={inp,sp};
    }
    panel.appendChild(row);
  });
  highlightPreset(vals._preset||'');
}

function buildPresetButtons(container,presets,active){
  container.innerHTML='';
  presets.forEach(name=>{
    const btn=document.createElement('button'); btn.className='pbtn'; btn.id='pbtn-'+name;
    btn.textContent=name; btn.onclick=()=>switchPreset(name);
    container.appendChild(btn);
  });
  const cloneBtn=document.createElement('button'); cloneBtn.className='pbtn';
  cloneBtn.style.cssText='color:#7c5cfc;border-color:#7c5cfc40;opacity:.8';
  cloneBtn.textContent='+ Clone'; cloneBtn.onclick=()=>clonePreset();
  container.appendChild(cloneBtn);
  highlightPreset(active);
}

function highlightPreset(name){
  document.querySelectorAll('.pbtn[id^="pbtn-"]').forEach(b=>b.classList.toggle('on',b.id==='pbtn-'+name));
  // Show reset button only for built-in (non-custom) presets
  const rrow=document.getElementById('reset-row');
  if(rrow) rrow.style.display=customPresets.includes(name)?'none':'flex';
}

function switchPreset(name){
  currentPreset=name;
  highlightPreset(name);
  fetch('/api/preset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preset:name})})
    .then(r=>r.json()).then(d=>{if(d.settings) syncSliders(d.settings);});
}

function resetPreset(){
  if(!confirm('Reset "'+currentPreset+'" to factory defaults? Your changes will be lost.')) return;
  fetch('/api/preset/reset',{method:'POST'})
    .then(r=>r.json()).then(d=>{if(d.settings) syncSliders(d.settings);});
}

function clonePreset(){
  fetch('/api/preset/clone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:currentPreset})})
    .then(r=>r.json()).then(d=>{
      if(!d.ok){alert('Clone failed');return;}
      currentPreset=d.name;
      customPresets=d.custom||[...customPresets,d.name];
      const pbg=document.getElementById('preset-buttons');
      if(pbg) buildPresetButtons(pbg,d.presets,d.name);
      if(d.settings) syncSliders(d.settings);
    });
}

function syncSliders(vals){
  SMETA.forEach(m=>{
    if(!(m.k in vals)) return;
    const ctrl=sliderMap[m.k];
    if(!ctrl) return;
    if(m.t==='s'){ if(ctrl.sel) ctrl.sel.value=vals[m.k]; }
    else { if(ctrl.inp){ctrl.inp.value=vals[m.k]; ctrl.sp.textContent=fmt(vals[m.k],m.s);} }
  });
}

let currentPreset='';
let customPresets=[];
fetch('/api/settings').then(r=>r.json()).then(vals=>{
  fetch('/api/preset').then(r=>r.json()).then(pd=>{
    currentPreset=pd.preset;
    customPresets=pd.custom||[];
    vals._preset=pd.preset;
    vals._presets=pd.presets;
    buildSettings(vals);
  });
});
function fmt(v,s){return s<1?v.toFixed(2):v.toFixed(0);}
let saveTimer=null; const pending={};
function save(k,v){
  pending[k]=v; clearTimeout(saveTimer);
  saveTimer=setTimeout(()=>{
    fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...pending})});
    Object.keys(pending).forEach(k=>delete pending[k]);
  },300);
}
</script>
</body>
</html>
"""
