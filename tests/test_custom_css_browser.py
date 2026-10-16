"""A dashboard's custom CSS reaches inside a widget, in a live Chromium.

Widgets render into a shadow root, so the page-level ``<style>`` alone would
never hide a widget's title row; composer.js copies the dashboard's CSS into
every shadow root after the widget renders (#366). Only a real browser can
show that the copy lands and that the rule takes effect, so this runs one.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask

from app.main import REPO_ROOT, create_app
from app.state.page_store import Cell, Page, Panel


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not installed"
)

CSS = '[data-widget="year_progress"] .w-title { display: none; }\n.panel { outline: 3px solid red; }'


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    """The app on a loopback port with one dashboard holding a widget that
    needs no network, and custom CSS that hides that widget's title row."""
    from werkzeug.serving import make_server

    app: Flask = create_app(
        testing=False,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
        devices_dir=REPO_ROOT / "devices",
    )
    app.config["TESTING"] = True
    store = app.config["SETTINGS_STORE"]
    store.patch_section("app", {"onboarded": True})
    store.patch_section("auth", {"disabled": True})
    app.config["PAGE_STORE"].save(
        Page(
            id="css",
            name="CSS",
            panel=Panel(w=800, h=480),
            cells=[Cell(id="c1", plugin="year_progress", x=0, y=0, w=800, h=480)],
            custom_css=CSS,
        )
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)


def test_custom_css_hides_a_widget_title_inside_its_shadow_root(live_server: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{live_server}/compose/css", wait_until="networkidle")
        page.wait_for_function("window.__tesseraeComposed === true")
        result = page.evaluate(
            """() => {
              const root = document.querySelector('.cell .cell-content').shadowRoot;
              const copy = root.querySelector('style#tesserae-custom-css');
              const title = root.querySelector('.w-title');
              return {
                copied: copy ? copy.textContent : null,
                titleDisplay: title ? getComputedStyle(title).display : 'missing',
                pageOutline: getComputedStyle(document.querySelector('.panel')).outlineStyle,
              };
            }"""
        )
        browser.close()

    assert errors == []
    assert result["copied"] == CSS
    assert result["titleDisplay"] == "none"
    assert result["pageOutline"] == "solid"
