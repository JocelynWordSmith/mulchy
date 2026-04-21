"""PipeWire sink enumeration and default-sink selection via wpctl."""

import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)


def _wpctl_env() -> dict:
    # wpctl needs XDG_RUNTIME_DIR to reach the user's PipeWire socket; systemd
    # units don't always inherit it.
    env = os.environ.copy()
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return env


def list_output_devices() -> list[dict]:
    try:
        r = subprocess.run(
            ["wpctl", "status"], capture_output=True, text=True, timeout=5,
            env=_wpctl_env(),
        )
        if r.returncode == 0:
            return _parse_wpctl_sinks(r.stdout)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("wpctl failed: %s", e)
    return []


def _parse_wpctl_sinks(output: str) -> list[dict]:
    sinks = []
    in_audio = False
    in_sinks = False
    for line in output.splitlines():
        clean = line.replace("│", " ").replace("├", " ").replace("└", " ").replace("─", " ").strip()
        if clean == "Audio":
            in_audio = True
            continue
        if clean == "Video":
            in_audio = False
            in_sinks = False
            continue
        if not in_audio:
            continue
        if clean == "Sinks:":
            in_sinks = True
            continue
        if in_sinks:
            if not clean or (clean.endswith(":") and not any(c.isdigit() for c in clean)):
                in_sinks = False
                continue
            m = re.match(r"(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[.*\])?\s*$", clean)
            if m:
                sinks.append({
                    "id": int(m.group(2)),
                    "name": m.group(3).strip(),
                    "is_default": m.group(1) == "*",
                })
    return sinks


def set_default_sink(sink_id: int) -> dict:
    try:
        r = subprocess.run(
            ["wpctl", "set-default", str(sink_id)],
            capture_output=True, text=True, timeout=5,
            env=_wpctl_env(),
        )
        if r.returncode == 0:
            log.info("PipeWire default sink set to %d", sink_id)
            return {"ok": True, "device": sink_id}
        err = r.stderr.strip() or f"wpctl exit code {r.returncode}"
        log.error("wpctl set-default failed: %s", err)
        return {"ok": False, "device": sink_id, "error": err}
    except FileNotFoundError:
        return {"ok": False, "device": sink_id, "error": "wpctl not found"}
    except Exception as e:
        return {"ok": False, "device": sink_id, "error": str(e)}
