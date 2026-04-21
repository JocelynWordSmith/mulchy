"""Mulchy web dashboard: MJPEG stream, audio-sink picker, WiFi onboarding, power-off."""

import functools
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, redirect, request, session
from PIL import Image

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; fall back to environment variables only

from mulchy.player import list_output_devices, set_default_sink

log = logging.getLogger(__name__)
app = Flask(__name__)

# Stable secret key derived from machine-id — persists across restarts without a keyfile
try:
    _mid = open("/etc/machine-id").read().strip()
    app.secret_key = hashlib.sha256((_mid + "mulchy").encode()).hexdigest()
except Exception:
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mulchy-fallback-secret")


# ── Shared state ───────────────────────────────────────────────────────────────

def _default_state_file() -> pathlib.Path:
    legacy = pathlib.Path.home() / "mulchy" / "state.json"
    if legacy.exists():
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    p = base / "mulchy"
    p.mkdir(parents=True, exist_ok=True)
    return p / "state.json"


_cond        = threading.Condition(threading.Lock())
_frame_jpeg  = None
_seq         = 0
_STATE_FILE  = _default_state_file()


def update(frame):
    global _frame_jpeg, _seq
    jpeg = _encode_jpeg(frame)
    with _cond:
        _frame_jpeg = jpeg
        _seq += 1
        _cond.notify_all()


def run(host="0.0.0.0", port=5000):
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


@app.route("/api/audio/devices")
def audio_devices():
    devices = list_output_devices()
    return jsonify({"devices": devices, "active": _active_sink_id(devices)})


@app.route("/api/audio/device", methods=["POST"])
def audio_device_set():
    data = request.get_json(force=True)
    device_id = data.get("device")
    if device_id is None:
        return jsonify({"error": "device id required"}), 400
    result = set_default_sink(int(device_id))
    if result.get("ok"):
        _save_state()
    return jsonify(result)


@app.route("/api/system/shutdown", methods=["POST"])
def system_shutdown():
    try:
        subprocess.Popen(
            ["sudo", "/sbin/shutdown", "-h", "now"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.warning("Shutdown requested via web UI")
        return jsonify({"ok": True})
    except Exception as e:
        log.error("Shutdown failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Generators ─────────────────────────────────────────────────────────────────

def _mjpeg():
    last = -1
    while True:
        with _cond:
            _cond.wait_for(lambda: _seq != last, timeout=1.5)
            last = _seq
            jpeg = _frame_jpeg
        if not jpeg:
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _encode_jpeg(frame, quality=70):
    if frame is None:
        return None
    try:
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception as e:
        log.warning("JPEG encode failed: %s", e)
        return None


def _active_sink_id(devices=None):
    if devices is None:
        devices = list_output_devices()
    return next((d["id"] for d in devices if d.get("is_default")), None)


def _load_state():
    try:
        state = json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    saved_device = state.get("audio_device")
    if saved_device is not None:
        set_default_sink(saved_device)


def _save_state():
    state = {"audio_device": _active_sink_id()}
    try:
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(_STATE_FILE)
    except Exception as e:
        log.error("Failed to save state: %s", e)


# ── WiFi management ───────────────────────────────────────────────────────────

_AP_CON    = "mulchy-ap"
_AP_SUBNET = "10.42.0."
_WIFI_PASS = os.environ.get("WIFI_PASSWORD", "")
# /tmp is tmpfs — this file is wiped on reboot, so a crash can never leave the
# Pi stuck between states. _connect_worker's finally: handles runtime cleanup.
_FLAG_FILE = pathlib.Path("/tmp/mulchy-connecting")


def _nmcli(*args, timeout=15):
    # NOPASSWD rule in /etc/sudoers.d/mulchy-nmcli lets the pi user run nmcli as root.
    try:
        r = subprocess.run(["sudo", "nmcli"] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def _active_client_con():
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
    try:
        r = subprocess.run(["sudo", "/usr/sbin/iwlist", "wlan0", "scan"],
                           capture_output=True, text=True, timeout=20)
        raw = r.stdout
    except Exception as e:
        log.error("WiFi scan error: %s", e)
        return []
    return _parse_iwlist(raw)


def _parse_iwlist(raw: str) -> list:
    best: dict = {}
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
                cur["signal"] = max(0, min(100, 2 * (dbm + 100)))
        elif line == "Encryption key:off":
            cur["open"]     = True
            cur["security"] = "--"
        elif line == "Encryption key:on":
            cur["open"]     = False
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
    try:
        time.sleep(1.5)  # let the HTTP response reach the client before network drops
        if con_name:
            _nmcli("con", "up", con_name, timeout=30)
        else:
            cmd = ["dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            _nmcli(*cmd, timeout=30)
    except Exception as e:
        log.error("WiFi connect worker: %s", e)
    finally:
        _FLAG_FILE.unlink(missing_ok=True)


def _wifi_authed():
    # AP-subnet requests are pre-authenticated — the AP password matched already.
    return request.remote_addr.startswith(_AP_SUBNET) or session.get("wifi_authed", False)


def _require_wifi_auth(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if not _wifi_authed():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped


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
@_require_wifi_auth
def wifi_status():
    con    = _active_client_con()
    ap_out, _, _ = _nmcli("-t", "-f", "NAME,STATE", "con", "show", "--active")
    ap_up  = any(f"{_AP_CON}:activated" in line for line in ap_out.splitlines())
    return jsonify({
        "connected":  con,
        "ap":         ap_up,
        "connecting": _FLAG_FILE.exists(),
    })


@app.route("/wifi/saved")
@_require_wifi_auth
def wifi_saved_route():
    return jsonify(_saved_networks())


@app.route("/wifi/scan")
@_require_wifi_auth
def wifi_scan_route():
    return jsonify(_scan_networks())


@app.route("/wifi/connect", methods=["POST"])
@_require_wifi_auth
def wifi_connect_route():
    data     = request.get_json(force=True)
    ssid     = (data.get("ssid")     or "").strip()
    password = (data.get("password") or "").strip() or None
    con_name = (data.get("name")     or "").strip() or None
    if not ssid and not con_name:
        return jsonify({"error": "ssid or name required"}), 400
    _FLAG_FILE.touch()
    threading.Thread(target=_connect_worker,
                     args=(ssid, password, con_name), daemon=True).start()
    return jsonify({"ok": True, "ssid": ssid or con_name})


@app.route("/wifi/disconnect", methods=["POST"])
@_require_wifi_auth
def wifi_disconnect_route():
    _nmcli("dev", "disconnect", "wlan0", timeout=10)
    # wlan0 holds one connection at a time, so bringing up the AP blocks
    # client autoconnect entirely — no profile modification needed.
    _nmcli("con", "up", _AP_CON, timeout=15)
    return jsonify({"ok": True})


@app.route("/wifi/remove", methods=["POST"])
@_require_wifi_auth
def wifi_remove_route():
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name or name == _AP_CON:
        return jsonify({"error": "invalid name"}), 400
    _, err, rc = _nmcli("con", "delete", name, timeout=10)
    return jsonify({"ok": rc == 0, "error": err.strip() if rc != 0 else None})


# ── HTML ───────────────────────────────────────────────────────────────────────

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
.hbtn.power{border-color:#3a2a2a;color:#ff6464}
.hbtn.power:active{background:#ff646420}
.vid-wrap{position:relative;background:#000;width:100%;flex:1}
.vid-wrap img{width:100%;display:block}

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
.sheet-title{font-size:.6rem;color:#555;text-align:center;text-transform:uppercase;letter-spacing:.1em;padding:6px 0 10px;border-bottom:1px solid #1e1e2e}
.sheet-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:14px 16px;display:flex;flex-direction:column;gap:12px}
.srow{display:flex;align-items:center;gap:8px;min-height:32px}
.slabel{width:80px;font-size:.65rem;color:#666;flex-shrink:0}
select{background:#141420;color:#aaa;border:1px solid #2a2a3e;padding:4px 6px;font:inherit;border-radius:4px;flex:1;font-size:.7rem}
.pbtn{background:none;border:1px solid #2a2a3e;color:#666;padding:5px 10px;border-radius:4px;cursor:pointer;font:inherit;font-size:.65rem;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
.pbtn:active{opacity:.7}
.wifi-link{display:block;color:#9c7cff;text-decoration:none;padding:10px 12px;background:#141420;border:1px solid #2a2a3e;border-radius:4px;font-size:.7rem;text-align:center}
.wifi-link:active{opacity:.7}
</style>
</head>
<body>
<header>
  <h1>MULCHY</h1>
  <div class="hbtns">
    <button class="hbtn"                  onclick="openSheet()">⚙</button>
    <button class="hbtn power" title="Power off" onclick="shutdownPi()">⏻</button>
  </div>
</header>

<div class="vid-wrap">
  <img id="vid" src="/stream/video" alt="camera">
</div>

<!-- Settings sheet -->
<div id="sheet-bg" onclick="closeSheet()"></div>
<div id="sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-title">Settings</div>
  <div class="sheet-body">
    <div class="srow">
      <span class="slabel">Output</span>
      <select id="audio-device-select"><option value="">Loading...</option></select>
      <button class="pbtn" style="font-size:.6rem;flex-shrink:0;padding:4px 8px" onclick="loadAudioDevices()">↻</button>
    </div>
    <a class="wifi-link" href="/wifi">WiFi settings →</a>
  </div>
</div>

<script>
function openSheet(){
  document.getElementById('sheet-bg').style.display='block';
  requestAnimationFrame(()=>document.getElementById('sheet').classList.add('open'));
}
function closeSheet(){
  document.getElementById('sheet').classList.remove('open');
  setTimeout(()=>document.getElementById('sheet-bg').style.display='none', 300);
}

function loadAudioDevices(){
  const sel=document.getElementById('audio-device-select');
  if(!sel) return;
  fetch('/api/audio/devices').then(r=>r.json()).then(d=>{
    sel.innerHTML='';
    if(!d.devices||!d.devices.length){
      sel.innerHTML='<option value="">No devices found</option>';
      return;
    }
    (d.devices||[]).forEach(dev=>{
      const o=document.createElement('option');
      o.value=dev.id; o.textContent=dev.name;
      if(dev.is_default) o.selected=true;
      sel.appendChild(o);
    });
  }).catch(()=>{sel.innerHTML='<option value="">Unavailable</option>';});
}

function switchAudioDevice(val){
  if(!val) return;
  fetch('/api/audio/device',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:parseInt(val)})})
    .then(r=>r.json()).then(d=>{
      if(!d.ok) alert('Failed to switch audio: '+(d.error||'unknown'));
    });
}

document.getElementById('audio-device-select').onchange=e=>switchAudioDevice(e.target.value);
loadAudioDevices();

const SHUTDOWN_HTML='<div style="display:flex;align-items:center;justify-content:center;min-height:100dvh;color:#ff6464;font:13px/1.7 SF Mono,monospace;text-align:center;padding:20px">Shutting down...<br><span style="color:#555;font-size:.7rem">Wait for the green LED to stop blinking, then unplug.</span></div>';
function shutdownPi(){
  if(!confirm('Power off the Pi now?\nWait ~20s for the green LED to stop blinking before unplugging.')) return;
  fetch('/api/system/shutdown',{method:'POST'})
    .then(r=>r.json()).then(d=>{
      if(d.ok) document.body.innerHTML=SHUTDOWN_HTML;
      else alert('Shutdown failed: '+(d.error||'unknown. Check sudoers config.'));
    }).catch(()=>{document.body.innerHTML=SHUTDOWN_HTML;});
}
</script>
</body>
</html>
"""
