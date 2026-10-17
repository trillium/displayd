#!/usr/bin/env python3
"""Gate: templates stay trusted local chrome, and the raw slot count is frozen.

Two rules, both reported by name, both non-zero exits.

RULE 1 -- a template carries no active or remote content
--------------------------------------------------------
Nothing under ``html-templates/`` may contain a ``<script`` tag, a
``javascript:`` URL, an ``@import``, or a remote ``http(s)://``
resource reference. The html renderer already refuses caller markup (see
``renderers/_html_templates.py``), so a template is the *trusted* half of
that boundary -- and a trusted file is exactly the place a script or a
remote URL would survive every other rule. Local chrome only: text,
layout, and images that resolve inside the template root.

Scope is the literal bytes for ``<script`` and the EFFECTIVE document for
the other three. Comments are stripped first (HTML ``<!-- -->`` and CSS
``/* */``), because the templates legitimately *document* the rule in
prose -- "no @import, no webfonts" is not an ``@import``. A commented-out
script tag is inert, but it is still the only thing in this tree that
would ever be one, so it is looked for in the bytes and fails.

RULE 2 -- raw slots are a ratchet
---------------------------------
``{{name|raw}}`` is the ONE place a document receives unescaped markup,
and it is filled only from a renderer's private mapping -- never from a
caller, never from a feed. That makes the number of raw slots a template
may declare a number that must never go up quietly: a new raw slot is a
new hole in the escaping rule, so it has to be argued for. The count is
frozen at ``RAW_SLOT_COUNT`` and the gate fails in BOTH directions:

- more raw slots than frozen -- fail: new unescaped markup surface;
- fewer than frozen -- fail: the frozen number is a fact about the tree,
  so retiring a raw slot is a deliberate edit to this constant, not a
  side effect of some other change.

``--list`` prints the findings (one per line) and exits 0; ``--root``
points the gate at another template directory so a synthetic tree can
prove the rules fail.

Deterministic: discovery is sorted, the report is sorted, and line
numbers come from the file itself. Exits non-zero naming the file, the
line, the matched text, and the rule.
"""

import argparse
import pathlib
import re
import sys

TEMPLATE_DIR = "html-templates"

# Every raw slot the templates may declare, counted over the effective
# (comment-stripped) document of every template in the root, partials
# included. Today, exactly three: chat.html `panes`, options.html `names`
# and picker.html `tiles` -- the three surfaces that compose their own
# markup over values they escaped themselves. Changing this number is an
# argument for the change, not an edit to make the gate quiet.
RAW_SLOT_COUNT = 3

COMMENT_RES = (re.compile(r"<!--.*?-->", re.DOTALL),
               re.compile(r"/\*.*?\*/", re.DOTALL))
RAW_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\|\s*raw\s*\}\}")

RULE_ACTIVE = "no-script-in-a-template"
RULE_JS_URL = "no-javascript-url-in-a-template"
RULE_IMPORT = "no-at-import-in-a-template"
RULE_REMOTE = "no-remote-resource-in-a-template"
RULE_RAW_COUNT = "raw-slot-count-is-frozen"

# (rule, compiled, in_comments_too)
RULE1 = (
    (RULE_ACTIVE, re.compile(r"<\s*/?\s*script", re.I), True),
    (RULE_JS_URL, re.compile(r"javascript\s*:", re.I), False),
    (RULE_IMPORT, re.compile(r"@import", re.I), False),
    (RULE_REMOTE, re.compile(r"https?://", re.I), False),
)

RULE_NAMES = {
    RULE_ACTIVE: "RULE 1 no active content: a template may not contain a "
                 "<script tag",
    RULE_JS_URL: "RULE 1 no active content: a template may not contain a "
                 "javascript: URL",
    RULE_IMPORT: "RULE 1 no active content: a template may not @import",
    RULE_REMOTE: "RULE 1 no local chrome only: a template may not reference "
                 "a remote http(s) resource",
    RULE_RAW_COUNT: "RULE 2 raw slots are frozen at %d: {{name|raw}} is the "
                    "one unescaped slot and only a renderer may fill it",
}


def rule_name(rule):
    """The rule's sentence, with the live frozen count filled in."""
    text = RULE_NAMES[rule]
    return text % RAW_SLOT_COUNT if "%d" in text else text


def repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def strip_comments(text):
    for regex in COMMENT_RES:
        text = regex.sub("", text)
    return text


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _context(line):
    return line.strip()[:60]


def template_files(root):
    """Every template file under root, sorted, as relative POSIX paths."""
    base = pathlib.Path(root)
    if not base.is_dir():
        return []
    return sorted(p.relative_to(base).as_posix()
                  for p in base.rglob("*.html"))


def scan_text(name, text):
    """Rule 1 findings for one file: (rule, line, snippet) triples."""
    found = []
    effective = strip_comments(text)
    for rule, regex, in_comments in RULE1:
        subject = text if in_comments else effective
        for match in regex.finditer(subject):
            line_no = _line_of(subject, match.start())
            snippet = _context(subject.split("\n")[line_no - 1])
            found.append((rule, name, line_no, match.group(0), snippet))
    return found


def raw_slots(text):
    """Raw slots this file really declares, comments excluded."""
    return RAW_RE.findall(strip_comments(text))


def scan_tree(root):
    """(rule1 findings, raw slot count) for a whole template root."""
    findings = []
    slots = 0
    for rel in template_files(root):
        path = pathlib.Path(root) / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            findings.append((RULE_ACTIVE, rel, 0, "unreadable",
                             str(err)[:60]))
            continue
        findings.extend(scan_text(rel, text))
        slots += len(raw_slots(text))
    return findings, slots


def report(findings, out=None):
    out = out or sys.stdout
    for rule, name, line_no, match, snippet in findings:
        print("%s: %s:%d: %r %s" % (rule, name, line_no, match, snippet),
              file=out)
        print("  %s" % rule_name(rule), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fail while a template carries active or remote content, "
                    "or while the raw slot count moves off its frozen value.")
    ap.add_argument("--root", default=None,
                    help="template root to inspect (default: %s in this repo)"
                         % TEMPLATE_DIR)
    ap.add_argument("--list", action="store_true",
                    help="print the findings and exit 0")
    args = ap.parse_args(argv)
    root = (pathlib.Path(args.root).resolve() if args.root
            else repo_root() / TEMPLATE_DIR)
    findings, slots = scan_tree(root)
    if slots != RAW_SLOT_COUNT:
        findings.append((RULE_RAW_COUNT, ".", 0, "%d raw slot(s)" % slots,
                         "frozen at %d" % RAW_SLOT_COUNT))
    findings.sort()
    if args.list:
        for rule, name, line_no, match, snippet in findings:
            print("%s: %s:%d: %r %s" % (rule, name, line_no, match, snippet))
        return 0
    if findings:
        report(findings)
        print("template trust: %d finding(s), %d raw slot(s), frozen at %d"
              % (len(findings), slots, RAW_SLOT_COUNT))
        return 1
    print("template trust ok: %d template file(s) carry no active or remote "
          "content; %d raw slot(s), frozen at %d"
          % (len(template_files(root)), slots, RAW_SLOT_COUNT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
