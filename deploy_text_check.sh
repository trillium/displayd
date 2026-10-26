# Sourced by deploy.sh (step 8): require GET /text/check to find every
# view's text inside its declared box. Hard-fails the deploy, naming each
# escaping text and its box -- the panel is already re-showed, so a failing
# gate never leaves it black. Needs $PANEL; PANEL_TEXT_CHECK overrides the URL.
TEXT_URL="${PANEL_TEXT_CHECK:-http://$PANEL/text/check}"
TEXT_CODE=$(curl -s -m 120 -o /tmp/displayd-text-check.$$ -w '%{http_code}' "$TEXT_URL" || true)
TEXT_REPORT=$(python3 - /tmp/displayd-text-check.$$ "$TEXT_CODE" <<'PY'
import json, sys
code = sys.argv[2]
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    print("BAD unreadable /text/check reply (HTTP %s)" % code); raise SystemExit
if r.get("ok") and code == "200":
    views = r.get("views", {})
    skipped = sorted(n for n, v in views.items() if not v.get("checkable", True))
    print("OK %d views clean%s" % (len(views) - len(skipped),
          "; unchecked here: " + ", ".join(skipped) if skipped else ""))
    raise SystemExit
print("BAD /text/check HTTP %s status=%s" % (code, r.get("status")))
for name, v in sorted(r.get("views", {}).items()):
    for o in v.get("offences", []):
        print("  %s [%s] %s %r ink=%s box=%s" % (
            name, o.get("case"), o.get("rule"), o.get("text"),
            o.get("ink"), o.get("box") or o.get("frame")))
    for c in v.get("cases", []):
        if c.get("error"):
            print("  %s [%s] could not draw: %s" % (name, c.get("case"), c["error"]))
PY
)
rm -f /tmp/displayd-text-check.$$
case "$TEXT_REPORT" in
    OK*) echo "text fits: ${TEXT_REPORT#OK }" ;;
    *) printf '%s\n' "$TEXT_REPORT" >&2
       echo "deploy.sh: text escapes its declared box (GET /text/check); shorten/fit the text or fix the box declaration -- the check is never weakened to pass" >&2
       exit 1 ;;
esac
