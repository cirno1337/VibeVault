# AI providers

**Off by default.** With `VIBEVAULT_PROVIDERS` empty, ingestion, search, browsing, redaction and deterministic processing all work; AI jobs can't be enqueued.

## Interface (`vibevault/providers`)
`Provider`: `name`, `is_local`, `model`, `configured()`, `check_ready()`, `complete(CompletionRequest) -> CompletionResponse`.
Typed failures: `ProviderRateLimited(retry_after)`, `ProviderUnavailable`, `ProviderBadResponse`; messages never contain prompts, responses or hostnames from transport errors.
One implementation, `OpenAICompatProvider` (chat-completions), serves all three targets:

| Name | Endpoint | Notes |
|---|---|---|
| `local` | `VIBEVAULT_PROVIDER_LOCAL_BASE_URL` (llama.cpp `llama-server`, e.g. `http://main-pc:8080/v1`) | no key needed; readiness = `GET /models` must answer |
| `groq` | preset `https://api.groq.com/openai/v1` | needs API key + model |
| `gemini` | preset `https://generativelanguage.googleapis.com/v1beta/openai` | needs API key + model |

> **Never run against the real services.** These are exercised only with fakes and `httpx.MockTransport`. The presets,
> JSON-mode support and model names must be verified on first real use. A native Gemini client can be added behind the same interface if the compatibility endpoint is a poor fit.

## Selection & states
Providers are tried in `VIBEVAULT_PROVIDERS` order. Each reports an explicit state: `disabled`, `unconfigured`, `blocked_external`,
`ready`, `rate_limited`, `budget_exhausted`, `unavailable` (visible under *Sources & AI*). External (non-`local`) providers are
`blocked_external` until `VIBEVAULT_ALLOW_EXTERNAL_AI=true`. Optional `…_DAILY_REQUEST_BUDGET` per provider (counted in `provider_usage`, UTC day);
requests/tokens are recorded where the API reports usage. Timeouts per provider (`…_TIMEOUT`, default 60 s). Keys come only from the environment.

## Extraction (`knowledge.py`, job `summarize_session`)
Events (current revisions, scanned, non-empty) → chunks (~12k chars, each event capped at 1.5k) → provider → JSON `{summary, items[]}` with `E<n>` evidence refs → drafts → on completion activated, previous model output superseded (human-confirmed items kept).
Rate limit → checkpoint is saved, job `Defer`s (no attempt used) and resumes at the next chunk. Invalid JSON/unavailable → retry with backoff, then `failed` and visible/retryable in the UI.
Current kinds: summary, decision, error_resolution, task, fact, dependency, contradiction. **Not yet done**: cross-session reasoning (contradictions between older/newer decisions, project dependency graph), prompt/quality evaluation on real data, structured provenance spans (quotes) beyond event IDs.

## Project classification (`classify.py`, job `classify_sessions`)
For every session **without a project** (never classified, or grown since), the model gets the project list
(name, folders, titles of recent sessions; opted-out projects are left out) and the conversation's messages
(redacted; up to `VIBEVAULT_CLASSIFY_MAX_CHARS` = 16k, start and end kept; on HTTP 413 — e.g. Groq's per-request
token limit — retried with half as much, down to 2k; a session that still fails is skipped and tried again next night) and answers `existing` / `new` / `none`.
Stored as a `project_assignment` derived item with the opening message as evidence:
* `existing` with confidence ≥ `VIBEVAULT_CLASSIFY_AUTO_APPLY_CONFIDENCE` (0.85): applied at once (audited; shown under
  *Projects → Project suggestions → Recently assigned automatically*; change it on the session page);
* `existing` below that, or `new`: a suggestion — `POST /derived/{id}/apply` (creates the suggested project, or joins
  a same-named one) or `POST /derived/{id}/dismiss`; buttons on the Projects and session pages;
* `none` (personal topics) or an invented project id: recorded, nothing changes.
Runs nightly at `VIBEVAULT_CLASSIFY_AT` (local `VIBEVAULT_TIMEZONE`, once per day, catches up after downtime), or via
`POST /jobs {"kind": "classify_sessions"}` / the *Run classification now* button. Rate limits defer the job with its
checkpoint, like summarisation; `…_DAILY_REQUEST_BUDGET` caps the calls (one request per session).

## Future orchestration (designed, intentionally not implemented)
1. Archive immediately → 2. deterministic indexing → 3. bounded analysis jobs → 4. cloud providers while budget/quota allow → 5. checkpoint and stop on rate limit → 6. leave remaining jobs `pending` → 7. *(future, separate secured service)* wake the PC → 8. probe `local` readiness (`check_ready`, already used) → 9. resume from checkpoints (already works) → 10. *(future, opt-in)* shut down only when idle.
Steps 7 and 10 (Wake-on-LAN, shutdown) will live in a **separate** authenticated orchestrator, not in this server. This server has no remote-execution endpoint and will not get one.
