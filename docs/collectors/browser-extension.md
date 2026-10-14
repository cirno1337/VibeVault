# Browser extension — specification

**Status: implemented in [`extension/`](../../extension/) (build/install: [extension/README.md](../../extension/README.md)).**
Adapters are tested against scrubbed saves of the real claude.ai / chatgpt.com pages (2026-10) plus synthetic
fixtures for streaming/edits/regenerations. The extension loads in Brave and authenticates against the server;
capture from a live page through to stored events has not been observed end to end yet. Contracts:
[`extension/src/adapters/types.ts`](../../extension/src/adapters/types.ts).

## Implementation notes
* Code map: `src/adapters/{claude,chatgpt}.ts` (all selectors) + `text.ts` (generic DOM → text/Markdown),
  `content.ts`, `normalizer.ts`, `redactor.ts` (port of `redaction.py`, checked against the shared vectors in
  `tests/fixtures/redaction_vectors.json` from both pytest and Vitest), `queue.ts` (IndexedDB + delivery),
  `background.ts` (service worker), `popup.ts`, `options.ts`.
* Content scripts never read settings or the token: they ask the service worker for an allow/deny decision
  before parsing; the worker re-checks the policy and that the sender runs on the site it claims (its host:
`sender.url` keeps the initial URL after in-app navigation, so it cannot vouch for the conversation id).
* Identity: claude.ai messages use the page's `data-turn-key` (a reply is `<prompt key>-hub-reply`), chatgpt.com
  messages their message ids; index-based ids are only a fallback. claude.ai virtualises the transcript, so
  `sequence` is the row's absolute `data-index` and messages are captured as they scroll into view.
* Claude Code on the web (`claude.ai/code/session_<id>`) shares the chat's transcript DOM and is collected as
  its own source `claude_code_web` (own on/off switch). A reply is several text blocks interleaved with tool
  cells: the text is kept, tool activity is skipped (not captured as tool events yet). One long agent turn can
  span many rows that share a turn key, so reply ids there are `<turn key>:<row position>`. Title comes from the
  pane header, `project.key` = `repo:<repository name>` from the header, so sessions can be aliased to the
  same project as the local Claude Code collector's working directory.
* chatgpt.com may keep other conversations mounted (several `<main>`s, background ones with emptied replies):
  only the container whose `data-chatgpt-selection-conversation-id` matches the URL is read, else nothing.
* `occurred_at`: the page's `<time datetime>` where shown, carried to neighbouring messages without one.
* `external_session_id` = conversation id; `metadata` = `{url, branch?, time_source, truncated?}`.
  Content above 250k characters is truncated, already in the content script (server cap is 1 MB).
* A transport/auth failure backs off the whole queue (30 s doubling, max 30 min, checked every minute).
* Per-conversation exclusion drops its fingerprints and queued events locally. Deleting already-sent events
  needs the admin token (`DELETE /sessions/{id}`), which the extension deliberately never holds; do it in the UI.

## Shape
Chromium Manifest V3. Content script per site → **site adapter** (only code that knows selectors) →
`ParsedConversation` → shared **normalizer** (→ `EventIn`) → **redactor** (same rules as `redaction.py`, ported) →
**IndexedDB queue** in the service worker → **uploader** to the configured VibeVault URL with an ingest token.

## Permissions (minimum)
* `host_permissions`: `https://chatgpt.com/*`, `https://claude.ai/*` only. No `<all_urls>`, no `cookies`, `history`, `tabs`, `webRequest`, `debugger`.
* `storage` (settings, exclusions), `alarms` (retry). The VibeVault server origin is requested at runtime via `optional_host_permissions` once the user configures it.
* Content scripts are declared for those two hosts only.

## Capture rules (to document in the options page)
* **Read**: visible text of user/assistant messages in the *currently open conversation tab* on those two sites, the conversation URL/id/title, and message order.
* **Never read**: cookies, storage, auth headers, hidden app state, other tabs/sites, the prompt box draft, uploaded file contents, account/profile data.
* **Transmitted when**: only while collection is enabled for the site *and* the conversation is not excluded; after a response finishes streaming (debounced), batched, to the configured server only. Nothing is sent to third parties. The popup shows state (enabled / queued N / last success / last error).
* Private/incognito: disabled by default.

## Behaviour
* **Incremental**: `MutationObserver` on the adapter's observation root, debounced; `streaming:true` parses are held back until stable.
* **Identity**: `externalEventId = <conversationId>:<sourceMessageId | i<index>>`; when the same id's text changes (edit, regeneration, streaming finished) emit `revision+1`. Regenerated branches get distinct ids when the site exposes a branch id, else a revision bump.
* **Dedupe**: keep a per-conversation map of fingerprints (`MessageFingerprint`); the server is idempotent anyway.
* **Ordering**: `sequence = message index`; `occurred_at` = page timestamp if exposed, else first-seen time (monotonic).
* **Offline**: queue batches in IndexedDB; retry with exponential backoff (alarms); drop only after explicit server rejection; surface counts.
* **Controls**: global switch, per-site switch, per-conversation exclusion (stores only the id; also callable to delete server-side via the API).
* **Project**: none known from web chats → send `project: {key: <conversation URL>}` (stays unresolved until you alias it).

## Testing plan
Adapters are pure `parse(document, url)` functions tested against **saved HTML fixtures** (jsdom/Vitest) — one fixture set per site and per layout change, covering: plain chat, code blocks, long streaming response, edited prompt, regenerated answer, empty conversation, non-conversation page. Normalizer tests: fingerprint/revision logic, redaction before queueing, exclusion enforcement. Queue tests: offline → online replay without duplicates.

## Risks
Both sites change their DOM without notice → keep selectors isolated, fail closed (collect nothing, show "adapter outdated") rather than collect garbage. No undocumented private APIs are used as the *only* mechanism. Site terms of service apply to your own data export/automation; check them.
