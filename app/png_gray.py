"""Low-bit-depth greyscale PNG encoding (PNG colour type 0, bit depth 1/2/4/8).

Pillow writes greyscale PNGs at 1 bit (mode ``1``) or 8 bits (mode ``L``),
and its sub-byte paths only exist for palette images (colour type 3). Some
clients want a true greyscale PNG at 2 or 4 bits per pixel: TRMNL firmware
1.6.0+ decodes a 2-bit greyscale PNG and paints its 4 shades on the 7.5"
panels, and TRMNL documents a 4-bit greyscale PNG for the TRMNL X's 16
greys. Both want colour type 0, so the samples are grey levels and no
palette lookup happens on the device.

The encoder here is the plain PNG container: an ``IHDR`` chunk, one
``IDAT`` with zlib-deflated scanlines (filter type 0 on every row), and
``IEND``. Rows are packed MSB first and padded to a whole byte, as the PNG
spec requires. Input is a 2-D array of *level indices* (0 .. 2**bits - 1),
not 8-bit grey values: a decoder scales sample ``k`` to ``k * 255 / (2**bits
- 1)`` itself, so the index is what goes on the wire.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence

import numpy as np
from PIL import Image

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SUPPORTED_DEPTHS = (1, 2, 4, 8)


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def encode_gray_png(indices: np.ndarray, *, bit_depth: int) -> bytes:
    """Encode a ``(height, width)`` array of grey level indices as a PNG
    with colour type 0 at ``bit_depth`` bits per sample.

    Values above the depth's top level are clamped rather than allowed to
    spill into the neighbouring pixel's bits."""
    if bit_depth not in _SUPPORTED_DEPTHS:
        raise ValueError(f"unsupported greyscale PNG bit depth {bit_depth}")
    if indices.ndim != 2:
        raise ValueError("indices must be a 2-D (height, width) array")
    height, width = indices.shape
    if width < 1 or height < 1:
        raise ValueError("image must be at least 1x1")
    top = (1 << bit_depth) - 1
    levels = np.clip(indices, 0, top).astype(np.uint8)

    per_byte = 8 // bit_depth
    if per_byte > 1:
        # Pad each row to a whole byte, then fold ``per_byte`` samples into
        # one, leftmost pixel in the most significant bits.
        padded_w = -(-width // per_byte) * per_byte
        if padded_w != width:
            levels = np.pad(levels, ((0, 0), (0, padded_w - width)))
        groups = levels.reshape(height, padded_w // per_byte, per_byte).astype(np.uint16)
        shifts = np.array(
            [bit_depth * (per_byte - 1 - i) for i in range(per_byte)], dtype=np.uint16
        )
        packed = (groups << shifts).sum(axis=2).astype(np.uint8)
    else:
        packed = levels

    # Filter type 0 (None) on every scanline.
    rows = np.concatenate([np.zeros((height, 1), dtype=np.uint8), packed], axis=1)
    raw = rows.tobytes()

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 0, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def level_indices(img: Image.Image, palette: Sequence[tuple[int, int, int]]) -> np.ndarray:
    """Map a quantised greyscale image back to the index of its palette
    entry, per pixel.

    ``quantize`` returns RGB pixels that sit exactly on palette entries; a
    measured (calibrated) ramp means those values are not evenly spaced, so
    the index cannot be recovered by scaling. Every 8-bit grey value is
    looked up against the nearest palette grey instead, which is exact for
    on-palette pixels and still well defined for any stray value."""
    # The palette's own greys as Pillow's ``L`` conversion sees them, so a
    # slightly tinted measured entry maps back to itself.
    swatch = Image.new("RGB", (len(palette), 1))
    swatch.putdata([tuple(int(c) for c in p[:3]) for p in palette])
    greys = np.asarray(swatch.convert("L"), dtype=np.int16).reshape(-1)
    values = np.arange(256, dtype=np.int16)
    lut = np.abs(values[:, None] - greys[None, :]).argmin(axis=1).astype(np.uint8)
    arr = np.asarray(img.convert("L"), dtype=np.uint8)
    return np.asarray(lut[arr], dtype=np.uint8)


def png_bit_depth(png: bytes) -> int:
    """The IHDR bit depth of a PNG (test and diagnostics helper)."""
    if png[:8] != _PNG_SIGNATURE or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return png[24]


def png_colour_type(png: bytes) -> int:
    """The IHDR colour type of a PNG (test and diagnostics helper)."""
    if png[:8] != _PNG_SIGNATURE or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return png[25]
