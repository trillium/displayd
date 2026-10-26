"""The inputs the text check feeds each view: real-feed shapes at their worst.

Single concept: stress content. It says nothing about layout (a view's own
draw declares its boxes); it only supplies the strings the panel meets from
real feeds -- long window titles, long app names, long bead titles, an
unbroken token, an empty string, and non-ASCII text -- so every view is
drawn at its worst, not at its demo. Views not listed get one default case.
"""

LONG_TITLE = ("Quarterly planning review: a deliberately very long window "
              "title that keeps going well past any sensible column width "
              "and then some more, for good measure")
LONG_WORD = "SupercalifragilisticexpialidociousTokenWithoutAnySpaces" * 3
LONG_APP = "Visual Studio Code - Insiders Extra Long Edition Beta Preview"
NON_ASCII = "日本語のとても長いウィンドウタイトル — Ünïcödé ✓ ☃ résumé naïve façade"
SHORT = "ok"


def _cases(label, **kw):
    return dict(label=label, **kw)


def _text_cases():
    return [("long title", LONG_TITLE), ("unbroken", LONG_WORD),
            ("non-ascii", NON_ASCII), ("short", SHORT)]


def _mac_state(app, title):
    return {"ts": 1.0, "accessibility_trusted": True,
            "focus": {"app_name": app, "window_title": title, "pid": 1,
                      "display_index": 0,
                      "window_bounds": {"x": 10, "y": 10, "w": 800, "h": 600}},
            "mouse": {"x": 10, "y": 10, "display_index": 0},
            "displays": [{"index": 0, "main": True,
                          "bounds": {"x": 0, "y": 0, "w": 1440, "h": 900}},
                         {"index": 1, "bounds": {"x": 1440, "y": 0,
                                                 "w": 400, "h": 300}}],
            "talon": {"mode": "command", "microphone": "Built-in", "muted": False}}


def cases_for(name):
    """The probe cases for one view (always at least one)."""
    out = []
    if name == "notice":
        out = [_cases(l, params={"title": t, "body": t + "\n" + t,
                                 "severity": "warn"})
               for l, t in _text_cases()]
        out.append(_cases("empty", params={"title": "", "body": ""}))
    elif name == "text":
        out = [_cases(l, params={"text": t}) for l, t in _text_cases()]
        out.append(_cases("empty", params={"text": ""}))
    elif name == "clock":
        out = [_cases("default"), _cases("long format",
                      params={"format": "%A %d %B %Y " * 4})]
    elif name == "activity":
        out = [_cases(l, params={"title": t}, feeds={"event": [
            {"tool": t, "outcome": "ok", "caller": t, "summary": t * 2,
             "client": t},
            {"tool": "", "outcome": "", "summary": ""}]})
               for l, t in _text_cases()]
    elif name == "chat":
        out = [_cases(l, params={"title": t}, feeds={"message": [
            {"id": "1", "author": t, "text": t * 3},
            {"id": "2", "author": "", "text": ""},
            {"id": "3", "author": "a", "text": "join", "join": True}]})
               for l, t in _text_cases()]
    elif name == "macbook":
        out = [_cases(l, params={"title": t}, feeds={
            "state": [_mac_state(t, t)],
            "talon_apps/state": [{"ts": 1.0, "apps": [t] * 9 + ["", "x"],
                                  "focused": t}]})
               for l, t in [("long app+title", LONG_APP),
                            ("non-ascii", NON_ASCII), ("unbroken", LONG_WORD)]]
        out.append(_cases("empty", feeds={"state": [_mac_state("", "")]}))
    elif name == "qr":
        out = [_cases(l, params={"data": "https://example.com/x",
                                 "caption": t}) for l, t in _text_cases()]
    elif name == "sleep":
        out = [_cases(l, params={"hint": t}) for l, t in _text_cases()]
    elif name == "reload":
        out = [_cases("default", params={"sha": "a" * 40,
                                         "relay_url": "https://example.com/r/x"})]
    elif name == "retro_grid":
        out = [_cases(l, params={"boxes": [{"label": t}] * 12})
               for l, t in _text_cases()]
    elif name == "touch_confidence":
        out = [_cases(l, params={"title": t, "instructions": t,
                                 "regions": [{"id": t, "rect": [10, 10, 80, 40],
                                              "action": t}]})
               for l, t in _text_cases()]
    elif name == "services":
        out = [_cases(l, params={"title": t}) for l, t in _text_cases()]
    elif name == "feed_health":
        out = [_cases(l, params={"title": t}) for l, t in _text_cases()]
    elif name == "row":
        out = [_cases(l, params={"title": t}) for l, t in _text_cases()]
    elif name == "resources":
        out = [_cases(l, params={"title": t}) for l, t in _text_cases()]
    elif name == "beads":
        out = [_cases(l, params={"title": t}) for l, t in _text_cases()]
    return out or [_cases("default")]
