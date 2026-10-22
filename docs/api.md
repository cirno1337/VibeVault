# API (v1)

Base path `/api/v1`. Interactive OpenAPI at `/docs`, machine-readable at `/openapi.json`.
`GET /healthz` (liveness) and `GET /readyz` (DB reachable and migrations at head → 200, else 503) are unauthenticated.

**Auth**: `Authorization: Bearer <token>`.
* `admin` token (`VIBEVAULT_ADMIN_TOKEN`): everything. Used by the web UI.
* `ingest` tokens (`VIBEVAULT_INGEST_TOKENS=name=token,…`): `POST /events`, `POST /events/batch`, `GET /me` only. The name is recorded as `ingested_by`.
* `read` tokens (`VIBEVAULT_READ_TOKENS=name=token,…`): read-only — `GET /projects`, `/sessions` (+`/{id}`, `/{id}/events`), `/events` (+`/{id}`), `/search`, `/me`. No writes, jobs, `derived`, `audit`, `stats`, `devices` or `providers` (403). Intended for integrations such as Khoj. Ingest tokens cannot read.

**Errors** are always `{"error": {"code", "message", "details"?}}`. Validation details contain `loc/msg/type` only, never the input value.

**Pagination** `limit` (≤ 200) + `offset` → `{items, total, limit, offset}`. **Time filters** `since`/`until` (timezone-aware ISO-8601).

| Method & path | Scope | Notes |
|---|---|---|
| `POST /events`, `POST /events/batch` | ingest | see [event-schema](event-schema.md) |
| `GET /search?q=` | admin | filters: `project_id source event_type actor since until include_superseded`; terms AND-ed, last term prefix, `"phrases"`; snippets use private-use markers U+E000/U+E001 |
| `GET /sessions`, `/sessions/{id}`, `/sessions/{id}/events` | admin | filters: `project_id source device since until unresolved title` |
| `PATCH /sessions/{id}` | admin | `excluded`, `project_id` |
| `DELETE /sessions/{id}` | admin | deletes evidence, leaves an exclusion tombstone |
| `GET /events`, `/events/{id}` | admin | detail includes raw payload, parent/children, revisions, citing derived items |
| `GET/POST /projects`, `GET/PATCH /projects/{id}`, `POST /projects/{id}/aliases` | admin | adding an alias re-resolves unresolved sessions |
| `POST /projects/{id}/merge` `{into}` | admin | moves sessions, events, aliases, derived items into `into`, deletes the project |
| `DELETE /projects/{id}` | admin | only when nothing refers to it (409 `project_not_empty` otherwise: merge instead) |
| `GET /devices`, `PATCH /devices/{id}` | admin | per-device collection switch |
| `GET /jobs`, `/jobs/{id}`, `POST /jobs`, `POST /jobs/{id}/retry` | admin | `POST /jobs` kinds: `digest_session`, `summarize_session` (409 if no AI configured) |
| `GET /derived`, `POST /derived/{id}/confirm` | admin | human confirmation |
| `GET /providers`, `/stats`, `/audit`, `/me` | admin (`/me`: any) | |

Compatibility: breaking changes get a new prefix (`/api/v2`); event `schema_version` evolves independently.
