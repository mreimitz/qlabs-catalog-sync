# QLabs Catalog Sync Console — app spec

The operator console for the QLabs Catalog Sync engine (RM-06). One administrator uses
this to register the source and target endpoints, define sync pairs, decide exactly
which source catalog objects are in scope, preview and dry-run the writes the engine
would make, and review what each run actually did. It is the only user interface this
project has: `console/` is a standalone SPA built with Vite + React on the
`@elabs-ai/components-*` (`brand-ui`) packages, served by the engine's own FastAPI
process at the same origin as its REST API (`/api/...`), `/healthz` and `/metrics` — no
separate deployment, no CORS (decision C8).

## Archetype: `data-app`

Every screen this console needs is "browse/manage records with filters, health status
and bulk-ish actions" — exactly the `brand-ui-new-app` recognition table's description
of the `data-app` archetype ("browse/manage records, logs, admin console, back-office,
filters, bulk actions"), not `dashboard` (no KPI-first landing page is the point of
entry), `settings` (there is no single profile/preferences surface — configuration
*is* the product), or any of the others. Per the `brand-ui-enterprise` skill's
professional/consumer/marketing classification, this is squarely **professional**
register: a single administrator does this repeatedly as their job, so the whole app
uses the **calm, dense-by-design** register, not a lighter consumer one. Per that
skill's shell archetypes, this is shell **B — enterprise admin** (icon sidebar for the
top-level views below, topnav, breadcrumb, and a right-side detail panel for
inspecting one object) rather than shell A's tool/workspace canvas — nothing here is a
freeform canvas or editor.

## Theme and dials

This `@elabs-ai/components-cli@4.0.0` install ships the generic `light`/`dark` token
themes (confirmed via `brand-ui info --json`), not the qlik-bright/qlik-dark/blueprint
theme set some other qlabs products use — this project only depends on the public
`@elabs-ai/components-*@4.0.0` packages, nothing brand-specific. The console's theme
switcher (built in T13.2) is therefore **System / Light / Dark**, the direct
equivalent of the enterprise baseline's mandated switcher, using whatever theme names
this token package actually exports.

- `theme`: `light` (the shipped default; `dark` is available and equally supported —
  neither is a marketing accent, both must render correctly, C7/C8 notwithstanding).
- `taste.register`: `product` — this is an app a trained operator uses for work, judged
  as product UI, never against a marketing bar.
- `taste.density`: `compact` — this is a dense, operator-facing internal tool: source
  trees with thousands of candidates, rule lists, run history. Compact density (tighter
  spacing and role type per the token dial) keeps more of that visible without
  scrolling, which is the whole value of the selection and runs screens.
- `taste.motion`: `system` — respect the OS reduce-motion preference; nothing here
  needs its own motion opinion.
- `taste.expressiveness`: `0` — restrained. No decorative flourishes; every visual
  choice is a token, not a brand statement.

`entities` and `surfaces` are deliberately **not** populated in the JSON spec below.
The scaffold's entity generator produces a standalone `interface`/`ColumnDef[]` pair
that is never wired into the emitted screen, and this console's real domain types come
from T12.8's generated `src/api/generated/schema.ts` (`EndpointOut`, `SyncPairOut`,
`SelectionRuleOut`, `RunSummaryOut`, ...) — inventing a second, hand-guessed model here
would only give a later task something wrong to delete. Likewise `surfaces` only
relabels the four fixed template nav entries (`data`/`tables`/`home`/`settings`)
positionally; with six real screens to place, that mapping is more misleading than
useful. The application shell's real navigation is T13.2's to design against the
screen list below, not this scaffold's job to approximate.

## Screens

Prose ownership per WP13's task table (`planning/Roadmap/RM-06-sync-console/implementation-plan.md`).
This scaffold emits a shell and one placeholder template screen; every screen below is
built by a later task against this shell, not by T13.1.

### Sign-in (T13.2)

Single administrator (C7). A username/password form posts to
`POST /api/auth/session` and receives `{username, csrf_token, expires_at}`
(`SessionInfo`) — the session itself lives in an `HttpOnly` `SameSite` cookie the SPA
never reads or sets. `csrf_token` is the only credential-adjacent value the SPA holds,
and it lives in memory (a request client module or React context), never in
`localStorage`/`sessionStorage`/a URL/a log line. On load, `GET /api/auth/session`
re-establishes that same token from an existing cookie without a fresh sign-in.
`DELETE /api/auth/session` signs out. A failed sign-in is a normal 401 with the shared
`ErrorModel`, not a special case.

### Application shell (T13.2)

Enterprise admin shell (archetype B): collapsible icon sidebar with the five top-level
views below, theme switcher (System / Light / Dark) and session/sign-out control in
the shell chrome, breadcrumb for drill-down (pair → selection/dry-run/runs). Toasts
(Sonner) for command results (rule reordered, healthcheck run, pair paused). A
right-side detail panel (`Sheet`/`Drawer`) for inspecting one object (a run, a
capability manifest, a config-change entry) without leaving the list.

### Endpoints (T13.3)

List (`GET /api/endpoints`) with health state (`HealthState`: healthy / degraded /
unhealthy) and secret-reference resolve status (`GET /api/endpoints/{name}/secret-resolve`)
per row — **never** the resolved secret value, only whether it resolves. "Register"
starts from `GET /api/connectors` — the connectors *this running image discovered*,
each with its capability manifest — never a free-text connector name or an install
step (C6: naming an instance of a connector already present, not installing a
package). The settings form for a new or edited endpoint is generated from the
connector's own config schema, not hand-built per connector. A healthcheck
(`POST /api/endpoints/{name}/healthcheck`) that reports `unhealthy` is a normal,
successfully-rendered response describing the endpoint's state — never an error toast.
A capability-manifest viewer (`GET /api/connectors` → `CapabilityManifestOut`) shows,
per entity type, `supported`, `identity_keys`, and per-field `FieldCapabilityMode`
(`rw`/`ro`/`na` — distinct meanings: `ro` can be read but never written, `na` has no
equivalent or cannot currently be read at all; both render as "never written" but the
reason differs and the UI says which).

### Sync pairs (T13.4)

List/create/edit (`GET|POST /api/pairs`, `PATCH /api/pairs/{id}`): source endpoint,
target endpoint, target Qlik space, entity types, cadence (seconds) and jitter, manual
edit policy (`ManualEditPolicy`: default source-wins, overridable per entity type or
per field to preserve local edits), and **activation opt-in**. Activation defaults to
**off**, and wherever it is toggled the UI states the consequence plainly: turning it
on makes the resulting Qlik data product **discoverable tenant-wide**. This is not a
cosmetic flag.

### Selection (T13.5) — the screen the console exists for

A lazy, paged source tree (`GET /api/pairs/{id}/source-tree` — **offset-based**
pagination: `{nodes, offset, limit, has_more, next_offset}`, not a cursor), each node
marked included/excluded **and which rule decided** (`DecisionSource`: `override` /
`rule` / `default`). An ordered rule editor (`GET|POST /api/pairs/{id}/rules`,
`POST /api/pairs/{id}/rules/reorder`): rules render in evaluation order (`ordinal`),
never insertion order, because **evaluation runs top to bottom and the last matching
rule decides** — the UI must never imply "first match wins" or hide the order.
Reordering sends the **complete ordered list of rule ids** to `/reorder`, never a
move-to-index delta. Per-object overrides (`GET|POST /api/pairs/{id}/overrides`) are
pinned by **qualified name** (`catalog.schema`, `object_id` on `SelectionOverrideOut`)
— never an opaque id; the API rejects the wrong form and the UI must not offer it. A
live preview (`POST /api/pairs/{id}/preview`, `PreviewOut`) shows counts for both
object and dataset scope (`ScopeCountsOut`: `total`, `included`, `excluded`,
`undetermined`) plus a bounded sample; `rule_set_source` (`stored`/`draft`) labels
whether the numbers are the saved configuration's own or an unsaved edit's, and the
two must never be visually conflated. **`undetermined` is its own state, not folded
into `excluded`**: a tag rule evaluated against a source that cannot report tags
returns "cannot tell", which is a real, separate outcome the UI renders with its own
treatment (not a green "excluded, fine" and not a red "included, fine" — a distinct
neutral state), because `excluded` and `undetermined` overlap and a run can still
touch an object the preview called undetermined.

### Dry run (T13.6)

`POST /api/pairs/{id}/dry-run` returns the planned write set grouped by data product,
with unresolved dataset members and unresolvable owners called out by name, not
silently dropped. Nothing here writes anything — it is a plan, and the screen says so.

### Runs (T13.7)

History (`GET /api/runs` — **keyset** pagination: `{items, limit, has_more,
next_cursor}`, an opaque cursor, no page numbers) and detail
(`GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/issues`). `RunRecordStatus` has
**three** meaningfully different resting states plus one transient one — `running` is
not a fourth kind of failure, it is a cycle in flight (`in_progress` is exposed
explicitly on `RunSummaryOut` so the UI never infers it from the string); `ok`,
`partial` and `failed` are the finished verdicts, and `skipped` is a clean no-op, not
an error. A row still `running` long after it should have finished is evidence the
process that started it died, not a fifth status the UI invents wording for. Orphans
(`RunOrphanIssueOut`) are source objects that vanished — the UI reports them as
**orphaned**, never as "deleted", because **the engine never deletes anything in
Qlik**. Run-now, pause and resume (`POST /api/pairs/{id}/run-now|pause|resume`,
`GET /api/pairs/{id}/run-status` → `RunControlStatusOut`) are pair-level controls
shown alongside the pair, not buried only in run history. The configuration change
log (`GET /api/config-changes` — also keyset-paginated) is reachable from here or from
the endpoint/pair screens it documents, answering "when did this start syncing".

## Security constraints the UI must never violate (C7, C8)

- The session cookie is `HttpOnly` **on purpose**. No code in this app may attempt to
  read `document.cookie` for it.
- The CSRF token comes only from the sign-in response body and from
  `GET /api/auth/session` — it is bound to the session, not a constant, and is sent as
  `X-CSRF-Token` on every `POST`/`PUT`/`PATCH`/`DELETE`.
- Never place a password, a secret value, a resolved secret reference, or the session
  token in `localStorage`, `sessionStorage`, a cookie this app sets, a URL, or a log
  line.
- The API client's base URL is the page's own origin, always — never a configurable
  absolute URL, never an environment variable naming another origin. Same origin, no
  CORS, by construction (C8).

```json
{
  "archetype": "data-app",
  "theme": "light",
  "title": "QLabs Catalog Sync Console",
  "standalone": true,
  "intent": {
    "purpose": "Operate the Databricks-to-Qlik metadata sync engine: register endpoints, define sync pairs, decide exactly which source objects are in scope, preview and dry-run the planned Qlik writes, and review run history.",
    "audience": "The single administrator who operates this deployment of the sync engine (decision C7 — one operator identity, no roles).",
    "scale": "production"
  },
  "taste": {
    "register": "product",
    "density": "compact",
    "motion": "system",
    "expressiveness": 0
  }
}
```
