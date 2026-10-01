"""Measure what the html renderer costs a panel: cold latency, warm, RSS.

The spike report's claim was that litehtml layout plus a PIL pass is cheap
enough to sit in the render path, with no growth in resident memory across
renders. This harness is how that claim gets checked instead of asserted,
and it is the reason the memory bound matters: a panel runs for days, so a
leak is far worse than a slow frame.

Two numbers, and they answer different questions:

  cold   first render in a fresh process -- layout, font discovery, image
         decode. This is the worst case and the one that would show up as a
         visible stall on a /show.
  warm   steady-state per render, median of N. This is what a feed push
         actually costs, ~60 times a second at the streaming caps.
  rss    resident memory before and after, so a per-render leak in the
         font/image caches or in the native document is visible as a slope
         rather than inferred.

Usage:
  python3 tools/bench_html.py [--renders N] [--width W] [--height H]
                              [--template status.html] [--json-out PATH]
                              [--sizes]

  --sizes also renders a pathological document set (tiny repeating tile over
  a full-HD box, a very tall document) because those are what the internal
  caps exist for; if any of them are slow or allocate, the caps are wrong.

Run from the repo root. Needs the native library:
  DISPLAYD_LITEHTML_BUILD=build/litehtml ./tools/build_litehtml.sh
"""

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import _html_native  # noqa: E402
import _html_templates as templates  # noqa: E402


def rss_kb():
    """Resident set size in KiB, or None where it cannot be read.

    Deliberately not psutil: this is the only place the script needs it, and
    the parser is three lines.
    """
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        import resource
        # ru_maxrss is KiB on Linux, bytes on macOS.
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform != "darwin" else peak / 1024)
    except Exception:
        return None


PANEL = (1920, 1080)
VARS = {"title": "BUILD", "sub": "12:04", "value": "142", "unit": "taps",
        "note": "AIM review - last 7 days"}

# The shapes the caps in _html_native.py and the container exist for. A
# render budget that only survives polite input is not a budget.
HOSTILE = [
    ("1px tile over full HD", "<div style='width:100%%;height:100%%;"
     "background-image:url(tile.png);background-repeat:repeat'></div>"),
    ("200 rows of 40px", "".join(
        "<div style='height:40px;background:#0a0a0a'></div>" for _ in range(200))),
    ("2000 nested divs", "".join(
        "<div style='padding:1px'>" for _ in range(2000)) + "x"
        + "".join("</div>" for _ in range(2000))),
    ("deep rgba text", "<div style='font-size:28px;"
     "color:rgba(255,255,255,0.02)'>" + "overlap " * 400 + "</div>"),
]


def timed(fn, count):
    """Median of `count` runs, plus the extremes, in milliseconds."""
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "runs": count,
    }


def make_tile(directory):
    """A 1x1 red PNG, for the repeating-background case."""
    from PIL import Image
    path = os.path.join(directory, "tile.png")
    Image.new("RGB", (1, 1), (255, 0, 0)).save(path)
    return _html_native.allow_root(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--renders", type=int, default=25,
                        help="warm renders to time (default 25)")
    parser.add_argument("--width", type=int, default=PANEL[0])
    parser.add_argument("--height", type=int, default=PANEL[1])
    parser.add_argument("--template", default="status.html")
    parser.add_argument("--sizes", action="store_true",
                        help="also time the pathological documents")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    print("html render bench -- %dx%d, %s" % (args.width, args.height,
                                              args.template))
    version = _html_native.version()
    print("engine: %s" % version)

    document, root = templates.load(args.template, VARS)
    report = {"engine": version, "width": args.width, "height": args.height,
              "template": args.template}

    # Cold: a fresh process pays library load, font discovery and first
    # decode. Measured before anything else so nothing is warm.
    start = time.perf_counter()
    _html_native.render(document, args.width, args.height, root=root)
    report["cold_ms"] = round((time.perf_counter() - start) * 1000.0, 2)
    print("cold first render : %8.2f ms" % report["cold_ms"])

    before = rss_kb()
    report["warm"] = timed(
        lambda: _html_native.render(document, args.width, args.height,
                                    root=root), args.renders)
    after = rss_kb()
    print("warm render       : %8.2f ms median (%d runs, %.2f-%.2f)"
          % (report["warm"]["median_ms"], args.renders,
             report["warm"]["min_ms"], report["warm"]["max_ms"]))

    # The claim that matters most: resident memory does not grow with the
    # render count. Sampling after 25, 100 and 400 renders shows the caches
    # filling and then flattening; a real leak keeps a constant per-render
    # cost, and these three numbers are what tell the two apart. One
    # before/after pair cannot.
    def draw():
        _html_native.render(document, args.width, args.height, root=root)

    draw()
    print("\nmemory over renders. The per-render cost should fall to ~0 once")
    print("the font and image caches are full; a leak would hold a constant")
    print("per-render cost instead. The last stage is the one that decides it.")
    report["rss_series"] = []
    total = 0
    for stage in (25, 75, 300, 2000):
        timed(draw, stage)
        total += stage
        now = rss_kb()
        report["rss_series"].append({"renders": total, "stage": stage,
                                     "rss_kb": now})
        if now is not None and after is not None:
            print("  %5d renders: %7d KiB  (%+.2f KiB/render over the last %d)"
                  % (total, now, (now - after) / float(stage), stage))
            after = now

    if args.sizes:
        print("\npathological documents:")
        report["hostile"] = []
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tile_root = make_tile(tmp)
            for label, body in HOSTILE:
                entry = dict(timed(
                    lambda b=body: _html_native.render(
                        b, args.width, args.height, root=tile_root), 5))
                entry["label"] = label
                report["hostile"].append(entry)
                print("  %-22s %8.2f ms" % (label, entry["median_ms"]))

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print("\nwrote %s" % args.json_out)


if __name__ == "__main__":
    main()
