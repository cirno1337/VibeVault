# Claude Code collector

Two machines (Arch PC, Ubuntu server) = two **independent** collectors, each with its own
`device_id`, spool and destinations. Implemented in `src/vibevault/collectors/` (stdlib only).

## What was verified, and how

Checked against the official hooks reference (`https://code.claude.com/docs/en/hooks`) during development:

* Hooks receive JSON on stdin with the common fields `session_id`, `transcript_path`, `cwd`, `hook_event_name`
  (plus `permission_mode`, `prompt_id`, agent fields, …). `SessionStart` adds `source` (startup/resume/clear/compact/fork) and optional `model`.
* `transcript_path` is **"written asynchronously and may lag the in-memory conversation"** — so a hook cannot rely on the transcript already containing the current turn. The design therefore treats hooks as *notifications* and re-scans the file.
* `Stop` payloads carry `last_assistant_message`; `PostToolUse` has `tool_name`, `tool_input`, `tool_use_id`.
  I could **not** confirm from the documentation I could read whether `PostToolUse` includes the tool *result*, nor read the exact schemas of `UserPromptSubmit`/`SessionEnd` (the page was truncated). **The collector does not depend on any of that**: it stores no hook body except session boundary metadata, and gets prompts/results from the transcript.
* 33 hook events exist; the collector only needs five (see `examples/claude-code-settings.example.json`).

## What is NOT verified

The **transcript JSONL schema is not an official, documented contract**. The parser follows the shape
observed in practice (`type` user/assistant/system; `uuid`, `parentUuid`, `sessionId`, `timestamp`, `cwd`, `gitBranch`,
`message.content[]` blocks `text`/`thinking`/`tool_use`/`tool_result`, optional top-level `toolUseResult`) and is defensive:
unknown record types and malformed lines are skipped and counted in the local audit, never fatal.
Tests use **hand-written synthetic fixtures** (`tests/fixtures/claude_code_synthetic.jsonl`), not captured sessions.
**Before relying on it:** run `vibevault-collector backfill --dry-run`, then scan one real transcript against a throw-away
server and compare against the UI; check each Claude Code upgrade. Not covered: sub-agent transcripts as separate sessions,
rewinds/branching of conversations, compaction summaries, hook-injected context.

## Data flow

```
hook (≤ a few ms) ──► local spool (SQLite, 0600): outbox + notifications
                         │  spawns detached `vibevault-collector sync` (skipped if one is running)
sync: scan transcript from saved byte offset (complete lines only)
      → parse → redact → outbox   (offset advanced in the same transaction)
      → deliver batches to each destination (retry/backoff, split on 413, dead-letter oversized)
```

* **Never blocks Claude Code**: the hook only parses stdin and writes one local SQLite transaction; network and transcript parsing happen in a detached process. The hook swallows every error, prints nothing (stdout from some events can be injected into the model context) and exits 0.
* **Durable & replay-safe**: events keep deterministic IDs (`<line uuid>:<block>`, `tool:<id>:call|result`); replays dedupe locally and on the server.
* **Backoff**: `5s·2^n` capped at 1 h per destination; 401/403 waits 15 min and shows in `status`.
* **Backfill is explicit**: history is only imported by `vibevault-collector backfill [--since-days N]`.
* **Mapping**: text → `message`; tool_use → `tool_call` (Bash command in `tool.command`; Edit/Write/MultiEdit/NotebookEdit/Read attach `file_changes` marked `file_change_status: requested` — the matching `tool_result` shows whether it succeeded); tool_result → `tool_result` (parent = call); `system` → `system`; hook SessionStart/SessionEnd → `session_start`/`session_end`. Thinking only if `capture_thinking = true`.
* Project identity = the git work tree containing `cwd` (else `cwd`; `~` itself never counts as a root for folders
  below it), so `cd src/` mid-session stays in the same project. Name = the folder name. The server maps it via
  aliases, else joins a project with the same name, else auto-creates one.

## Install plan (later; not done)

1. `pip install vibevault` (or `pipx`) on each machine.
2. Copy `examples/collector.example.toml` → `~/.config/vibevault/collector.toml` (`chmod 600`), set `device_id`, destination URL; put the ingest token in the environment or a `0600` token file. Create one ingest token per machine on the server (`VIBEVAULT_INGEST_TOKENS=pc=…,server=…`).
3. Merge the hooks from `examples/claude-code-settings.example.json` into `~/.claude/settings.json`.
4. Install the systemd user timer from `examples/systemd-collector.example.txt` (safety net).
5. `vibevault-collector status`; run a short Claude Code session; confirm it appears in the UI.

Commands: `hook`, `sync`, `scan`, `flush`, `backfill`, `status`, `enable`/`disable`, `exclude-session <id>`.
