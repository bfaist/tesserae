"""The "More options" disclosure in a live browser (Playwright Chromium).

Server-rendered markup is covered in test_options_more.py; this drives the
live side in static/components.js: show_if toggling an advanced option inside
the folded section, the section hiding and reappearing with its options, the
open state remembered across a reload, and the section opening by itself for
a validation error or a link that targets one of its fields. Folded or
hidden, every field must still be in the submitted form.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask, render_template

from app.main import REPO_ROOT, create_app


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

_LAYOUT = {
    "name": "layout",
    "type": "select",
    "label": "Layout",
    "default": "list",
    "choices": [{"value": v, "label": v} for v in ("list", "cluster")],
}
_GAP = {
    "name": "cluster_gap",
    "type": "number",
    "label": "Gap",
    "default": 4,
    "show_if": {"layout": "cluster"},
    "advanced": True,
}
OPTS: list[dict[str, Any]] = [
    _LAYOUT,
    {"name": "title", "type": "string", "label": "Title", "default": ""},
    {
        "name": "refresh",
        "type": "number",
        "label": "Refresh",
        "default": 15,
        "min": 1,
        "max": 60,
        "advanced": True,
    },
    _GAP,
    {"name": "typeface", "type": "string", "label": "Typeface", "default": "", "advanced": True},
]


def _page(tmp_path: Path, opts: list[dict[str, Any]], name: str) -> Path:
    """The real drawer fragment with the real stylesheet and script, as a
    file:// page."""
    app: Flask = create_app(testing=True, data_root=tmp_path / "data")
    with app.test_request_context():
        body = render_template(
            "panels_source_form.html", opts=opts, values={"layout": "list"}, sid="s1"
        )
    css = (REPO_ROOT / "static" / "style" / "forms.css").read_text(encoding="utf-8")
    script = (REPO_ROOT / "static" / "components.js").read_text(encoding="utf-8")
    path = tmp_path / f"{name}.html"
    path.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{css}</style></head>"
        f'<body><form id="f">{body}</form><script>{script}</script></body></html>',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


_FORM_JS = "() => Object.fromEntries(new FormData(document.getElementById('f')))"
_MORE = "[data-opt-more]"


def test_show_if_parent_reveals_an_advanced_option_inside_more_options(
    tmp_path: Path, browser: Any
) -> None:
    pg = browser.new_page()
    pg.goto(_page(tmp_path, OPTS, "more").as_uri())
    more = pg.locator(_MORE)
    assert more.is_visible()
    assert more.evaluate("(d) => d.open") is False, "closed by default"
    gap_wrap = pg.locator(f"{_MORE} .opt-cond")
    assert gap_wrap.evaluate("(w) => w.hidden") is True

    pg.select_option('[name="opt_layout"]', "cluster")
    assert gap_wrap.evaluate("(w) => w.hidden") is False
    pg.locator(f"{_MORE} > summary").click()
    assert pg.locator('[name="opt_cluster_gap"]').is_visible()
    assert pg.locator(f'{_MORE} [name="opt_cluster_gap"]').count() == 1

    pg.select_option('[name="opt_layout"]', "list")
    assert gap_wrap.evaluate("(w) => w.hidden") is True
    # Hidden or folded, every field is still submitted.
    pg.locator(f"{_MORE} > summary").click()
    form = pg.evaluate(_FORM_JS)
    assert form["opt_cluster_gap"] == "4" and form["opt_refresh"] == "15"
    assert "opt_typeface" in form


def test_section_hides_while_none_of_its_options_show(tmp_path: Path, browser: Any) -> None:
    pg = browser.new_page()
    pg.goto(_page(tmp_path, [_LAYOUT, _GAP], "cond").as_uri())
    more = pg.locator(_MORE)
    assert more.evaluate("(d) => d.hidden") is True
    assert "opt_cluster_gap" in pg.evaluate(_FORM_JS)
    pg.select_option('[name="opt_layout"]', "cluster")
    assert more.evaluate("(d) => d.hidden") is False
    pg.select_option('[name="opt_layout"]', "list")
    assert more.evaluate("(d) => d.hidden") is True


def test_open_state_is_remembered(tmp_path: Path, browser: Any) -> None:
    ctx = browser.new_context()
    pg = ctx.new_page()
    url = _page(tmp_path, OPTS, "remember").as_uri()
    pg.goto(url)
    pg.locator(f"{_MORE} > summary").click()
    pg.wait_for_function("() => localStorage.getItem('tesserae.optionsMore.open') === '1'")
    pg.reload()
    assert pg.locator(_MORE).evaluate("(d) => d.open") is True
    pg.locator(f"{_MORE} > summary").click()
    pg.wait_for_function("() => localStorage.getItem('tesserae.optionsMore.open') === '0'")
    pg.reload()
    assert pg.locator(_MORE).evaluate("(d) => d.open") is False
    ctx.close()


def test_opens_for_an_invalid_field_and_for_a_link(tmp_path: Path, browser: Any) -> None:
    ctx = browser.new_context()
    pg = ctx.new_page()
    path = _page(tmp_path, OPTS, "invalid")
    pg.goto(path.as_uri())
    # Folded away, so set it as a saved out-of-range value would arrive.
    pg.evaluate("() => { document.querySelector('[name=\"opt_refresh\"]').value = '500'; }")
    assert pg.locator(_MORE).evaluate("(d) => d.open") is False
    assert pg.evaluate("() => document.getElementById('f').reportValidity()") is False
    assert pg.locator(_MORE).evaluate("(d) => d.open") is True
    # Opened for an error, not by the person: nothing remembered.
    pg.wait_for_timeout(50)
    assert pg.evaluate("() => localStorage.getItem('tesserae.optionsMore.open')") is None

    pg2 = ctx.new_page()
    pg2.goto(path.as_uri() + "#src-s1-opt-typeface")
    assert pg2.locator(_MORE).evaluate("(d) => d.open") is True
    assert pg2.locator('[name="opt_typeface"]').is_visible()
    ctx.close()
