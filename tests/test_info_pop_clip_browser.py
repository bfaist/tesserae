"""The info-pop help popover stays inside whatever clips it (live Chromium).

A popover opens below its (i) icon, anchored to the icon's left, and used to
flip to right-anchored only when it would run off the viewport. A settings
card clips its overflow, so a popover near the card's right edge opened half
out of sight with no way to read it (#363). The popover now measures the room
every clipping ancestor leaves it, flips inside that, and if there is less
room than its width, shrinks to fit and pins to the room's left edge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.main import REPO_ROOT


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

_TEXT = (
    "Offset of the wall-clock grid. 00:00 lands wakes on the hour boundaries; "
    "00:05 shifts the whole grid five minutes later."
)


def _wrap(idx: str) -> str:
    return (
        f'<span class="info-pop-wrap" id="{idx}">'
        '<button type="button" class="info-pop" aria-expanded="false">i</button>'
        f'<span class="info-pop-content" role="tooltip" hidden>{_TEXT}</span></span>'
    )


@pytest.fixture
def page_path(tmp_path: Path) -> Path:
    css = (REPO_ROOT / "static" / "style" / "base.css").read_text(encoding="utf-8")
    script = (REPO_ROOT / "static" / "info-pop.js").read_text(encoding="utf-8")
    path = tmp_path / "info-pop.html"
    path.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{css}</style>"
        "<style>body { margin: 0; padding: 40px; }"
        " .card { width: 420px; overflow: hidden; border: 1px solid #999; padding: 12px; }"
        " .narrow { width: 200px; overflow: hidden; border: 1px solid #999; padding: 12px; }"
        " .row { display: flex; justify-content: flex-end; }</style>"
        "</head><body>"
        # A card wide enough for the popover, with the icon at its right edge.
        f'<div class="card" id="card"><div class="row">Anchor {_wrap("edge")}</div></div>'
        # A card narrower than the popover's minimum width.
        f'<div class="narrow" id="narrow"><div class="row">Anchor {_wrap("tight")}</div></div>'
        f"<script>{script}</script>"
        "</body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def browser() -> Iterator[object]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _rects(pg, wrap_id: str, box_id: str):  # type: ignore[no-untyped-def]
    return pg.evaluate(
        "([w, b]) => { const c = document.querySelector('#' + w + ' .info-pop-content').getBoundingClientRect();"
        " const k = document.getElementById(b).getBoundingClientRect();"
        " return { left: c.left, right: c.right, width: c.width, boxLeft: k.left, boxRight: k.right }; }",
        [wrap_id, box_id],
    )


def test_popover_at_a_cards_right_edge_flips_inside_the_card(browser, page_path: Path) -> None:  # type: ignore[no-untyped-def]
    pg = browser.new_page(viewport={"width": 1280, "height": 700})
    pg.goto(page_path.as_uri())
    pg.click("#edge .info-pop")
    pg.wait_for_timeout(100)
    r = _rects(pg, "edge", "card")
    assert r["right"] <= r["boxRight"], "the popover ran past the card's clipped edge"
    assert r["left"] >= r["boxLeft"]
    assert r["width"] >= 200, "it had room, so it must not have been shrunk"


def test_popover_in_a_card_narrower_than_itself_shrinks_to_fit(browser, page_path: Path) -> None:  # type: ignore[no-untyped-def]
    pg = browser.new_page(viewport={"width": 1280, "height": 700})
    pg.goto(page_path.as_uri())
    pg.click("#tight .info-pop")
    pg.wait_for_timeout(100)
    r = _rects(pg, "tight", "narrow")
    assert r["left"] >= r["boxLeft"]
    assert r["right"] <= r["boxRight"]
    # Every word is still on screen: the text wraps inside the narrower box.
    assert pg.evaluate(
        "() => { const c = document.querySelector('#tight .info-pop-content');"
        " return c.scrollWidth <= c.clientWidth + 1; }"
    )


def test_closing_clears_the_fit_so_the_next_open_measures_afresh(browser, page_path: Path) -> None:  # type: ignore[no-untyped-def]
    pg = browser.new_page(viewport={"width": 1280, "height": 700})
    pg.goto(page_path.as_uri())
    pg.click("#tight .info-pop")
    pg.wait_for_timeout(100)
    pg.keyboard.press("Escape")
    assert (
        pg.evaluate("() => document.querySelector('#tight .info-pop-content').style.maxWidth") == ""
    )
    assert (
        pg.evaluate("() => document.querySelector('#tight .info-pop-content').style.minWidth") == ""
    )
    assert pg.evaluate("() => document.querySelector('#tight .info-pop-content').style.left") == ""
