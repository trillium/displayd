# Registered display surfaces

A **surface** is anything that can show content: the DisplayD panel itself, a
Sidecar browser tab, an iPad, a wall screen. Surfaces register with displayd,
describe their viewport / renderer / touch capabilities, and consumers send
content to one surface (or one region of it) without knowing which renderer
draws it. The API is renderer-agnostic; DisplayD's on-device HTML renderer is
**one** implementation behind it.

> **Status.** The renderer-agnostic registered-surfaces layer did not exist on
> `main` when this was written, so this change *is* that minimal layer plus the
> multi-Sidecar capabilities on top. See "Implemented vs pending" below.

## INITIAL vs LATER

**INITIAL (this change):** multi-column event streams (streams, columns,
surfaces and regions are four distinct things); renderer-agnostic surface
registration/discovery with viewport + capabilities incl. touch, updates,
lifecycle and targeting; DisplayD's HTML renderer as one implementation;
tests for multiple surfaces, differing viewports and touch. The data model
already carries a **stable panel id** (separate from the surface id) and a
**versioned per-panel config object**, so the LATER items fit without rework.

**LATER (roadmap only, NOT built here):**

1. Per-panel accessibility scale (text + UI), stored in `panel.config`.
2. Stable Sidecar/panel fingerprint distinct from the surface; reconnect
   restores config (stream subscription, renderer, layout/region, scale);
   unknown-identity and config-migration rules (`panel.version`); a panel
   moving between surfaces. Today only the fields exist (`panel.id`,
   `panel.version`, `panel.config`); nothing restores anything.
3. MIT license for Sidecar; after a functional end-to-end baseline, a
   *measured* Go-vs-Rust performance/language review (throughput, latency,
   resources, concurrency, deploy cost). No speculative rewrite.
4. Review-bead decision UI: load a review bead, deterministic decision page
   from its structured fields, pre-filled option buttons, selection mapped
   back to the bead's action with confirmation.
5. Optional demo/join mode: explicitly enabled, off by default, ephemeral
   revocable pairing code/token with expiry, no unauthenticated public
   endpoint, minimal grant (register + use); trust boundary and network
   defaults documented. Until then the only gate is the existing bearer token.

## Implemented vs pending

**Implemented and covered by automated tests** (`tests/test_surfaces.py`,
`tests/test_surfaces_http.py`, `tests/test_surface_columns.py`, headless `DISPLAYD_FAKE_FB=1` daemon, fake clock
for leases):

- Register / update / heartbeat / deregister, TTL leases, expiry (410 vs 404).
- Discovery with filters (kind, renderer, content type, touch interaction,
  minimum viewport, region, labels).
- Viewport geometry (`scale`, `insets`, derived `usable` rect), named regions,
  per-region content types and touch.
- Touch capability declaration + a validated touch-event report/read path
  (closed interaction vocabulary, point-in-viewport, per-region hit-testing,
  multi-point limits).
- Targeting (`id`, `id/region`, structured, `select` first-match / broadcast).
- Content delivery: pull (long-poll) for any Sidecar, push for renderers
  displayd runs itself.
- The panel as a built-in surface, and the `displayd-html` implementation
  drawing into **one panel region** without disturbing the others.
- Event streams (producer-declared, bounded ring, `seq`, long-poll), columns
  (stream + view + region bindings, validated atomically), column targeting,
  and stream-specific follow-on actions queued for the producer.
- The panel block (`id`, `version`, `config`) with atomic merge on update.
- Bearer-token gate on every verb; other routes untouched.

**Pending deployment / device verification (not done here):**

- The panel itself does not yet *draw* columns: columns are stored, validated,
  discoverable and targetable on every surface, and a Sidecar renders its own,
  but composing a column's stream into a panel region with an HTML template is
  not built. Deliver content to a column's region (`displayd-html`) to draw it.
- Follow-on actions are queued for the producer; no on-panel follow-on UI yet.

- Not deployed to the live jumbotron / `~/displayd` on lnx-server. No
  `deploy.sh` run, nothing restarted.
- No real Sidecar (browser, iOS) client exists yet; every Sidecar in the tests
  is a simulated HTTP client. Real-device latency, reconnect behaviour and
  clock skew are unverified.
- The `displayd-html` path needs the built litehtml engine
  (`tools/build_litehtml.sh`); the tests prove the region rebind and param
  plumbing, not pixels. Eyeball one region update on the real panel after a
  deploy.
- Registry state is in memory: a daemon restart forgets registered Sidecars
  (they re-register on their next 410/404 heartbeat). Persistence is a
  deliberate non-goal for now.
- No MCP tools for surfaces yet (`mcp_server.py` derives tools from
  `GET /renderers`; surfaces are plain HTTP for now).
- Real panel touch stays `touch.py`'s: it reports `tap` only, and only while
  its heartbeat (`POST /touch/announce`) is fresh. `POST /surfaces/panel/touch`
  is refused (409) so two writers cannot fight.

## Model

```json
{
  "id": "ipad", "name": "Studio iPad", "kind": "sidecar",
  "renderer": {"name": "swiftui", "version": "1", "content_types": ["text", "scene"]},
  "viewport": {"width": 1024, "height": 768, "scale": 2,
               "insets": {"top": 24, "bottom": 20}},
  "touch": {"supported": true, "interactions": ["tap", "pinch"], "max_points": 2},
  "regions": [{"name": "left", "rect": {"x": 0, "y": 24, "w": 512, "h": 724},
               "content_types": ["text"], "touch": true}],
  "labels": {"room": "studio"},
  "panel": {"id": "studio-ipad", "config": {"columns": [
    {"id": "agent", "stream": "agent", "view": "log", "region": "left"}]}},
  "ttl_seconds": 30
}
```

- `panel.id` is a stable token **distinct from the surface id** (optional, may
  be shared); `panel.version` is the config schema version (currently 1);
  `panel.config` is a free-form object (16 KB cap) of which `columns` is the
  only key displayd interprets. Update merges `panel.config` one level deep.

- `id`/region names are short lowercase tokens; `content` is a reserved id.
- `viewport.usable` is derived (viewport minus insets); every region must fit.
  With no `regions`, one `full` region covers the usable viewport.
- Touch interactions: `tap`, `double_tap`, `long_press`, `swipe`, `drag`,
  `pinch`, `multi_touch`. `pinch`/`multi_touch` need `max_points >= 2`.
- A bad spec is refused whole (`400` naming the field); nothing half-valid is
  stored. Updates merge a partial spec and re-validate atomically.

## API

| Method | Path | Meaning |
|---|---|---|
| GET | `/surfaces[?kind=&renderer=&content_type=&touch=&min_width=&min_height=&region=&label.k=v]` | discover live surfaces (touch: an interaction, `any`, or `none`) |
| POST | `/surfaces` | register (upsert): `201` created, `200` replaced |
| GET | `/surfaces/<id>` | one surface: `404` unknown, `410` expired |
| POST | `/surfaces/<id>` | update (merge a partial spec; cannot rename) |
| DELETE | `/surfaces/<id>` | deregister |
| POST | `/surfaces/<id>/heartbeat` | extend the lease; `410` means re-register |
| POST | `/surfaces/<id>/content` | `{"region"?, "content"}` send to one surface |
| POST | `/surfaces/content` | `{"target", "content"}` send anywhere |
| GET | `/surfaces/<id>/content[?since=&wait=&region=]` | a Sidecar pulls (long-poll, `wait` <= 25s) |
| POST | `/surfaces/<id>/touch` | a Sidecar reports a touch event |
| GET | `/surfaces/<id>/events[?since=&wait=]` | consumers read touch events (long-poll) |
| GET/POST | `/surfaces/<id>/columns` | read the columns (with stream info and `view_supported`) / replace them |
| POST | `/surfaces/<id>/columns/<col>/action` | `{"action", "event_seq"?, "value"?}` follow-on action for the column's stream |
| GET/POST | `/event-streams` | list / declare a stream (`id`, `event_types`, `views`, `actions`) |
| GET/DELETE | `/event-streams/<id>` | one stream / remove it |
| POST | `/event-streams/<id>/events` | producer publishes `{"type", "data"}` (`415` for an undeclared type) |
| GET | `/event-streams/<id>/events[?since=&wait=]` | subscribe (long-poll) |
| GET | `/event-streams/<id>/actions[?since=&wait=]` | producer reads follow-on actions |

Discovery also filters by `panel=<id>` and `stream=<id>`.

## Streams, columns, surfaces, regions

Four separate concepts: a **stream** is what happens (declared by its
producer, with event types, views it can be shown as, and follow-on actions);
a **surface** is where it can show; a **region** is a rect of a surface; a
**column** binds `{stream, view, region}` and lives in the panel config, one
per region. The same stream can feed columns on many surfaces, and one surface
can compose several columns side by side. A column's `view` is only checked
against the stream when read (`view_supported`), so a column may be bound
before its stream is declared. Follow-on actions are stream-specific: only
actions the stream declared are accepted (`409`), and the queued action
carries `surface`, `panel`, `column`, `region` and `event_seq` for the producer.

Content is `{"type": "<content type>", ...}`, at most 256 KB. `type` must be one
the region accepts (`415` otherwise); everything else belongs to the renderer
implementation.

### Targets

`"phone"` · `"ipad/left"` · `{"surface": "ipad", "region": "left"}` ·
`{"surface": "ipad", "column": "agent"}` (resolves to the column's region) ·
`{"select": {...}, "region"?, "all"?}`.

`select` keys: `kind`, `renderer`, `content_type`, `touch`, `min_width`,
`min_height`, `region`, `panel`, `stream`, `labels`. Unknown keys are refused. Without `all`, the
first match **that accepts the content type** gets it; with `all`, every match
is tried and the reply lists `delivered` and `skipped` (with reasons), so one
wrong-shaped Sidecar never hides the rest. A surface with several regions and
no `full` is never guessed at: name a region (`409`), or a broadcast skips it.

### Touch events

A Sidecar reports `{"interaction", "x", "y", "region"?, "points"?, "dx"?,
"dy"?, "scale"?, "duration_ms"?}` in surface pixels. displayd checks the
interaction is declared, the point is inside the usable viewport, the region
(named, else the smallest touch-enabled region containing the point) exists,
and `points` is within `max_points`. The stored event carries a region-local
`local` coordinate and a per-surface `seq`; consumers read with
`GET .../events?since=<seq>`.

## Renderer implementations

The hub looks an implementation up by the surface's `renderer.name`
(`surface_renderers.py`: `validate(content)`, `render(surface, region,
content)`). Two ship:

- `displayd-html` — the on-device litehtml renderer. Content
  `{"type": "displayd-html", "template": "status.html", "vars": {...}}`
  (a named trusted template + escaped variables, never markup). For the panel
  with no layout it is `show("html", ...)`; with a layout it rebinds **only the
  targeted region's** renderer/thread and leaves every other region running.
- `relay` — the fallback for any renderer name displayd does not run: the
  content is stored in the region's slot and the Sidecar pulls and draws it
  itself.

Register another implementation with `SurfaceHub.register_renderer(impl)`;
`tests/test_surfaces.py` does so with a renderer displayd has never heard of.

## The panel surface

`GET /surfaces/panel` is always present, never expires, and is re-described on
every read: viewport = the framebuffer, regions = the active layout's regions
(names slugged to tokens, e.g. `Top Bar!` -> `top-bar`) or one `full`, touch =
`tap` while the touch service heartbeat is fresh, else none.

## Code map

`surface_model.py` (vocabulary + validator) · `surface_registry.py` (leases,
discovery) · `surface_channels.py` (per-surface content/event streams) ·
`surface_target.py` (select + resolve) · `surface_touch.py` (event validation)
· `surface_renderers.py` (implementations) · `surface_hub.py` (deliver) ·
`surface_streams.py` (event streams) · `surface_columns.py` (columns + actions)
· `surface_panel.py` (panel block) · `daemon_surfaces.py` (panel wiring) ·
`daemon_http_surfaces.py` / `daemon_http_streams.py` (routes).
