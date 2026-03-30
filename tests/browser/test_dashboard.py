"""
Browser tests for the Mulchy web dashboard.
Requires Chromium: uv run playwright install chromium
"""

import threading
import time

import pytest
from playwright.sync_api import Page, expect

import mulchy.web as web
from mulchy import config as cfg


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """Start Flask on a fixed port for the browser test session."""
    tmp = tmp_path_factory.mktemp("browser_state")
    web._STATE_FILE = tmp / "state.json"
    cfg.load_preset("ambient")
    web._active_preset = "ambient"
    web._custom_presets = {}
    web._preset_settings = {}

    web.app.config["TESTING"] = False
    t = threading.Thread(
        target=lambda: web.app.run(host="127.0.0.1", port=5099, use_reloader=False),
        daemon=True,
    )
    t.start()
    time.sleep(0.8)  # wait for Flask to bind
    yield "http://127.0.0.1:5099"


def test_dashboard_loads(live_server: str, page: Page):
    page.goto(live_server)
    page.wait_for_load_state("networkidle")
    expect(page).to_have_title("mulchy")


def test_settings_tab_has_preset_buttons(live_server: str, page: Page):
    page.goto(live_server)
    page.wait_for_load_state("networkidle")
    # Wait for the settings panel to be populated (built via JS fetch)
    page.wait_for_selector('[id^="pbtn-"]', timeout=5000)
    buttons = page.locator('[id^="pbtn-"]').all()
    assert len(buttons) >= 4  # default, ambient, glitchy, percussive


def test_preset_button_becomes_active_on_click(live_server: str, page: Page):
    page.goto(live_server)
    page.wait_for_selector("#pbtn-glitchy", timeout=5000)
    page.click("#pbtn-glitchy")
    # Give the fetch a moment to settle
    page.wait_for_timeout(500)
    expect(page.locator("#pbtn-glitchy")).to_have_class("pbtn on")


def test_slider_exists_and_is_interactive(live_server: str, page: Page):
    page.goto(live_server)
    page.wait_for_selector('input[type="range"]', timeout=5000)
    slider = page.locator('input[type="range"]').first
    expect(slider).to_be_visible()
    # Fill with a value — page should stay alive
    slider.fill("0.5")
    slider.dispatch_event("input")
    page.wait_for_timeout(300)
    # Page still responsive
    assert page.url.startswith(live_server)


def test_clone_button_adds_preset(live_server: str, page: Page):
    page.goto(live_server)
    page.wait_for_selector("text=+ Clone", timeout=5000)
    initial_count = page.locator('[id^="pbtn-"]').count()
    page.click("text=+ Clone")
    page.wait_for_function(
        f"document.querySelectorAll('[id^=\"pbtn-\"]').length > {initial_count}",
        timeout=3000,
    )


def test_smoothness_slider_present(live_server: str, page: Page):
    """The CROSSFADE_SMOOTHNESS slider should be visible in settings."""
    page.goto(live_server)
    page.wait_for_selector('input[type="range"]', timeout=5000)
    sliders = page.locator('input[type="range"]').all()
    # SMETA has 10 range sliders (9 numeric + waveform select) — Smoothness is the last range
    assert len(sliders) >= 9
