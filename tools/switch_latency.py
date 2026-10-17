#!/usr/bin/env python3
"""Switch-latency gate: switch request -> first changed pixel, per view.

What it measures
================
The panel's feel is the gap between deciding to show a view and seeing
something different. This harness measures exactly that, end to end,
against a headless daemon over loopback HTTP:

    t0 = the instant POST /show is sent
    t1 = the instant the framebuffer is written with bytes that differ
         from what was on screen

so the number includes the HTTP round trip, param validation, the view
thread's first draw, and the single compose-then-swap write. It is the
same instrument as ``tools/tap_latency.py`` (which breaks a tap's latency
into stages) pointed at a different subject: one cold switch per view.

Cold means cold: the frame cache entry for the view is dropped before the
switch, so every measurement is a real first paint and never a memcpy of
a frame this run already drew. The baseline is a real different view, so
"changed pixel" means changed, not merely written.

No view is skipped quietly
==========================
The view list is the renderer registry itself -- whatever ``GET
/renderers`` would offer -- so a new view cannot miss the gate. For each
one:

- no fixture in ``switch_latency_fixtures.VIEWS`` -- FAIL, named;
- needs the litehtml engine and the engine is not built here -- REPORTED
  as not measured, with the path the loader looked at, never counted as
  fast (a red build-hint card is not the view);
- it presented no frame inside the timeout -- FAIL, named;
- it raised -- FAIL, named;
- its first paint was over the budget -- FAIL, named, with the number.

The report is printed either way, so the names are in the test output and
not only in a failure. An empty measured set is a FAILURE, never a pass.
``--require-all`` additionally turns "engine missing" into a failure
(a target that has the runtime installed should say so).

The report is rendered by ``switch_latency_report`` (printing and the CLI
live there; this module is the measurement), and the per-view inputs live
in ``switch_latency_fixtures``.

Usage:
  python3 tools/switch_latency.py [--budget MS] [--timeout S]
                                  [--only NAME[,NAME...]]
                                  [--require-all] [--json-out PATH]
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (HERE, ROOT, os.path.join(ROOT, "bridges")):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("DISPLAYD_FAKE_FB", "1")

import switch_latency_fixtures as fixtures  # noqa: E402
import switch_latency_report  # noqa: E402
from switch_latency_panel import Panel, engine_status  # noqa: E402

BUDGET_MS = 100.0
TIMEOUT_S = 5.0
TRIES = 3
# Views that hover at or over BUDGET_MS today, by name. Each is reported under
# "KNOWN SLOW" with its number, never counted as passing, and still fails past
# its own ceiling. Delete the entry once the view is reliably fast. Adding one is an
# argument for the change -- Slice 0 does not touch rendering, so it records
# the debt instead of hiding it. view -> (ceiling_ms, why).
KNOWN_SLOW = {
    "retro_grid": (500.0, "measured 105-400 ms cold on the Pillow path "
                          "(dither + bevel per cell); migration slice owns it"),
}
# The alert rule an error card draws across its top (theme "alert"). A
# failure card is loud and fast, so it is the one frame that would let a
# broken view look like a quick one -- see measure().
ERROR_RULE = (214, 74, 74)


def feed_pushes(name, spec, scratch):
    """(renderer, input, payload) triples to buffer before the switch."""
    feeds = spec.get("feeds") or {}
    if callable(feeds):
        feeds = feeds(scratch)
    triples = [(name, key, payload) for key, payload in feeds.items()]
    for renderer, inputs in (spec.get("extra_feeds") or {}).items():
        triples += [(renderer, key, payload) for key, payload in inputs.items()]
    return triples


def measure(panel, name, spec, scratch, timeout):
    """One cold switch -> first changed pixel.

    Returns (latency_ms, reason). A reason string means the number is not
    trustworthy and the view is a FAILURE, not a fast view.
    """
    params = spec.get("params") or {}
    if callable(params):
        params = params(scratch)
    for renderer, input_name, payload in feed_pushes(name, spec, scratch):
        result = panel.post("/feed/%s/%s" % (renderer, input_name), payload)
        if not result.get("ok"):
            return None, "feed %s/%s refused: %s" % (
                renderer, input_name, result.get("error"))
    with panel.daemon.cache_lock:      # cold: no cached frame may answer
        panel.daemon.frame_cache.pop(name, None)
    if panel.settle_baseline(timeout) is None:
        return None, "the baseline view never painted"
    panel.daemon.last_error = None
    panel.arm()
    t0 = time.perf_counter()
    result = panel.post("/show", {"renderer": name, "params": params})
    if result.get("error"):
        return None, "show refused: %s" % result.get("error")
    deadline = t0 + timeout
    while panel.painted_at is None and time.perf_counter() < deadline:
        time.sleep(0.0005)
    if panel.painted_at is None:
        return None, "no frame within %.1fs" % timeout
    latency = (panel.painted_at - t0) * 1000.0
    frame = panel.last_image
    if frame is not None and frame.getpixel((frame.size[0] // 2, 1)) == \
            ERROR_RULE:
        # Loud and fast: a failure card is not the view, so its latency
        # says nothing about the view being switched to.
        return latency, "drew a failure card, not the view"
    if panel.daemon.current != name:
        return latency, "the panel moved on to %r before the paint" % (
            panel.daemon.current,)
    if panel.daemon.last_error:
        return latency, "the view raised: %s" % panel.daemon.last_error
    return latency, None


def best_of(panel, name, spec, scratch, timeout, tries=TRIES):
    """The fastest of `tries` cold switches; any failed try is the answer.

    Load on the box adds latency and never removes it, so the minimum is the
    view's own cost. A view that is really slow is slow on every try.
    """
    best = None
    for _ in range(tries):
        ms, reason = measure(panel, name, spec, scratch, timeout)
        if reason:
            return ms, reason
        best = ms if best is None else min(best, ms)
    return best, None


def sweep(panel, scratch, budget, timeout, only, require_all):
    """Measure every registry view once. Returns the report dict."""
    registry = panel.daemon.renderers
    names = sorted(n for n, e in registry.items() if "module" in e)
    broken = sorted(n for n, e in registry.items() if "broken" in e)
    engine = engine_status(panel.daemon)
    engine_why = "litehtml engine not usable here (%s)" % (
        engine.get("error") or ("looked for %s" % engine.get("library")))
    measured, deferred, problems, known_slow = [], [], [], []
    for name in broken:
        problems.append("%s: broken renderer: %s" % (name, registry[name]))
    for name in names:
        if only and name not in only:
            continue
        spec = fixtures.VIEWS.get(name)
        if spec is None:
            problems.append("%s: no fixture in switch_latency_fixtures.VIEWS "
                            "-- a view cannot enter the gate unmeasured" % name)
            continue
        if spec.get("needs_engine") and not engine.get("ok"):
            deferred.append((name, engine_why))
            continue
        ms, reason = best_of(panel, name, spec, scratch, timeout)
        if reason:
            problems.append("%s: %s" % (name, reason))
            continue
        slow = KNOWN_SLOW.get(name)
        if slow and not only:
            known_slow.append((name, ms, slow[1]))
            if ms > slow[0]:
                problems.append("%s: %.1f ms is over its %.0f ms known-slow "
                                "ceiling" % (name, ms, slow[0]))
            continue
        measured.append((name, ms))
        if ms > budget:
            problems.append("%s: %.1f ms is over the %.0f ms budget"
                            % (name, ms, budget))
    if not measured:
        problems.append("no view was measured: an empty gate is a failure, "
                        "not a pass")
    if deferred and require_all:
        problems.append("%d view(s) need the litehtml engine and --require-all "
                        "was given" % len(deferred))
    report = {"budget_ms": budget,
              "measured": [{"view": n, "ms": round(ms, 1)}
                           for n, ms in measured],
              "not_measured": [{"view": n, "reason": why} for n, why in deferred],
              "known_slow": [{"view": n, "ms": round(ms, 1), "why": why}
                             for n, ms, why in known_slow],
              "problems": problems,
              "engine": "usable" if engine.get("ok") else engine_why}
    return report


def run(budget=BUDGET_MS, timeout=TIMEOUT_S, only=None, require_all=False,
        out=print):
    """Measure every view once, render the report to `out`, and return it.
    Never raises: a harness that dies on a broken view is a harness that
    skips it. Pass ``out=None`` to measure without printing."""
    with tempfile.TemporaryDirectory() as scratch:
        panel = Panel(scratch)
        try:
            result = sweep(panel, scratch, budget, timeout, only,
                           require_all)
        except Exception as err:
            result = {"budget_ms": budget, "measured": [], "not_measured": [],
                      "problems": ["the harness raised: %s: %s"
                                   % (type(err).__name__, err)],
                      "engine": "unknown"}
        finally:
            panel.close()
    if out:
        switch_latency_report.render(result, out)
    return result


if __name__ == "__main__":
    import switch_latency_report

    sys.exit(switch_latency_report.main())
