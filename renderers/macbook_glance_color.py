"""The GLANCE look: type sizes, mode-chip and status colours.

Single concept: the colour and type vocabulary every GLANCE painter draws
with -- the four font sizes the macbook feature loads, the Talon
mode-chip colours, and the status palette (stale/dim/rule/row/focus).
Values only: no drawing, no imports.

The view's accent is not here: it is the palette's identity slot,
``theme.ACCENT_SLOTS["macbook"]``.
"""

TITLE_SIZE = 30
APP_SIZE = 44
ROW_SIZE = 32
META_SIZE = 30

MODE_COLORS = {
    "command": (110, 220, 130),
    "dictation": (255, 200, 90),
    "mixed": (120, 200, 255),
    "sleep": (120, 120, 140),
    "other": (150, 150, 150),
}
C_STALE = (255, 180, 80)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_ROW = (255, 255, 255)
C_FOCUS_BG = (38, 66, 44)
C_FOCUS = (110, 220, 130)
