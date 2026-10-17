#!/usr/bin/env python3
"""Printing and the command line for the switch-latency gate.

Single concept: how a measurement report is read. One place decides what a
reader sees, so the numbers a human pastes into a PR and the numbers a test
asserts on are the same report: the per-view latency table, the views that
could NOT be measured here (named, with the reason), and the failures.

The rules this module renders are in ``switch_latency.py``; nothing here
measures anything. It is split out only so the measurement core stays inside
the line budget next to the other tools.

  python3 tools/switch_latency_report.py [--budget MS] [--timeout S]
                                         [--only NAME[,NAME...]]
                                         [--require-all] [--json-out PATH]

Exits non-zero when the report carries a problem, so this is also the
command a health gate or a pre-merge hook can run.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import switch_latency  # noqa: E402


def render(report, out=print):
    """Print the whole report: table, gaps, failures, verdict."""
    budget = report.get("budget_ms", switch_latency.BUDGET_MS)
    measured = report.get("measured") or []
    deferred = report.get("not_measured") or []
    problems = report.get("problems") or []
    out("switch latency: request -> first changed pixel, budget %.0f ms"
        % budget)
    for row in measured:
        out("  %7.1f ms  %s" % (row["ms"], row["view"]))
    if not measured:
        out("  (nothing measured)")
    slow = report.get("known_slow") or []
    if slow:
        out("KNOWN SLOW (over budget, recorded in KNOWN_SLOW, not a pass):")
        for row in slow:
            out("  %7.1f ms  %-14s %s" % (row["ms"], row["view"], row["why"]))
    if deferred:
        out("NOT MEASURED HERE -- named, never silently skipped:")
        for row in deferred:
            out("  %-16s %s" % (row["view"], row["reason"]))
    for problem in problems:
        out("FAIL: %s" % problem)
    if problems:
        out("switch latency: FAILED (%d problem(s) over %d measured view(s))"
            % (len(problems), len(measured)))
    elif measured:
        slowest = max(measured, key=lambda row: row["ms"])
        out("switch latency ok: %d view(s) measured, slowest %s at %.1f ms"
            % (len(measured), slowest["view"], slowest["ms"]))
    out("engine: %s" % report.get("engine", "unknown"))
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--budget", type=float, default=switch_latency.BUDGET_MS,
                    help="milliseconds a cold switch may take (default %.0f)"
                         % switch_latency.BUDGET_MS)
    ap.add_argument("--timeout", type=float, default=switch_latency.TIMEOUT_S,
                    help="seconds to wait for a view's first frame")
    ap.add_argument("--only", default=None,
                    help="comma-separated view names (debugging: this makes "
                         "the run a partial sweep, not the gate)")
    ap.add_argument("--require-all", action="store_true",
                    help="fail when a view needs the litehtml engine and the "
                         "engine is not built here")
    ap.add_argument("--json-out", default=None, help="write the report here")
    args = ap.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None
    report = switch_latency.run(budget=args.budget, timeout=args.timeout,
                                only=only, require_all=args.require_all,
                                out=None)
    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(report, handle, indent=2)
    return render(report)


if __name__ == "__main__":
    sys.exit(main())
