# Event schema, version "1"

The contract between collectors and the server. Source-independent: nothing in it depends on the DOM of
chatgpt.com/claude.ai or on a transcript file layout. Unknown fields are **rejected** (typos fail loudly);
additive changes will bump `schema_version`.

`POST /api/v1/events` (one event) and `POST /api/v1/events/batch` (`{"events": [...]}`, ≤ 500).

| Field | Req | Notes |
|---|---|---|
| `schema_version` | | `"1"` (default) |
| `source` | ✔ | lowercase `[a-z0-9_.-]`, e.g. `claude_code`, `chatgpt_web`, `claude_web`, `synthetic` |
| `device_id` | ✔ | stable per machine/browser profile; part of event identity |
| `device_label` | | human name |
| `external_event_id` | ✔ | stable id from the source (collectors must derive it deterministically) |
| `revision` | | ≥ 1; bump when the *same* event's content changes (edit, finished streaming) |
| `external_session_id` | | omit for events without a known session |
| `session_title` | | only if the source provides one |
| `project` | | `{key, name?}`: `key` is the source-side identity (cwd, git remote, URL). Mapped through project aliases; unmatched keys stay *unresolved* (kept as `project_hint`); with `name`, an existing project of the same name (case-insensitive, not generic names like `src`) gets the key as an alias (`VIBEVAULT_MATCH_PROJECTS_BY_NAME`, default on), else `VIBEVAULT_AUTO_CREATE_PROJECTS` creates one |
| `event_type` | ✔ | `session_start session_end message reasoning tool_call tool_result file_change error system other` |
| `actor` | ✔ | `user assistant tool system` |
| `occurred_at` | ✔ | timezone-aware ISO-8601 (naive timestamps are rejected) |
| `sequence` | | source ordering hint; breaks timestamp ties |
| `content` | | text; indexed for search; ≤ 1 MB |
| `metadata` | | free JSON object (model, usage, git branch, …) ≤ 256 KB |
| `parent_external_event_id` | | e.g. a tool result's call |
| `tool` | | `{tool_call_id, name?, command?, input?, output?, exit_code?, is_error?, started_at?, finished_at?}`; call and result halves merge by `tool_call_id` |
| `file_changes` | | `[{path, operation: create/edit/delete/read/rename, old_path?, diff?, before_hash?, after_hash?}]` |
| `redaction` | | what the collector did: `{status: none/redacted/unscanned, count, rules{name:count}}` — never matched text |
| `raw_payload` / `raw_payload_ref` | | original record (redacted) and/or a pointer to it |

## Semantics

* **Idempotent**: identity = `(source, device_id, external_event_id, revision)`. Same identity and same
  semantic content (type, actor, content, tool, file_changes) → `duplicate`. Same identity, different
  content → `conflict` (original kept; send a higher `revision` for a real edit).
* **Per-item results**: the response lists `created | duplicate | conflict | rejected | skipped` per event,
  with sanitized validation errors (never echoing input). A bad item doesn't fail the batch.
  HTTP 422 is only for an invalid envelope; 413 for oversize; 401/403 for auth.
* **`skipped`**: device collection disabled, project collection disabled, or session excluded/deleted
  (tombstone). Retries of events that were already stored still return `duplicate`.
* **Out-of-order**: any arrival order is accepted; sessions are created on first sight; the highest
  revision becomes current regardless of arrival order; tool call/result pair when both are present.
* **Redaction**: the server re-scans every string field (content, metadata, tool, diffs, raw payload, title)
  unless `VIBEVAULT_SERVER_REDACTION=false`, and merges the counts with the collector's.
* **Server ID**: `id = UUIDv5(namespace, source|device|external_event_id|revision)`.
