"""Minimal pure-Python PNG decoding, enough to show a captcha in a terminal.

Pillow is a compiled extension that pip on iOS a-Shell cannot install, so
this handles the common case directly: non-interlaced, 8-bit grayscale,
RGB, palette, grayscale+alpha, or RGBA images.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Channels per pixel for each supported color type.
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
Pixel = tuple[int, int, int]


class PngError(ValueError):
    """The data is not a PNG this decoder supports."""


@dataclass(frozen=True)
class Image:
    """Decoded image as rows of opaque RGB pixels."""

    width: int
    height: int
    rows: list[list[Pixel]]

    def pixel(self, x: int, y: int) -> Pixel:
        """Return the RGB pixel at column ``x``, row ``y``."""

        return self.rows[y][x]


def decode_png(data: bytes, *, background: Pixel = (255, 255, 255)) -> Image:
    """Decode PNG bytes, compositing any transparency over ``background``."""

    if not data.startswith(PNG_SIGNATURE):
        raise PngError("not a PNG file")

    header: tuple[int, ...] | None = None
    palette = b""
    transparency = b""
    compressed = bytearray()
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break

    if header is None:
        raise PngError("PNG is missing its IHDR chunk")
    width, height, depth, color_type, _compression, _filter, interlace = header
    if depth != 8 or color_type not in CHANNELS:
        raise PngError(f"unsupported PNG: bit depth {depth}, color type {color_type}")
    if interlace:
        raise PngError("interlaced PNGs are not supported")

    channels = CHANNELS[color_type]
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as error:
        raise PngError(f"corrupt PNG data: {error}") from error
    scanlines = _unfilter(raw, width, height, channels)

    rows = [
        [
            _to_rgb(
                line[x * channels : (x + 1) * channels],
                color_type,
                palette,
                transparency,
                background,
            )
            for x in range(width)
        ]
        for line in scanlines
    ]
    return Image(width=width, height=height, rows=rows)


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> list[bytes]:
    """Undo the per-scanline PNG filters (None, Sub, Up, Average, Paeth)."""

    stride = width * channels
    if len(raw) < height * (stride + 1):
        raise PngError("PNG image data is truncated")

    lines: list[bytes] = []
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if kind == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], up_left)) & 0xFF
        elif kind != 0:
            raise PngError(f"unknown PNG filter type {kind}")
        lines.append(bytes(line))
        previous = line
    return lines


def _paeth(left: int, up: int, up_left: int) -> int:
    """Return the Paeth predictor for one byte."""

    estimate = left + up - up_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_up_left = abs(estimate - up_left)
    if distance_left <= distance_up and distance_left <= distance_up_left:
        return left
    if distance_up <= distance_up_left:
        return up
    return up_left


def _to_rgb(
    values: bytes,
    color_type: int,
    palette: bytes,
    transparency: bytes,
    background: Pixel,
) -> Pixel:
    """Convert one pixel's channel bytes into an opaque RGB triple."""

    alpha = 255
    if color_type == 0:
        red = green = blue = values[0]
    elif color_type == 2:
        red, green, blue = values
    elif color_type == 3:
        index = values[0]
        red, green, blue = palette[index * 3 : index * 3 + 3] or b"\x00\x00\x00"
        if index < len(transparency):
            alpha = transparency[index]
    elif color_type == 4:
        red = green = blue = values[0]
        alpha = values[1]
    else:
        red, green, blue, alpha = values

    if alpha == 255:
        return red, green, blue
    return (
        _blend(red, background[0], alpha),
        _blend(green, background[1], alpha),
        _blend(blue, background[2], alpha),
    )


def _blend(foreground: int, background: int, alpha: int) -> int:
    """Composite one channel over a background with 8-bit alpha."""

    return (foreground * alpha + background * (255 - alpha)) // 255
