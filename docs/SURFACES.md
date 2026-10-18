# Registered display surfaces

A **surface** is anything that can show content: the DisplayD panel itself, a
Sidecar browser tab, an iPad, a wall screen. Surfaces register with displayd,
describe their viewport / renderer / touch capabilities, and consumers send
content to one surface (or one region of it) without knowing which renderer
draws it. The API is renderer-agnostic; DisplayD's on-device HTML renderer is
**one** implementation behind it.

> **Status.** Built and merged in stages (0.9.0 to 0.14.0): the registry, event
> streams and columns, persistence, the panel drawing its own columns, MCP
> tools, the review-bead decision page, and now the per-panel scale and panel
> identity restore. "Implemented vs pending" below is the honest ledger of what
> is covered by automated tests and what still needs a real device.

## INITIAL vs LATER

**INITIAL (this change):** multi-column event streams (streams, columns,
surfaces and regions are four distinct things); renderer-agnostic surface
registration/discovery with viewport + capabilities incl. touch, updates,
lifecycle and targeting; DisplayD's HTML renderer as one implementation;
tests for multiple surfaces, differing viewports and touch. The data model
already carries a **stable panel id** (separate from the surface id) and a
**versioned per-panel config object**, so the LATER items fit without rework.

**Since built:** persistence (0.10.0), the panel drawing columns (0.11.0), MCP
tools (0.12.0), the review-bead decision page (0.13.0), the per-panel
accessibility scale and stable panel identity with config restore (0.14.0;
see "Panel scale" and "Panel identity" below).

**Still LATER / not in this repo:**

1. MIT licensing and the measured Go-vs-Rust review for Sidecar: Sidecar is its
   own project now (written in Go); displayd is the on-device renderer and the
   surfaces API.
2. Optional demo/join mode: explicitly enabled, off by default, ephemeral
   revocable pairing code/token with expiry, no unauthenticated public
   endpoint, minimal grant (register + use); trust boundary and network
   defaults documented. Not built: the only gate is the existing bearer token.

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
- Registry persistence across restart (`surface_store.py`; `tests/test_surface_store.py`).
- The panel draws its own columns in their layout regions
  (`column_painter.py`; `tests/test_column_painter.py`, `tests/test_column_view.py`).
- Per-panel accessibility scale (`panel.config.scale`) and stable panel
  identity with config restore (`surface_identity.py`;
  `tests/test_surface_identity.py`, scale cases in `tests/test_column_view.py`
  and `tests/test_column_painter.py`).
- The whole product at once, through the real headless daemon: phone (390x844,
  tap/swipe), iPad (1024x768, two regions, tap/pinch), wall display (1920x1080,
  three regions, no touch) and the panel, one stream feeding differently
  composed columns, targeting by surface/region/column/capability, touch
  picking a column, stream-specific follow-on actions reaching the right
  producer, a panel moving to another surface
  (`tests/test_multi_surface_scenario.py`).

**Pending deployment / device verification (not done here):**

- Panel columns draw only the `log`, `calls` and `status` views (see "The
  panel draws its columns"); a stream's other views (`chart`, `controls`...)
  show a one-line notice on the panel and are a Sidecar's to draw.
- Follow-on actions are queued for the producer; no on-panel follow-on UI yet
  (the panel's touch is `tap` only and a column draws no buttons).
- The panel's drawing is checked as renderer params; `scale` is verified in the
  template's CSS and plan arithmetic, not on the physical panel. Look at one
  column at scale 1 and 2 after a deploy to confirm legibility.
- A panel's remembered config is displayd-side memory keyed by `panel.id`; no
  real Sidecar client sends a `panel.id` yet, and nothing authenticates a
  claimed id beyond the bearer token, so any holder of the token can claim any
  id (and so read back its config). Treat ids as names, not secrets.

- Not deployed to the live jumbotron / `~/displayd` on lnx-server. No
  `deploy.sh` run, nothing restarted.
- No real Sidecar (browser, iOS) client exists yet; every Sidecar in the tests
  is a simulated HTTP client. Real-device latency, reconnect behaviour and
  clock skew are unverified.
- The `displayd-html` path needs the built litehtml engine
  (`tools/build_litehtml.sh`); the tests prove the region rebind and param
  plumbing, not pixels. Eyeball one region update on the real panel after a
  deploy.
- Event/action rings, touch events and content slots are in memory and start
  empty after a restart (see "Persistence"); producers republish.
- MCP: `mcp_surface_tools.py` exposes the whole API as tools (see "MCP
  tools"); the server is the existing `mcp_server.py`, so a client that already
  registers it gets them after it restarts the server.
- Real panel touch stays `touch.py`'s: it reports `tap` only, and only while
  its heartbeat (`POST /touch/announce`) is fresh. `POST /surfaces/panel/touch`
  is refused (409) so two writers cannot fight.

## Persistence

The registry (registered surfaces with their `panel` block and columns,
expiry tombstones, declared event streams) is saved to one JSON file:
`state/surfaces.json` next to the policy file (`DISPLAYD_SURFACES`
overrides; on lnx-server `~/displayd/state/surfaces.json`). `deploy.sh`
neither ships nor deletes it (no `--delete`; `/state/` is git-ignored).
`GET /state` -> `surfaces` shows the path, generation, last write/error and
what the boot restored.

- **Writes** are atomic (temp file in the same directory, fsync,
  `os.replace`) and never fail a request: an error shows in `last_error` and
  the next change retries. Structural changes write at once; heartbeats are
  coalesced to one write per 5 s, so a crash can lose a few seconds of lease
  refresh, which only means an early `410` and a re-register.
- **Format** is versioned (`format`, currently 1) and walked up through
  `surface_store.MIGRATIONS` (`{from_version: fn(doc) -> doc}`). Every record
  is re-validated on load by the same `normalize` a registration passes
  (so `panel.config` follows today's rules); an invalid record is dropped and
  counted in `restored`, the rest load. A corrupt file or one written by a
  newer format is moved to `surfaces.json.unreadable-<n>`, never overwritten
  or deleted, and the registry starts empty.
- **Leases**: a surface whose `expires_at` passed while the daemon was down
  loads as a tombstone (`410`, re-register), not a ghost; one still within its
  lease keeps its remaining time. Tombstones persist, so `410` vs `404` holds
  across restarts.
- **Not stored**: event/action rings, touch events, content slots. After a
  restart a new `generation` starts every seq counter at `generation << 32`
  (generation 0, a fresh install, keeps seq 1, 2, ...), so a consumer's
  `since=<seq>` is never repeated or regressed; it simply sees later events.
- Panel identities (`identities`: `panel.id` -> last config, version, surface)
  are stored in the same file, additive within format 1; a malformed entry is
  dropped and counted in `restored.identities_invalid`.
- A stored record can never shadow the built-in `panel`.
- The built-in panel's own `panel` block (its columns) is stored under
  `panels` in the same file: additive within format 1, so a file without it
  loads as "no columns set".

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

## Panel scale

`panel.config.scale` is a number in `[0.5, 3]` (default 1; anything else is a
`400`, the whole spec refused). It scales text and UI together, per panel, and
travels with the panel's config (so it persists, and is restored with the
identity below). On the built-in panel, `POST /surfaces/panel {"panel":
{"config": {"scale": 2}}}` redraws its columns at once. The column view takes
its pixel metrics from `surface_column_view.metrics(scale)`; a larger scale
fits fewer, shorter rows (`plan(rect, scale)`), so nothing scrolls or clips. A
Sidecar reads `scale` from its own `panel.config` and applies it itself.

## Panel identity

`panel.id` names a logical panel, separate from the surface it appears on.
displayd remembers the config each id last had (`surface_identity.py`, persisted
under `identities` in `state/surfaces.json`). On `POST /surfaces`, the
**response** says how the panel was recognised in `panel.identity`:

| `identity` | meaning |
|---|---|
| `anonymous` | no `panel.id`; nothing remembered or restored |
| `new` | an id displayd has not seen; registers as given |
| `kept` | the caller sent its own `panel.config`; it wins and is remembered |
| `restored` | known id, no config sent: the remembered config is applied; `panel.restored` = `{from_surface, dropped, at}` |
| `unusable` | known id whose stored config is newer than this build, has no migration, or is invalid here; registers empty, memory untouched |

Columns whose region the new surface lacks are `dropped` (listed), never
fatal, and a restore does not overwrite the memory, so a small phone cannot
shrink what a wall display configured. Configs of an older `panel.version` walk
up `surface_panel.MIGRATIONS` (`{from_version: fn(config) -> config}`; none are
needed yet). The memory holds the 256 most recent ids. Moving a logical panel
between surfaces is therefore: deregister the old surface, register the new one
with the same `panel.id`.

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

Its `panel` block is settable (`POST /surfaces/panel/columns {"columns": [...]}`
or `POST /surfaces/panel {"panel": {...}}`; nothing else on a built-in can be
updated). Columns are validated against the regions it has *when set*, and
setting `columns` replaces the list. A column whose region is not in the
current layout is **dormant**: kept (and persisted), listed in
`panel.dormant`, hidden from `panel.config.columns` and drawn again when its
region exists -- a layout change never breaks `GET /surfaces`.

## The panel draws its columns

A column `{stream, view, region}` on the panel is drawn by the panel itself
(`column_painter.py`), with the html renderer and `html-templates/column.html`:

- only while a layout is active, only into the column's region, and only that
  region's renderer is replaced (`_rebind_region`); other regions keep their
  threads. With no layout nothing is drawn (a column never hijacks a single
  view);
- repainted when an event is published, a stream is declared/removed, the
  columns change or a layout is set; bursts are coalesced (0.25 s) and a region
  whose drawn params did not change is not rebound;
- sized to the region: `surface_column_view.plan(rect)` gives the rows and
  characters that fit (rows from the region height, characters from its
  width), so a narrow side column and the wide centre truncate differently. The
  pixel metrics live in `column.html` and are pinned against `plan` by
  `tests/test_column_view.py`; the template is fixed-px, like every html view;
- painting is autonomous and never counts as operator activity;
- one content delivery to a column's region replaces its drawing until the
  column or its stream changes again;
- `GET /state` -> `surfaces.columns` reports `painted`, the drawn
  `{region: column}` and per-region `errors`.

Views drawn: `log` (newest events, one per row), `calls` (`name state`),
`status` (the latest event as `key: value`). A view displayd does not draw, or
one the stream does not offer, shows a one-line notice.

## Code map

`surface_model.py` (vocabulary + validator) · `surface_registry.py` (leases,
discovery) · `surface_channels.py` (per-surface content/event streams) ·
`surface_target.py` (select + resolve) · `surface_touch.py` (event validation)
· `surface_renderers.py` (implementations) · `surface_hub.py` (deliver) ·
`surface_streams.py` (event streams) · `surface_columns.py` (columns + actions)
· `surface_panel.py` (panel block, scale) · `surface_identity.py` (identity restore) · `surface_builtin_panel.py` (the panel's settable block, dormant columns) · `surface_patch.py` (update merge) · `surface_column_view.py` / `column_painter.py` (the panel draws columns) · `surface_store.py` / `surface_restore.py` (persistence) · `daemon_surfaces.py` (panel wiring) ·
`daemon_http_surfaces.py` / `daemon_http_streams.py` (routes).

## MCP tools

`mcp_surface_tools.py` is one table, one row per tool, each mapping its
arguments to exactly one HTTP route (no second code path). `mcp_server.py`
lists them next to the core tools and dispatches through `call`; the
`mcp_client` gained `api_delete`.

| Tool | Route |
| --- | --- |
| `surfaces_list` (kind, renderer, content_type, touch, min_width, min_height, region, labels) | `GET /surfaces?...` |
| `surface_get` / `surface_register` / `surface_update` | `GET` / `POST /surfaces[/<id>]` |
| `surface_heartbeat` / `surface_deregister` | `POST .../heartbeat` / `DELETE /surfaces/<id>` |
| `surface_send` (target: id, `id/region`, or `{select, all?}`) | `POST /surfaces/content` |
| `surface_events` (since, wait) / `surface_touch_report` | `GET .../events` / `POST .../touch` |
| `surface_columns` / `surface_columns_set` / `column_action` | `GET`/`POST .../columns`, `POST .../columns/<c>/action` |
| `streams_list` / `stream_declare` / `stream_get` / `stream_remove` | `/event-streams[/<id>]` |
| `stream_publish` / `stream_events` / `stream_actions` | `/event-streams/<id>/events`, `/actions` |

Ids are quoted into one path segment. `wait` is clamped below
`DISPLAYD_TIMEOUT` (and to 25 s) so a held-open read is never mistaken for an
unreachable panel. Bad arguments are a tool error (`isError`), displayd's own
refusals come back as `displayd refused (HTTP n)`. `tests/test_mcp_surfaces.py`
pins every route with a stubbed client and walks register -> send -> touch ->
columns -> actions against a headless daemon. The tool catalog needs no
registration change: tools are listed live by the running server.
