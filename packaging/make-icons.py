"""Иконки приложения для планшета: тёмный квадрат, кнопка-треугольник и две строки.

Рисуются кодом, а не лежат готовыми, чтобы их можно было перерисовать одной
командой и не хранить в репозитории ничего, что нельзя объяснить. Требования
Android к иконкам — 192x192 и 512x512 в PNG; в манифесте объявлены обе.

Запуск: .venv/bin/python packaging/make-icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "page"

# Цвета страницы: см. --bg и --accent в page/style.css.
BACK = (23, 23, 27)
INK = (242, 242, 242)
ACCENT = (76, 141, 255)

SUPERSAMPLE = 4


def rounded(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    """Точка внутри прямоугольника со скруглёнными углами, координаты 0..1."""
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False

    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)

    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def triangle(x: float, y: float, ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
    def side(px, py, qx, qy):
        return (qx - px) * (y - py) - (qy - py) * (x - px)

    d1 = side(ax, ay, bx, by)
    d2 = side(bx, by, cx, cy)
    d3 = side(cx, cy, ax, ay)

    return (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0)


def pixel(x: float, y: float) -> tuple[int, int, int, int]:
    if not rounded(x, y, 0.02, 0.02, 0.98, 0.98, 0.19):
        return (0, 0, 0, 0)

    if triangle(x, y, 0.40, 0.27, 0.40, 0.61, 0.66, 0.44):
        return (*ACCENT, 255)

    if rounded(x, y, 0.27, 0.68, 0.73, 0.745, 0.033):
        return (*INK, 255)

    if rounded(x, y, 0.27, 0.785, 0.60, 0.85, 0.033):
        return (*INK, 255)

    return (*BACK, 255)


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    rows = []
    step = 1 / (size * SUPERSAMPLE)

    for py in range(size):
        row = []

        for px in range(size):
            red = green = blue = alpha = 0
            samples = 0

            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    y = (py * SUPERSAMPLE + sy + 0.5) * step
                    r, g, b, a = pixel(x, y)
                    red += r * a
                    green += g * a
                    blue += b * a
                    alpha += a
                    samples += 1

            if alpha:
                row.append((
                    round(red / alpha),
                    round(green / alpha),
                    round(blue / alpha),
                    round(alpha / samples),
                ))
            else:
                row.append((0, 0, 0, 0))

        rows.append(row)

    return rows


def write_png(path: Path, rows: list[list[tuple[int, int, int, int]]]) -> None:
    height = len(rows)
    width = len(rows[0])
    raw = b"".join(
        b"\x00" + b"".join(bytes(pixel) for pixel in row) for row in rows
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def main() -> None:
    for size in (192, 512):
        write_png(PAGE / f"icon-{size}.png", render(size))
        print(f"page/icon-{size}.png")


if __name__ == "__main__":
    main()
