"""Draw small images in a terminal with colored half-block characters.

Each character cell shows two vertically stacked pixels: the upper half
block's foreground is the top pixel and its background is the bottom one.
That makes a 4-digit captcha readable on a phone-width terminal.
"""

from __future__ import annotations

from rich.color import Color
from rich.console import Console
from rich.style import Style
from rich.text import Text

from .png import Image, Pixel

UPPER_HALF_BLOCK = "▀"
# Leave room for the terminal's own margin so lines never wrap.
WIDTH_MARGIN = 2


def render_image(image: Image, *, max_width: int) -> Text:
    """Return ``image`` scaled to at most ``max_width`` columns as styled text."""

    columns = max(1, min(image.width, max_width))
    # Half blocks make each cell one pixel wide and two tall, i.e. roughly square.
    pixel_rows = max(2, round(image.height * columns / image.width))
    pixel_rows += pixel_rows % 2

    text = Text(no_wrap=True, overflow="crop")
    for row in range(0, pixel_rows, 2):
        for column in range(columns):
            top = _sample(image, column, row, columns, pixel_rows)
            bottom = _sample(image, column, row + 1, columns, pixel_rows)
            text.append(
                UPPER_HALF_BLOCK,
                style=Style(color=Color.from_rgb(*top), bgcolor=Color.from_rgb(*bottom)),
            )
        text.append("\n")
    return text


def print_image(image: Image, console: Console | None = None) -> None:
    """Print ``image`` fitted to the console width."""

    console = console or Console()
    console.print(render_image(image, max_width=console.width - WIDTH_MARGIN), end="")


def _sample(image: Image, column: int, row: int, columns: int, rows: int) -> Pixel:
    """Return the source pixel under one target pixel (nearest neighbor)."""

    x = min(image.width - 1, column * image.width // columns)
    y = min(image.height - 1, row * image.height // rows)
    return image.pixel(x, y)
