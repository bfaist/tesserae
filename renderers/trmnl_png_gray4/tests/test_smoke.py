"""trmnl_png_gray4 renderer smoke: composition PNG in, true 2-bit
greyscale PNG out (IHDR bit depth 2, colour type 0), on the same TRMNL
/api/display topic as the 1-bit and 16-grey variants."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app.main import REPO_ROOT
from app.png_gray import png_bit_depth, png_colour_type
from app.renderer_loader import discover
from app.state.page_store import Panel


@pytest.fixture
def trmnl_png_gray4(tmp_path):
    registry = discover(
        REPO_ROOT / "renderers",
        schema_path=REPO_ROOT / "schema" / "renderer.schema.json",
        data_root=tmp_path,
    )
    assert registry.errors == [], registry.errors
    renderer = registry.get("trmnl_png_gray4")
    assert renderer is not None
    return renderer


def _gradient_png(w: int, h: int) -> bytes:
    ramp = (np.arange(w) * 255 // max(1, w - 1)).astype(np.uint8)
    img = Image.fromarray(np.tile(ramp, (h, 1)), mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_manifest_matches_byos_wire_contract(trmnl_png_gray4) -> None:
    assert trmnl_png_gray4.extension == "png"
    assert trmnl_png_gray4.mime == "image/png"
    assert trmnl_png_gray4.device == "trmnl"
    assert trmnl_png_gray4.manifest["gamuts"] == ["gray_4"]


def test_output_is_two_bit_greyscale_png(trmnl_png_gray4) -> None:
    panel = Panel(w=800, h=480, gamut="gray_4", native_w=800, native_h=480, native_declared=True)
    out = trmnl_png_gray4.transform(
        _gradient_png(800, 480), panel=panel, settings=trmnl_png_gray4.settings_defaults()
    )
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
    assert png_bit_depth(out) == 2
    assert png_colour_type(out) == 0
    img = Image.open(io.BytesIO(out))
    img.load()
    assert img.size == (800, 480)
    assert set(np.asarray(img).reshape(-1).tolist()) == {0, 85, 170, 255}


def test_none_dither_snaps_to_four_levels(trmnl_png_gray4) -> None:
    panel = Panel(w=8, h=1, gamut="gray_4")
    src = Image.new("RGB", (8, 1))
    for x, v in enumerate((0, 20, 80, 100, 160, 180, 240, 255)):
        src.putpixel((x, 0), (v, v, v))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    out = trmnl_png_gray4.transform(buf.getvalue(), panel=panel, settings={"dither": "none"})
    img = Image.open(io.BytesIO(out))
    img.load()
    assert np.asarray(img).reshape(-1).tolist() == [0, 0, 85, 85, 170, 170, 255, 255]


def test_odd_width_rows_are_padded(trmnl_png_gray4) -> None:
    """A width that is not a multiple of four still decodes at its own
    size: each scanline is padded to a whole byte."""
    panel = Panel(w=7, h=3, gamut="gray_4")
    out = trmnl_png_gray4.transform(_gradient_png(7, 3), panel=panel, settings={"dither": "none"})
    img = Image.open(io.BytesIO(out))
    img.load()
    assert img.size == (7, 3)
    assert png_bit_depth(out) == 2


def test_calibration_ramp_still_writes_level_indices(trmnl_png_gray4) -> None:
    """A measured ramp moves which level a pixel lands on, never the
    samples: the PNG still carries indices 0..3."""
    panel = Panel(w=64, h=2, gamut="gray_4")
    settings = {"dither": "none", "_gray_ramp": ("#1E1E1E", "#6E6E6E", "#B4B4B4", "#E8E8E8")}
    out = trmnl_png_gray4.transform(_gradient_png(64, 2), panel=panel, settings=settings)
    img = Image.open(io.BytesIO(out))
    img.load()
    assert set(np.asarray(img).reshape(-1).tolist()) <= {0, 85, 170, 255}
    assert png_bit_depth(out) == 2


def test_payload_is_selfcontained_url(trmnl_png_gray4) -> None:
    payload = trmnl_png_gray4.payload("abc123", "http://h:5555/", settings={})
    assert payload == {"url": "http://h:5555/renders/abc123.png"}
