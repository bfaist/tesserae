"""esp32_gray_bin renderer smoke: composition PNG in, raw 4-bpp
grayscale packed buffer out. Byte-contract checks so a firmware update
doesn't get surprised by a silent format drift."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.main import REPO_ROOT
from app.renderer_loader import discover
from app.state.page_store import Panel


@pytest.fixture
def esp32_gray_bin(tmp_path):
    registry = discover(
        REPO_ROOT / "renderers",
        schema_path=REPO_ROOT / "schema" / "renderer.schema.json",
        data_root=tmp_path,
    )
    assert registry.errors == [], registry.errors
    renderer = registry.get("esp32_gray_bin")
    assert renderer is not None
    return renderer


@pytest.fixture
def composition_png() -> bytes:
    """A 1872x1404 grayscale-friendly gradient. Sized at the E1003's
    native dims so the renderer skips the resize path and we exercise
    the pack step directly."""
    img = Image.new("RGB", (1872, 1404))
    for y in range(1404):
        for x in range(1872):
            g = (x * 255) // 1872
            img.putpixel((x, y), (g, g, g))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_esp32_gray_bin_manifest_fields(esp32_gray_bin) -> None:
    assert esp32_gray_bin.device == "esp32"
    assert esp32_gray_bin.orientation == "composition"
    assert esp32_gray_bin.extension == "bin"
    assert esp32_gray_bin.mime == "application/octet-stream"
    assert esp32_gray_bin.retain is True
    assert esp32_gray_bin.topic == "tesserae/esp32/frame/bin"


def test_esp32_gray_bin_e1003_byte_count(esp32_gray_bin, composition_png) -> None:
    """The E1003 case: 1872x1404 panel -> exactly 1314144 bytes. The
    firmware's image_decoder demands this exact count and refuses to
    paint on any mismatch."""
    panel = Panel(w=1872, h=1404, gamut="mono", native_w=1872, native_h=1404)
    artifact = esp32_gray_bin.transform(
        composition_png, panel=panel, settings=esp32_gray_bin.settings_defaults()
    )
    assert len(artifact) == 1872 * 1404 // 2 == 1314144


def test_esp32_gray_bin_white_image_packs_to_all_ff() -> None:
    """A pure-white image quantised to 16-level gray with no dither must
    produce all 0xFF bytes (both nibbles at max)."""
    from app.quantizer import pack_to_panel_bin_4bpp_gray

    img = Image.new("RGB", (8, 4), (255, 255, 255))
    out = pack_to_panel_bin_4bpp_gray(img, width=8, height=4, dither="none", contrast=1.0)
    assert out == b"\xff" * (8 * 4 // 2)


def test_esp32_gray_bin_black_image_packs_to_all_00() -> None:
    """Symmetric: pure-black image, all 0x00."""
    from app.quantizer import pack_to_panel_bin_4bpp_gray

    img = Image.new("RGB", (8, 4), (0, 0, 0))
    out = pack_to_panel_bin_4bpp_gray(img, width=8, height=4, dither="none", contrast=1.0)
    assert out == b"\x00" * (8 * 4 // 2)


def test_esp32_gray_bin_high_nibble_is_left_pixel() -> None:
    """The spec says HIGH nibble = LEFT pixel. A 2x1 image with white on
    the left and black on the right must pack to 0xF0. If this test
    ever fails as 0x0F, the packer's nibble order is swapped and the
    panel would paint mirrored per-pair."""
    from app.quantizer import pack_to_panel_bin_4bpp_gray

    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (255, 255, 255))  # left = white
    img.putpixel((1, 0), (0, 0, 0))  # right = black
    out = pack_to_panel_bin_4bpp_gray(img, width=2, height=1, dither="none", contrast=1.0)
    assert out == b"\xf0", f"expected 0xf0 (left=white=high nibble), got {out.hex()}"


def test_esp32_gray_bin_grayscale_landscape_flip() -> None:
    """A tiny colour test to prove the grayscale-conversion path runs
    (pure red should land as a midtone, not the full-white 0xFF)."""
    from app.quantizer import pack_to_panel_bin_4bpp_gray

    img = Image.new("RGB", (4, 2), (255, 0, 0))
    out = pack_to_panel_bin_4bpp_gray(img, width=4, height=2, dither="none", contrast=1.0)
    # PIL's L conversion uses ITU-R 601 weights: R*0.299. So red 255
    # -> gray ~76, which quantises to index 4 or 5 out of 16 (0..15).
    # Both nibbles the same -> byte in 0x44..0x55 range.
    for byte in out:
        assert 0x30 <= byte <= 0x66, f"unexpected byte {byte:#x} for red input"


def test_esp32_gray_bin_output_is_raw_not_png(esp32_gray_bin, composition_png) -> None:
    """Belt-and-braces: the output must NOT start with the PNG magic
    (0x89 0x50 0x4E 0x47). The firmware refuses to paint if it sees a
    PNG-shaped payload where it expected raw bytes."""
    panel = Panel(w=1872, h=1404, gamut="mono", native_w=1872, native_h=1404)
    artifact = esp32_gray_bin.transform(
        composition_png, panel=panel, settings=esp32_gray_bin.settings_defaults()
    )
    assert artifact[:4] != b"\x89PNG"


def test_gray_8_levels_pack_to_nibbles_that_halve_to_0_through_7() -> None:
    """The Inkplate 10 contract: its firmware halves every nibble to the
    Inkplate library's 8 levels. Each gray_8 level must land on the nibble
    0, 2, 4, 6, 9, 11, 13, 15, which ``n >> 1`` turns back into 0..7."""
    from app.quantizer import GRAY_8_NIBBLES, GRAY_8_PALETTE, pack_to_panel_bin_4bpp_gray

    assert GRAY_8_NIBBLES == (0, 2, 4, 6, 9, 11, 13, 15)
    assert [n >> 1 for n in GRAY_8_NIBBLES] == list(range(8))
    img = Image.new("RGB", (8, 1))
    for x, rgb in enumerate(GRAY_8_PALETTE):
        img.putpixel((x, 0), rgb)
    out = pack_to_panel_bin_4bpp_gray(img, width=8, height=1, dither="none", levels=8)
    nibbles = [n for b in out for n in (b >> 4, b & 0x0F)]
    assert nibbles == [0, 2, 4, 6, 9, 11, 13, 15]
    assert [n >> 1 for n in nibbles] == list(range(8))


def test_gray_8_panel_never_emits_an_odd_level_between_pairs(esp32_gray_bin) -> None:
    """A gray_8 panel's frame only carries the 8 level nibbles, so no
    pixel is aimed at a grey the firmware would merge with its neighbour.
    Same byte count as a 16-level frame (the Inkplate's 495000)."""
    import numpy as np

    from app.quantizer import GRAY_8_NIBBLES

    ramp = (np.arange(1200) * 255 // 1199).astype(np.uint8)
    img = Image.fromarray(np.tile(ramp, (825, 1)), mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    panel = Panel(w=1200, h=825, gamut="gray_8", native_w=1200, native_h=825)
    artifact = esp32_gray_bin.transform(
        buf.getvalue(), panel=panel, settings=esp32_gray_bin.settings_defaults()
    )
    assert len(artifact) == 1200 * 825 // 2 == 495000
    seen = {n for b in artifact for n in (b >> 4, b & 0x0F)}
    assert seen <= set(GRAY_8_NIBBLES)
    assert len(seen) == 8


def test_gray_16_panel_keeps_sixteen_levels(esp32_gray_bin) -> None:
    """The 16-level path is untouched: a gradient on a gray_16 panel uses
    nibbles outside the gray_8 set."""
    from app.quantizer import GRAY_8_NIBBLES

    img = Image.new("RGB", (64, 2))
    for x in range(64):
        g = (x * 255) // 63
        img.putpixel((x, 0), (g, g, g))
        img.putpixel((x, 1), (g, g, g))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    panel = Panel(w=64, h=2, gamut="gray_16", native_w=64, native_h=2)
    settings = {**esp32_gray_bin.settings_defaults(), "dither": "none"}
    artifact = esp32_gray_bin.transform(buf.getvalue(), panel=panel, settings=settings)
    seen = {n for b in artifact for n in (b >> 4, b & 0x0F)}
    assert seen - set(GRAY_8_NIBBLES)
