import struct
import unittest
import zlib
from pathlib import Path

from rich.console import Console

from agecat.png import PngError, decode_png
from agecat.terminal_image import UPPER_HALF_BLOCK, render_image

FIXTURE = Path(__file__).parent / "fixtures" / "captcha.png"


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def encode_png(
    rows: list[bytes],
    width: int,
    color_type: int,
    channels: int,
    *,
    filters: list[int] | None = None,
    extra: bytes = b"",
    depth: int = 8,
    interlace: int = 0,
) -> bytes:
    """Encode raw scanlines, applying the requested filter per row."""

    filters = filters or [0] * len(rows)
    raw = bytearray()
    previous = bytes(width * channels)
    for row, kind in zip(rows, filters, strict=True):
        filtered = bytearray()
        for i, value in enumerate(row):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            predictor = {
                0: 0,
                1: left,
                2: up,
                3: (left + up) >> 1,
                4: paeth(left, up, up_left),
            }[kind]
            filtered.append((value - predictor) & 0xFF)
        raw += bytes([kind]) + filtered
        previous = row
    header = struct.pack(">IIBBBBB", width, len(rows), depth, color_type, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + extra
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    options = [
        (abs(estimate - left), 0, left),
        (abs(estimate - up), 1, up),
        (abs(estimate - up_left), 2, up_left),
    ]
    return min(options)[2]


class PngTests(unittest.TestCase):
    def test_real_captcha_matches_reference_pixels(self):
        image = decode_png(FIXTURE.read_bytes())

        self.assertEqual((image.width, image.height), (120, 36))
        # Reference values decoded by ffmpeg; the fixture uses Sub, Up and Paeth filters.
        self.assertEqual(image.pixel(0, 0), (231, 231, 231))
        self.assertEqual(image.pixel(5, 17), (219, 215, 220))
        self.assertEqual(image.pixel(13, 10), (143, 70, 174))
        self.assertEqual(image.pixel(119, 35), (239, 239, 239))

    def test_every_filter_type_round_trips_for_rgb(self):
        rows = [
            bytes((x * 7 + y * 31 + c * 3) % 256 for x in range(5) for c in range(3))
            for y in range(5)
        ]

        image = decode_png(encode_png(rows, 5, 2, 3, filters=[0, 1, 2, 3, 4]))

        for y in range(5):
            for x in range(5):
                self.assertEqual(image.pixel(x, y), tuple(rows[y][x * 3 : x * 3 + 3]))

    def test_grayscale_palette_and_alpha_types(self):
        gray = decode_png(encode_png([bytes([0, 128, 255])], 3, 0, 1))
        self.assertEqual(gray.rows[0], [(0, 0, 0), (128, 128, 128), (255, 255, 255)])

        palette = chunk(b"PLTE", bytes([255, 0, 0, 0, 0, 255])) + chunk(b"tRNS", bytes([255, 0]))
        indexed = decode_png(encode_png([bytes([0, 1])], 2, 3, 1, extra=palette))
        # Index 1 is fully transparent, so it shows the white background.
        self.assertEqual(indexed.rows[0], [(255, 0, 0), (255, 255, 255)])

        gray_alpha = decode_png(encode_png([bytes([0, 0])], 1, 4, 2), background=(100, 100, 100))
        self.assertEqual(gray_alpha.rows[0], [(100, 100, 100)])

        rgba = decode_png(encode_png([bytes([200, 0, 0, 128])], 1, 6, 4), background=(0, 0, 0))
        self.assertEqual(rgba.rows[0], [(100, 0, 0)])

    def test_unsupported_input_raises(self):
        with self.assertRaises(PngError):
            decode_png(b"GIF89a")
        with self.assertRaisesRegex(PngError, "bit depth"):
            # Two bytes per row as far as the test encoder knows; the decoder rejects depth first.
            decode_png(encode_png([bytes(2)], 2, 0, 1, depth=16))
        with self.assertRaisesRegex(PngError, "interlaced"):
            decode_png(encode_png([bytes(1)], 1, 0, 1, interlace=1))
        with self.assertRaisesRegex(PngError, "IHDR"):
            decode_png(b"\x89PNG\r\n\x1a\n" + chunk(b"IEND", b""))


class TerminalImageTests(unittest.TestCase):
    def test_render_fits_width_and_keeps_aspect(self):
        image = decode_png(FIXTURE.read_bytes())

        text = render_image(image, max_width=60)

        lines = text.plain.rstrip("\n").split("\n")
        self.assertEqual({len(line) for line in lines}, {60})
        # 120x36 scaled to 60 wide is 18 pixels tall: 9 rows of half blocks.
        self.assertEqual(len(lines), 9)
        self.assertTrue(set("".join(lines)) <= {UPPER_HALF_BLOCK})

    def test_render_never_upscales_past_the_image(self):
        image = decode_png(encode_png([bytes([0, 255])], 2, 0, 1))

        text = render_image(image, max_width=80)

        self.assertEqual(text.plain, UPPER_HALF_BLOCK * 2 + "\n")

    def test_cells_carry_top_and_bottom_pixel_colors(self):
        image = decode_png(encode_png([bytes([255, 0, 0]), bytes([0, 0, 255])], 1, 2, 3))
        console = Console(force_terminal=True, color_system="truecolor", width=10)

        with console.capture() as captured:
            console.print(render_image(image, max_width=10), end="")

        # Red foreground over blue background.
        self.assertIn("38;2;255;0;0", captured.get())
        self.assertIn("48;2;0;0;255", captured.get())


if __name__ == "__main__":
    unittest.main()
