# VibeVault

A private, self-hosted archive of AI-assisted software development. It stores the **original
evidence** (prompts, responses, tool calls, commands, outputs, file edits) from several tools in
one source-independent format, makes it searchable and browsable, and keeps any AI-generated
interpretation strictly separate from — and traceable to — that evidence.

**Status: milestone 1 complete, milestones 2–4 in progress.** The server, archive, search, job queue, web UI,
Claude Code collector and browser extension (claude.ai, Claude Code on the web, chatgpt.com) are implemented and tested.
AI provider integrations (summaries, nightly project classification) are implemented but only tested with fakes —
not yet validated against real models. See [Status](#status) for the honest breakdown.

## Quick start (development)

Requirements: Python ≥ 3.11, Node ≥ 20.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
(cd frontend && npm ci && npm run build)          # builds the UI into frontend/dist

export VIBEVAULT_ADMIN_TOKEN=$(openssl rand -hex 32)       # full access / web UI login
export VIBEVAULT_INGEST_TOKENS=laptop=$(openssl rand -hex 32)   # collectors: ingest only
export VIBEVAULT_STATIC_DIR=$PWD/frontend/dist
vibevault serve                                    # http://127.0.0.1:8787 (migrates the DB on start)
```

Open <http://127.0.0.1:8787>, sign in with the admin token. To see something immediately, load the
clearly-labelled **synthetic** dataset (never mix it with real data; `vibevault purge-synthetic` removes it):

```bash
VIBEVAULT_TOKEN=<ingest or admin token> vibevault seed
```

UI development with hot reload: run `vibevault serve` and `cd frontend && npm run dev` (proxies `/api`).

### Tests

```bash
pytest                                   # backend + collector (173 tests)
ruff check src tests                     # lint
cd frontend && npm test && npm run build # UI unit tests, typecheck + production build
cd extension && npm ci && npm test       # browser extension (82 tests); `npm run build` makes the loadable bundle
```

## Deploy (Docker Compose)

```bash
cp .env.example .env     # set VIBEVAULT_ADMIN_TOKEN / VIBEVAULT_INGEST_TOKENS
docker compose up -d --build
```

Published on `127.0.0.1` only by default. See [docs/deployment.md](docs/deployment.md) for Tailscale
exposure, backup/restore, retention and upgrades.

## Collect from Claude Code

`vibevault-collector` (installed with the package; Python stdlib only at runtime) uses documented
Claude Code hooks to *notify* a local spool and reads the session transcript incrementally. It
redacts secrets before anything is stored or sent, never blocks Claude Code, and queues events
durably while the server is unreachable. Two machines = two independent collectors with
their own `device_id`. See [docs/collectors/claude-code.md](docs/collectors/claude-code.md).

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | layers, data flow, key decisions, PostgreSQL path |
| [docs/event-schema.md](docs/event-schema.md) | the versioned event contract, idempotency & ordering rules |
| [docs/schema.md](docs/schema.md) | database tables and relationships |
| [docs/api.md](docs/api.md) | endpoints, auth, errors (live OpenAPI at `/docs`) |
| [docs/privacy.md](docs/privacy.md) | what is captured, redaction and its limits, controls, retention |
| [docs/collectors/claude-code.md](docs/collectors/claude-code.md) | hooks/transcripts: what is verified, what is not; install plan |
| [docs/collectors/browser-extension.md](docs/collectors/browser-extension.md) | Manifest V3 extension (claude.ai, chatgpt.com): spec and implementation notes |
| [docs/providers.md](docs/providers.md) | AI provider interface, policy, budgets, future pipeline |
| [docs/deployment.md](docs/deployment.md) | Docker, network exposure, backup/restore, retention |
| [docs/roadmap.md](docs/roadmap.md) | plan, milestone status, next steps |

## Status

| Area | State |
|---|---|
| Ingestion API (single/batch, idempotent, revisions, out-of-order, per-item results) | ✅ implemented, tested |
| SQLite archive + Alembic migrations + FTS5 search | ✅ implemented, tested |
| Projects / sessions / devices, unresolved projects, alias re-resolution | ✅ implemented, tested |
| Auth (admin + ingest-scoped tokens), limits, structured errors & logs | ✅ implemented, tested |
| Redaction pipeline (collector-side and server-side), custom rules | ✅ implemented, tested — best-effort only |
| Durable job queue (retry/backoff, dedupe, leases, checkpoints, restart recovery) | ✅ implemented, tested |
| Deterministic processing (per-session digest) | ✅ implemented, tested |
| Projects: git-root keys, same-name joining, merge/delete, manual assignment | ✅ implemented, tested |
| Nightly AI project classification (auto-apply above a confidence threshold, otherwise suggestions) | ⚠️ implemented, tested with fakes; **not yet run against a real model** |
| Web UI (dashboard, projects, sessions, timeline, search, event evidence, jobs, sources) | ✅ implemented; verified in headless Chromium against a live server |
| Claude Code collector (hooks + incremental transcript + spool + delivery) | ✅ implemented; tested on **synthetic** fixtures only |
| AI extraction pipeline + provider layer (local/Groq/Gemini/DeepSeek via OpenAI-compatible API) | ⚠️ implemented and tested with fakes/mock transport; **never run against a real model or service** |
| Docker image / Compose (optional external reverse-proxy network) | ⚠️ written, Compose config validated; **image not built in CI** (no CI yet) |
| Browser extension (claude.ai chat + Claude Code on the web, chatgpt.com; Manifest V3) | ✅ implemented; adapters tested on scrubbed real page saves (2026-10); live capture into the server verified in Brave |
| Real-machine Claude Code install, real transcripts, Tailscale, llama.cpp | ❌ not done (needs your machines) |
| Wake-on-LAN, remote shutdown, remote command execution | ❌ deliberately not implemented |

## Repository layout

```
src/vibevault/         server (FastAPI), models, ingest, search, jobs, worker, providers, CLI
  collectors/          collector-side code (stdlib only): spool, Claude Code parser, delivery
  migrations/          Alembic migrations (0001 creates schema + FTS5)
frontend/              React + TypeScript + Vite UI
extension/             Manifest V3 browser extension (TypeScript, esbuild, Vitest)
tests/                 pytest suite; tests/fixtures are synthetic
examples/              collector config, Claude Code hook settings, systemd timer
docs/
```
