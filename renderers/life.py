"""Conway's Game of Life, drawn as a moving pattern. Purely procedural."""

import random
import time

from PIL import ImageDraw

NAME = "life"
DESCRIPTION = "Conway's Game of Life, drawn as coloured cells that evolve"
STATIC = False
PARAMS = {
    "cell": {"type": "integer", "help": "cell size in pixels, default 12"},
    "density": {"type": "number", "help": "starting fill 0-1, default 0.14"},
    "speed": {"type": "number", "help": "seconds between generations, default 0.09"},
}


def _seed(cols, rows, density):
    count = int(cols * rows * density)
    return {(random.randrange(cols), random.randrange(rows)) for _ in range(count)}


def _step(live, cols, rows):
    counts = {}
    for x, y in live:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    key = ((x + dx) % cols, (y + dy) % rows)
                    counts[key] = counts.get(key, 0) + 1
    return {cell for cell, n in counts.items() if n == 3 or (n == 2 and cell in live)}


def run(screen, params, stop):
    cell = max(2, int(params.get("cell") or 12))
    density = float(params.get("density") or 0.14)
    speed = float(params.get("speed") or 0.09)
    cols, rows = screen.W // cell, screen.H // cell

    live = _seed(cols, rows, density)
    frame = 0
    while not stop.is_set():
        frame += 1
        img = screen.new_image((0, 0, 0))
        draw = ImageDraw.Draw(img)
        for x, y in live:
            colour = (
                (x * 9 + frame * 5) & 255,
                (y * 11 + frame * 3) & 255,
                (x * 3 + y * 5 + frame * 7) & 255,
            )
            px, py = x * cell, y * cell
            draw.rectangle((px, py, px + cell - 2, py + cell - 2), fill=colour)
        screen.present(img)

        nxt = _step(live, cols, rows)
        live = nxt if nxt else _seed(cols, rows, density)
        time.sleep(speed)
