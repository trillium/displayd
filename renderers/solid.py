"""Fill the whole screen with one colour. Handy for testing and for blackout."""

NAME = "solid"
DESCRIPTION = "Fill the screen with a single colour"
STATIC = True
PARAMS = {
    "color": {"type": "string", "help": "fill colour, default black"},
}


def run(screen, params, stop):
    screen.clear(screen.color(params.get("color"), (0, 0, 0)))
