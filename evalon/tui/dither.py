"""Terminal-native dither primitives adapted from Dither Kit's paint model.

The browser kit paints low-resolution canvas cells with a 4x4 Bayer matrix,
orange opacity ramps, and a second bloom layer. A terminal has no alpha or blur,
so this module maps those ideas to Unicode density cells and true-color ramps.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.style import Style
from rich.text import Text
from textual.widgets import Button, Static

BAYER4: tuple[tuple[float, ...], ...] = tuple(
    tuple((value + 0.5) / 16 for value in row)
    for row in (
        (0, 8, 2, 10),
        (12, 4, 14, 6),
        (3, 11, 1, 9),
        (15, 7, 13, 5),
    )
)

ORANGE = (255, 122, 24)
ORANGE_HOT = (255, 176, 74)
BLACK = (3, 2, 1)
_CELLS = (" ", "░", "▒", "▓", "█")
_LOGO_GLYPHS: dict[str, tuple[str, ...]] = {
    "E": (
        "11111",
        "10000",
        "10000",
        "11110",
        "10000",
        "10000",
        "11111",
    ),
    "V": (
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "01010",
        "00100",
    ),
    "A": (
        "01110",
        "10001",
        "10001",
        "11111",
        "10001",
        "10001",
        "10001",
    ),
    "L": (
        "10000",
        "10000",
        "10000",
        "10000",
        "10000",
        "10000",
        "11111",
    ),
    "O": (
        "01110",
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "01110",
    ),
    "N": (
        "10001",
        "11001",
        "11001",
        "10101",
        "10011",
        "10011",
        "10001",
    ),
}


def _mix(
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, amount))
    return tuple(round(a + (b - a) * t) for a, b in zip(start, end, strict=True))


def _hex(color: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02x}" for channel in color)


def gradient_text(
    value: str,
    *,
    start: tuple[int, int, int] = ORANGE_HOT,
    end: tuple[int, int, int] = ORANGE,
    bold: bool = False,
) -> Text:
    """Color text one terminal cell at a time."""
    output = Text()
    denominator = max(1, len(value) - 1)
    for index, character in enumerate(value):
        output.append(
            character,
            Style(color=_hex(_mix(start, end, index / denominator)), bold=bold),
        )
    return output


def dither_line(width: int, density: float, row: int) -> Text:
    """Paint one ordered-dither row using the kit's Bayer thresholds."""
    output = Text()
    density = max(0.0, min(1.0, density))
    for column in range(max(0, width)):
        threshold = BAYER4[row & 3][column & 3]
        lit = density > threshold
        tier = min(4, max(0, round(density * 4))) if lit else max(0, round(density * 2))
        color = _mix(BLACK, ORANGE, 0.22 + density * (0.78 if lit else 0.34))
        output.append(_CELLS[tier], style=Style(color=_hex(color)))
    return output


class DitherLogo(Static):
    """Large Bayer-dithered Evalon wordmark for the project index."""

    DEFAULT_CSS = """
    DitherLogo {
        height: 10;
        margin: 1 1 0 1;
        background: #050301;
        color: #ff7a18;
    }
    """

    def render(self) -> Text:
        word = "EVALON"
        scale = 2
        gap = "  "
        glyph_width = len(_LOGO_GLYPHS["E"][0]) * scale
        logo_width = len(word) * glyph_width + (len(word) - 1) * len(gap)
        left = max(0, (self.size.width - logo_width) // 2)
        lines: list[Text] = []

        for row in range(7):
            line = Text(" " * left)
            logical_column = 0
            for letter_index, letter in enumerate(word):
                glyph = _LOGO_GLYPHS[letter][row]
                for pixel in glyph:
                    for _ in range(scale):
                        position = logical_column / max(1, logo_width - 1)
                        if pixel == "1":
                            density = 0.96 - position * 0.34
                            threshold = BAYER4[row & 3][logical_column & 3]
                            if density > threshold + 0.24:
                                cell = "█"
                            elif density > threshold - 0.10:
                                cell = "▓"
                            else:
                                cell = "▒"
                            color = _mix(
                                ORANGE_HOT,
                                (196, 65, 4),
                                position,
                            )
                            line.append(
                                cell,
                                style=Style(color=_hex(color), bold=True),
                            )
                        else:
                            line.append(" ")
                        logical_column += 1
                if letter_index < len(word) - 1:
                    line.append(gap)
                    logical_column += len(gap)
            lines.append(line)

        subtitle = "Evalon  //  local agent observability"
        subtitle_left = max(0, (self.size.width - len(subtitle)) // 2)
        lines.append(Text())
        label = Text(" " * subtitle_left)
        label.append_text(gradient_text(subtitle))
        lines.append(label)
        return Text("\n").join(lines)


class DitherArea(Static):
    """Small orange area chart with a dithered fill and terminal bloom."""

    DEFAULT_CSS = """
    DitherArea {
        height: 7;
        background: #050301;
        color: #ff7a18;
        border: tall #2a1609;
        padding: 0 1;
    }
    """

    def __init__(self, values: Iterable[float] = (), **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._values = list(values)

    def set_values(self, values: Iterable[float]) -> None:
        self._values = list(values)
        self.refresh()

    def render(self) -> Text:
        width = max(8, self.size.width - 4)
        height = max(2, self.size.height - 2)
        values = self._resample(self._values, width)
        ceiling = max(values, default=0.0)
        if ceiling <= 0:
            return Text("no latency samples", style="#69401f")

        output = Text()
        for row in range(height):
            y = 1.0 - row / max(1, height - 1)
            for column, value in enumerate(values):
                level = value / ceiling
                if abs(level - y) < 0.12:
                    output.append("▀", style=Style(color="#ffb04a", bold=True))
                    continue
                density = max(0.0, min(1.0, (level - y) * 1.8))
                threshold = BAYER4[row & 3][column & 3]
                if density > threshold:
                    color = _mix((65, 24, 4), ORANGE, 0.35 + density * 0.65)
                    output.append(_CELLS[min(4, 1 + round(density * 3))], style=_hex(color))
                elif level > y:
                    output.append("·", style="#5a2708")
                else:
                    output.append(" ")
            if row < height - 1:
                output.append("\n")
        return output

    @staticmethod
    def _resample(values: list[float], width: int) -> list[float]:
        if not values:
            return [0.0] * width
        if len(values) == 1:
            return values * width
        result: list[float] = []
        last = len(values) - 1
        for column in range(width):
            position = (column / max(1, width - 1)) * last
            left = int(position)
            fraction = position - left
            a = values[left]
            b = values[min(left + 1, last)]
            result.append(a + (b - a) * fraction)
        return result


class DitherButton(Button):
    """A terminal button that keeps Dither Kit's color and bloom vocabulary.

    All normal Textual ``Button`` keyword arguments pass through.
    """

    DEFAULT_CSS = """
    DitherButton {
        min-width: 16;
        width: auto;
        height: 3;
        margin-right: 1;
        color: #ffbd7a;
        background: #130904;
        border: tall #9a430d;
    }
    DitherButton:hover, DitherButton:focus {
        color: #fff2df;
        background: #3d1704;
        border: tall #ff7a18;
        text-style: bold;
    }
    DitherButton.-aura {
        border: tall #ff6b0b;
    }
    """

    def __init__(
        self,
        label: str,
        *,
        color: str = "orange",
        bloom: str = "off",
        **kwargs: object,
    ) -> None:
        del color
        super().__init__(f"░▒▓ {label}", **kwargs)
        self.add_class(f"-{bloom}")
