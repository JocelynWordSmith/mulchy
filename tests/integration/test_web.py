"""
Integration tests for mulchy.web Flask routes.
Uses Flask's test client — no real server, no network.
"""

import json

import pytest

import mulchy.web as web
from mulchy import config as cfg


@pytest.fixture
def client(tmp_state_file, clean_config):
    """Flask test client with isolated state."""
    web.app.config["TESTING"] = True
    # Reset module-level web state between tests
    web._active_preset = "ambient"
    web._custom_presets = {}
    web._preset_settings = {}
    cfg.load_preset("ambient")
    with web.app.test_client() as c:
        yield c


# ── Basic routes ──────────────────────────────────────────────────────────────

def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<html" in r.data.lower() or b"<!doctype" in r.data.lower()


# ── Settings API ──────────────────────────────────────────────────────────────

def test_api_settings_get_has_all_meta_keys(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = json.loads(r.data)
    for key, *_ in web._SETTINGS_META:
        assert key in data, f"Missing key in /api/settings response: {key}"


def test_api_settings_post_updates_config(client, clean_config):
    r = client.post(
        "/api/settings",
        data=json.dumps({"MASTER_VOLUME": 0.42}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert cfg.MASTER_VOLUME == pytest.approx(0.42)


def test_api_settings_post_clamps_above_max(client, clean_config):
    r = client.post(
        "/api/settings",
        data=json.dumps({"MASTER_VOLUME": 99.0}),
        content_type="application/json",
    )
    assert r.status_code == 200
    # Should be clamped to the meta-defined max of 1.0
    assert cfg.MASTER_VOLUME <= 1.0


def test_api_settings_post_clamps_below_min(client, clean_config):
    r = client.post(
        "/api/settings",
        data=json.dumps({"MASTER_VOLUME": -5.0}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert cfg.MASTER_VOLUME >= 0.0


def test_api_settings_post_ignores_unknown_keys(client, clean_config):
    original = cfg.MASTER_VOLUME
    r = client.post(
        "/api/settings",
        data=json.dumps({"NOT_A_REAL_KEY": 123}),
        content_type="application/json",
    )
    assert r.status_code == 200
    assert cfg.MASTER_VOLUME == pytest.approx(original)


# ── Preset API ────────────────────────────────────────────────────────────────

def test_api_preset_get_returns_expected_fields(client):
    r = client.get("/api/preset")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "preset" in data
    assert "presets" in data
    assert "custom" in data
    assert "ambient" in data["presets"]


def test_api_preset_post_valid_switches_preset(client, clean_config):
    r = client.post(
        "/api/preset",
        data=json.dumps({"preset": "glitchy"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["ok"] is True
    assert data["preset"] == "glitchy"
    assert cfg.LAYER_GLITCH_LEVEL == pytest.approx(0.65)


def test_api_preset_post_invalid_returns_400(client):
    r = client.post(
        "/api/preset",
        data=json.dumps({"preset": "does_not_exist"}),
        content_type="application/json",
    )
    assert r.status_code == 400


def test_api_preset_reset_restores_factory(client, clean_config):
    # Change a setting
    client.post(
        "/api/settings",
        data=json.dumps({"MASTER_VOLUME": 0.1}),
        content_type="application/json",
    )
    # Reset
    r = client.post("/api/preset/reset")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["ok"] is True
    assert "settings" in data


def test_api_preset_clone_creates_custom(client, clean_config):
    r = client.post(
        "/api/preset/clone",
        data=json.dumps({"source": "ambient"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["ok"] is True
    new_name = data["name"]
    assert new_name in cfg.PRESETS
    assert new_name in web._custom_presets
    assert new_name in data.get("custom", [])


def test_api_preset_clone_increments_counter(client, clean_config):
    # Clone twice; second clone should get a different name
    r1 = client.post(
        "/api/preset/clone",
        data=json.dumps({"source": "ambient"}),
        content_type="application/json",
    )
    r2 = client.post(
        "/api/preset/clone",
        data=json.dumps({"source": "ambient"}),
        content_type="application/json",
    )
    name1 = json.loads(r1.data)["name"]
    name2 = json.loads(r2.data)["name"]
    assert name1 != name2


def test_api_preset_delete_custom_succeeds(client, clean_config):
    # Clone first
    client.post(
        "/api/preset/clone",
        data=json.dumps({"source": "ambient"}),
        content_type="application/json",
    )
    # Delete
    r = client.post("/api/preset/delete")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["ok"] is True
    assert data["preset"] == "ambient"


def test_api_preset_delete_builtin_returns_400(client):
    # Ensure active preset is a built-in
    web._active_preset = "ambient"
    r = client.post("/api/preset/delete")
    assert r.status_code == 400


# ── State persistence ─────────────────────────────────────────────────────────

def test_state_written_on_preset_switch(client, tmp_state_file, clean_config):
    client.post(
        "/api/preset",
        data=json.dumps({"preset": "glitchy"}),
        content_type="application/json",
    )
    assert tmp_state_file.exists()
    state = json.loads(tmp_state_file.read_text())
    assert state["active_preset"] == "glitchy"


def test_state_written_on_settings_change(client, tmp_state_file, clean_config):
    client.post(
        "/api/settings",
        data=json.dumps({"MASTER_VOLUME": 0.33}),
        content_type="application/json",
    )
    assert tmp_state_file.exists()
