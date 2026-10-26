"""The text-fit gate: does every view's text stay inside its intended box?

Single concept: ``GET /text/check`` -- the sibling of ``/touch/check``. Each
loaded view is drawn offscreen against stress content (text_stress.py) with
the text ledger recording (text_audit.py), and every line whose ink escaped
the box its draw declared is reported by view, naming the text and the box.
It never touches the live screen, the idle clock, or the policy layer.
"""

import text_audit
import text_stress

try:
    import _html_native
except Exception:  # pragma: no cover - the html renderer stack is optional
    _html_native = None


class TextAuditMixin:
    """Text-fit report for every loaded view."""

    def _engine_built(self):
        try:
            return bool(_html_native and _html_native.lib_path())
        except Exception:
            return False

    def text_check(self):
        """GET /text/check body. ``ok`` False means a line escaped its box
        (status ``overflow``) or a view could not be drawn (``error``);
        engine-less litehtml views are ``checkable`` False with the reason,
        never a silent pass."""
        w, h = self.screen.W, self.screen.H
        built = self._engine_built()
        results, ok = {}, True
        for name, entry in sorted(self.renderers.items()):
            module = entry.get("module") if isinstance(entry, dict) else None
            if module is None:
                continue
            if name in text_audit.NEEDS_ENGINE and not built:
                results[name] = {"ok": True, "checkable": False,
                                 "reason": "litehtml engine not built here"}
                continue
            cases = []
            for case in text_stress.cases_for(name):
                if name in ("picker", "unified"):
                    views = self._expected_picker_views({}, name)
                    case = dict(case, params=dict(case.get("params") or {},
                                                  **({"views": views}
                                                     if views else {})))
                cases.append(text_audit.check_case(module, case, w, h))
            good = all(c["ok"] for c in cases)
            ok = ok and good
            results[name] = {"ok": good, "checkable": True, "cases": cases,
                             "offences": [dict(o, case=c["case"])
                                          for c in cases
                                          for o in c["offences"]]}
        return {"ok": ok, "status": "ok" if ok else "overflow",
                "display": [w, h], "views": results,
                "engine": built, "coverage": text_audit.COVERAGE}
