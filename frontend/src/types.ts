export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface Redaction { status: "none" | "redacted" | "unscanned"; count: number; rules: Record<string, number> }
export interface ToolExec {
  tool_call_id: string; name: string | null; command: string | null; input: Record<string, unknown> | null;
  output: string | null; exit_code: number | null; is_error: boolean | null;
  call_event_id: string | null; result_event_id: string | null;
}
export interface FileChange { path: string; operation: string; old_path: string | null; diff: string | null }
export interface VEvent {
  id: string; source: string; device: { id: number; key: string; label: string | null };
  session_id: number | null; external_session_id: string | null; external_event_id: string; revision: number;
  is_current: boolean; project_id: number | null; project_hint: string | null; event_type: string; actor: string;
  occurred_at: string; ingested_at: string; ingested_by: string | null; sequence: number | null; ingest_seq: number;
  content: string; metadata: Record<string, unknown>; parent_external_event_id: string | null;
  tool_call_id: string | null; redaction: Redaction; processing_status: string; raw_payload_ref: string | null;
  has_raw_payload: boolean; raw_payload?: unknown; tool: ToolExec | null; file_changes: FileChange[];
}
export interface Derived {
  id: number; kind: string; origin: "deterministic" | "model" | "human"; epistemic_status: string; state: string;
  title: string; body: string; data: Record<string, unknown>; project_id: number | null; session_id: number | null;
  provider: string | null; model: string | null; prompt_version: string | null; confidence: number | null;
  created_at: string; confirmed_by: string | null; confirmed_at: string | null;
  evidence: { event_id: string; note: string | null }[]; interpretation: boolean;
  session_title?: string | null; // list endpoint only
}
export interface Session {
  id: number; external_session_id: string; title: string | null; source: string; preview?: string;
  device: { id: number; key: string; label: string | null }; project: { id: number; name: string } | null;
  project_hint: string | null; started_at: string | null; last_event_at: string | null; ended_at: string | null;
  status: string; excluded: boolean; event_count: number; derived?: Derived[];
}
export interface Project {
  id: number; slug: string; name: string; description: string | null; collection_enabled: boolean;
  ai_processing_enabled: boolean; aliases: string[]; session_count: number; event_count: number;
  last_event_at: string | null; derived?: Derived[];
}
export interface Job {
  id: number; kind: string; status: "pending" | "running" | "completed" | "failed"; payload: Record<string, unknown>;
  checkpoint: Record<string, unknown>; result: Record<string, unknown> | null; attempts: number; max_attempts: number;
  run_after: string; last_error: string | null; last_error_at: string | null; status_note: string | null;
  created_at: string; finished_at: string | null;
}
export interface Stats {
  totals: { events: number; sessions: number; projects: number; devices: number; unresolved_sessions: number; derived_items: number };
  events_by_type: Record<string, number>; events_by_source: Record<string, number>;
  events_last_24h: number; events_last_7d: number; last_ingested_at: string | null;
  processing: { events_pending: number; events_processed: number; jobs: Record<string, number> };
  redaction: { events_redacted: number; events_unscanned: number }; ai: { enabled: boolean }; version: string;
}
export interface SearchHit {
  event_id: string; session_id: number | null; project_id: number | null; source: string; event_type: string;
  actor: string; occurred_at: string; snippet: string; session_title: string | null; project_name: string | null;
}
export interface Device {
  id: number; source: string; device_key: string; label: string | null; collection_enabled: boolean;
  first_seen_at: string; last_seen_at: string; event_count: number;
}
export interface ProviderStatus {
  name: string; state: string; local: boolean; detail: string | null; model: string | null;
  requests_today: number; daily_request_budget: number | null; retry_after_seconds: number | null;
}
