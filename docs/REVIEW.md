# Review decision page

A bead from the `review` store, drawn as one focused decision on the panel,
answered by touch. Everything on the page is deterministic; the only model
hook is the optional, off-by-default summary below.

## Pieces

| File | Concept |
| --- | --- |
| `review_decision.py` | pure parse of `review show <id> --json` into options |
| `review_flow.py` | state machine: idle, showing, confirming, sending, done, failed |
| `review_view.py` | template variables and the touch regions |
| `html-templates/review.html`, `renderers/review.py` | the `review` view |
| `daemon_review.py`, `daemon_http_review.py` | daemon wiring and routes |
| `review_summary.py` | optional AI summary hook (off) |
| `tools/review_bridge.py` | Mac-side executor (talks to the store) |

## Options

In order: `metadata.decision.options` (strings, or `{id, label, text?, action?,
value?}` with `action` `respond` (default) or `dismiss`), else the
description's enumeration `(1) ... (2) ...` (answer text `Option N: <text>`).
At most 5. A bead with neither is shown read-only: no invented buttons. A
bead is actionable only when it is `open`, labelled `human`, and has options.

## Flow and routes (all token-gated when a token is set)

`GET /review` state; `POST /review/load {bead}` (a `review show --json`
payload; shows the view); `POST /review/select {slot}` (1-based; asks for
confirmation, sends nothing); `POST /review/confirm` (publishes the decision
event); `POST /review/back` (one step back; from showing, leaves the view);
`POST /review/result {nonce, ok, message}` (the bridge's answer). Any call
from the wrong state is a 409 and changes nothing, so a stale or doubled tap
cannot send twice. Select/confirm/back are also gated on the view showing.

## Bridge

`python3 tools/review_bridge.py load <bead>` fetches the bead and loads it;
`serve` follows the `review-decisions` stream. The event carries only a bead
id and an option id: the bridge re-fetches the bead, re-parses it, checks it
is still actionable and the option/action still match, then runs
`review human respond <id> <text>` or `review human dismiss <id>` and reports
the outcome. On start it executes only the decision the panel is awaiting
(nonce from `GET /review`), never a backlog. Env: `DISPLAYD_URL`, `REVIEW_BIN`.
It is not in `bridges/mac-set.manifest` and has no LaunchAgent (tracked
separately).

## Touch regions

`touch-review.json.example` holds `view_regions.review` for 1920x1080 (generated
from `review_view.review_regions`, pinned by a test): `rv-opt1..5`, `rv-back`,
`rv-confirm`. Merge it into the host `touch.json`; the host file is not
edited by the repo.

## AI summary (off)

`review_summary.py` summarizes only a long context of a bead without
structured options, only when `DISPLAYD_REVIEW_AI=1` and a provider was
registered with `set_provider(fn)`. None ships. Options, ids and answer text
never reach or come from it; a provider failure leaves the plain context; the
result is shown labelled `AI summary:`.
