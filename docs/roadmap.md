# Plan and roadmap

## Plan followed for milestone 1
1. Inspect repo (empty) and toolchain. 2. Architecture + schema. 3. Vertical slice ingest → SQLite → FTS → UI.
4. Tests (schema, ingest/dedupe, ordering, search, redaction, auth/limits, provenance). 5. Durable queue + deterministic processing.
6. Collector interfaces + Claude Code collector. 7. Provider layer + optional extraction. 8. Docker + docs.

## Milestone 1 — done (see README status table for caveats)
Server, archive, search, UI, queue, Claude Code collector (synthetic-fixture tested), provider layer (fake-tested), Docker files (unbuilt), extension *spec*.

## Milestone 2 — make it real on your machines
* Install the Claude Code collector on the Arch PC; **validate the parser against real transcripts**, fix schema drift, add real-shape fixtures (scrubbed).
* Deploy to the home server (compose, Tailscale), set up backup timer + restore drill.
* Confirm the Docker image builds/runs; add CI (pytest, ruff, frontend build, image build).
* Install on the Ubuntu server (second device identity).

## Milestone 3 — browser extension
Done: adapters for chatgpt.com and claude.ai, normalizer, redactor port, IndexedDB queue, options/popup,
per-conversation exclusion (spec: docs/collectors/browser-extension.md). Adapters fitted to the real 2026-10 DOM
(scrubbed fixtures). Open: confirm live capture in Brave end to end; re-save fixtures when a site changes.

## Milestone 4 — AI knowledge
Run the local llama.cpp provider end-to-end; tune prompts on real sessions; verify Groq/Gemini endpoints; quality checks; cross-session passes (contradictions, project dependencies); question-answering view ("why did I…?") that cites events; evidence quotes/spans.

## Later
Orchestrator service for Wake-on-LAN/idle shutdown (separate, secured, opt-in); PostgreSQL migration set if SQLite limits appear; optional embeddings for semantic search; per-project retention rules; multi-token management UI; at-rest encryption of the DB file.

## Known limitations (milestone 1)
* `session.status` is open/ended only; "interrupted" is not inferred.
* Search is lexical (FTS5/porter); no fuzzy/semantic search; searches event text, not tool inputs/diffs or file paths unless mirrored in `content`.
* Deleting events does not rewrite backups.
* Auth is bearer tokens in env vars (no rotation UI, no per-device token binding); no TLS in-app; the UI keeps the admin token in `sessionStorage`.
* Single SQLite writer; fine for one user, not for heavy concurrent ingestion.
* The in-process worker uses a thread; AI calls are synchronous inside job steps (bounded by chunk/time budgets).
