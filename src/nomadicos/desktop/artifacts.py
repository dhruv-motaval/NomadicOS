"""Bounded screenshot artifact encoding (Phase 12, SPEC §26).

Pure-stdlib PNG writer: the capture evidence lives in the task workspace as
a standard image file; TaskState and execution evidence carry only bounded
metadata (dimensions, size, hash, artifact reference) — never pixel data.
"""

from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode top-down 32bpp BGRA pixel rows as a truecolor PNG (RGBA)."""
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid capture dimensions {width}x{height}")
    stride = width * 4
    expected = stride * height
    if len(bgra) < expected:
        raise ValueError(f"capture buffer too small: {len(bgra)} < {expected}")
    rgba = bytearray(bgra[:expected])
    rgba[0::4], rgba[2::4] = bgra[2:expected:4], bgra[0:expected:4]
    scanlines = b"".join(
        b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(scanlines, 6))
        + _chunk(b"IEND", b"")
    )
