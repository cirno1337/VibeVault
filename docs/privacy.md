# Privacy, redaction and retention

The archive can contain source code, credentials and personal data. Treat the database file, the
backups and the collector spools as sensitive.

## What is collected (Claude Code collector)
User prompts, assistant text, tool calls and results (including command output), file paths and edit
diffs the model requested, session boundaries, model name/usage, working directory and git branch, and
the original transcript line (redacted). **Not** collected unless you opt in: reasoning/"thinking" blocks
(`capture_thinking`). Images are replaced by a placeholder. Nothing is collected from other applications,
environment variables, or files that Claude Code did not put in the transcript.
Browser extension: visible message text, conversation URL/id/title and order on claude.ai and chatgpt.com
only — never cookies, storage, the prompt draft or uploaded files; private windows off by default; redacted
in the browser before queueing. Details: [its spec](collectors/browser-extension.md).

## Redaction
Pipeline: **collector redacts before spooling/sending** → **server re-scans on ingest** (defence in depth).
Built-in rules: private-key blocks, Anthropic/OpenAI/GitHub/GitLab/Groq/Google/Slack/Stripe/AWS keys, JWTs,
`Bearer` tokens, credentials in URLs, `password|secret|token|api_key = value` assignments, and
dictionary keys such as `authorization`, `cookie`, `password`. Add your own with a JSON rules file
(`[{"name","pattern","group"?}]`). Matches become `[REDACTED:<rule>]`; the event records `redaction.status`, a count and
per-rule counts — **never the secret**. Placeholders (`${VAR}`, `<token>`, `xxxx`) are left alone.

> **Regex redaction is not perfect.** It will miss secrets with unusual formats, secrets split across
> events, secrets in binary/base64 blobs, and passwords in prose; it will occasionally redact harmless text.
> It reduces risk; it cannot guarantee that no secret is stored. Keep the vault on a private network, encrypt the
> disk, and review `redaction.unscanned` in the dashboard (events that bypassed scanning).

Events stored while server redaction is off and not scanned by the collector are marked `unscanned` and are
**never sent to AI providers**.

## Controls
* **Global off switch** — collector: `vibevault-collector disable` (hooks/sync do nothing; queue is kept) or `enabled = false`.
* **Per project / path** — collector `exclude_paths` globs and a `.vibevault-ignore` file in the project (or any parent) directory; server: project `collection_enabled=false`.
* **Per device** — server: `PATCH /devices/{id}` (UI: Sources & AI).
* **Per session** — collector: `vibevault-collector exclude-session <id>` (drops queued events, ignores future ones); server: `PATCH /sessions/{id} {excluded:true}` or `DELETE /sessions/{id}` (removes stored events; tombstone prevents re-upload).
* **AI** — off unless `VIBEVAULT_PROVIDERS` is set; external providers additionally need `VIBEVAULT_ALLOW_EXTERNAL_AI=true`; per-project `ai_processing_enabled`.
* **Audit** — server `audit_log` and collector local `audit` table record operations as counts and identifiers only. Logs never contain event content, tokens or request bodies.

## Secrets handling in the repo
No secrets are committed; `.env` is git-ignored; `.env.example` has placeholders; weak/placeholder tokens are rejected at start-up; collector tokens come from the environment or a `0600` file (looser permissions are refused). Spool directory is `0700`, spool DB `0600`.

## AI processing policy
Nothing is sent to any AI provider unless a provider is explicitly configured. Only redacted, scanned events from projects that allow AI are sent. Archived content is framed as data in prompts. Model output is never executed and cannot become "confirmed" without a human.

## Retention and deletion
* Default: **keep everything** (it is your memory). Nothing is deleted automatically.
* `vibevault prune --older-than-days N [--yes]` deletes events older than N days (dry-run by default) and then removes model-derived items left without evidence.
* Per-session deletion via API/UI; per-source purge for synthetic data: `vibevault purge-synthetic`.
* Backups contain everything that existed when they were taken; deletion does not reach old backups — rotate them.
* Claude Code itself may remove old transcripts from disk after its own retention period; VibeVault's copy is independent.
