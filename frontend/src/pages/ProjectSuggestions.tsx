import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../hooks";
import type { Derived, Page } from "../types";
import { ago } from "../util";

const pct = (c: unknown) => (typeof c === "number" ? `${Math.round(c * 100)}%` : "");

/** Model-made project assignments: open suggestions to apply/dismiss, and what was applied automatically. */
export function ProjectSuggestions({ onChanged }: { onChanged?: () => Promise<void> }) {
  const items = useFetch<Page<Derived>>("/derived?kind=project_assignment&state=active&limit=200");
  const providers = useFetch<{ ai_enabled: boolean }>("/providers");
  const [msg, setMsg] = useState("");
  const all = items.data?.items ?? [];
  const open = all.filter((d) => !d.data.applied && d.data.decision !== "none");
  const auto = all.filter((d) => d.data.applied && !d.confirmed_at).slice(0, 15);

  async function act(path: string, done: string) {
    try { await api(path, { method: "POST" }); setMsg(done); await items.reload(); await onChanged?.(); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  async function runNow() {
    try { await api("/jobs", { method: "POST", body: { kind: "classify_sessions" } }); setMsg("Classification queued (see Processing)."); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }

  if (!items.data || (!open.length && !auto.length && !providers.data?.ai_enabled)) return null;
  return (
    <section className="card">
      <h2>Project suggestions</h2>
      <p className="muted small">Made by the AI model for sessions without a project. Confident matches to existing projects are applied automatically; change them on the session page if wrong.</p>
      {open.length === 0 ? <p className="small">No open suggestions.</p> : (
        <ul className="list">{open.map((d) => (
          <li key={d.id}>
            <Link to={`/sessions/${d.session_id}`}>{d.session_title || `Session ${d.session_id}`}</Link>
            {" → "}<strong>{d.data.decision === "new" ? `new project "${String(d.data.new_project_name)}"` : String(d.data.project_name)}</strong>
            <span className="muted small"> · {pct(d.data.confidence)} · {d.body}</span>
            <div className="row">
              <button onClick={() => act(`/derived/${d.id}/apply`, "Applied.")}>{d.data.decision === "new" ? "Create project & assign" : "Assign"}</button>
              <button onClick={() => act(`/derived/${d.id}/dismiss`, "Dismissed.")}>Dismiss</button>
            </div>
          </li>
        ))}</ul>
      )}
      {auto.length > 0 && (<>
        <h3>Recently assigned automatically</h3>
        <ul className="list">{auto.map((d) => (
          <li key={d.id}><Link to={`/sessions/${d.session_id}`}>{d.session_title || `Session ${d.session_id}`}</Link> → {String(d.data.project_name)}
            <span className="muted small"> · {pct(d.data.confidence)} · {ago(d.created_at)}</span></li>
        ))}</ul>
      </>)}
      <div className="row">{providers.data?.ai_enabled && <button onClick={runNow}>Run classification now</button>}{msg && <span className="small">{msg}</span>}</div>
    </section>
  );
}
