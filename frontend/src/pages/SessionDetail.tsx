import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, DerivedCard, Diff, ErrorBox, Evidence, EventLinkMeta, Loading, Pager } from "../components";
import { useFetch } from "../hooks";
import type { Page, Project, Session, VEvent } from "../types";
import { fmtTime } from "../util";

function ToolCard({ call, result }: { call: VEvent; result?: VEvent }) {
  const t = call.tool ?? result?.tool;
  const failed = t?.is_error || (t?.exit_code != null && t.exit_code !== 0);
  const running = !result && !t?.output && t?.exit_code == null;
  const isBash = t?.name === "Bash" || !!t?.command;
  return (
    <details className={`toolcard ${failed ? "failed" : ""}`}>
      <summary>
        <span className="actor">{isBash ? "terminal" : "tool"}</span>
        <code>{isBash ? `$ ${t?.command ?? call.content}` : call.content}</code>
        {failed && <Badge tone="bad">failed{t?.exit_code != null ? ` (exit ${t.exit_code})` : ""}</Badge>}
        {!failed && t?.exit_code === 0 && <Badge tone="good">exit 0</Badge>}
        {running && <Badge>no result yet</Badge>}
      </summary>
      <div className="toolbody">
        {call.file_changes.map((f, i) => <Diff key={i} f={f} />)}
        {call.file_changes.length > 0 && <p className="muted small">File operations are as requested by the model; the result below shows whether the tool succeeded.</p>}
        {t?.input && !t.command && <pre className="out">{JSON.stringify(t.input, null, 2)}</pre>}
        {t?.output != null && <pre className={`out ${failed ? "out-bad" : ""}`}>{t.output || "(no output)"}</pre>}
        <EventLinkMeta e={call} />{result && <> · result: <Link to={`/events/${result.id}`}>details</Link></>}
      </div>
    </details>
  );
}

/** Assign the session to a project, or start a new project from it (e.g. an idea discussed in a chat). */
function ProjectPicker({ s, onChanged }: { s: Session; onChanged: () => Promise<void> }) {
  const projects = useFetch<Page<Project>>("/projects?limit=200");
  const [target, setTarget] = useState("");
  const [name, setName] = useState("");
  const [msg, setMsg] = useState("");
  async function assign(projectId: number | null) {
    try { await api(`/sessions/${s.id}`, { method: "PATCH", body: { project_id: projectId } }); setMsg(""); await onChanged(); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  const suggestion = s.project ? undefined : s.derived?.find((d) => d.kind === "project_assignment" && !d.data.applied && d.data.decision !== "none");
  async function act(path: string) {
    try { await api(path, { method: "POST" }); setMsg(""); await onChanged(); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  async function createAndAssign() {
    try { const p = await api<Project>("/projects", { method: "POST", body: { name: name.trim() } }); setName(""); await assign(p.id); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  return (
    <section className="card">
      <h2>Project</h2>
      <p className="small">{s.project ? <>Assigned to <Link to={`/projects/${s.project.id}`}>{s.project.name}</Link>.</> : "Not assigned to a project."}
        {s.project_hint && <> Reported as <code className="chip">{s.project_hint}</code>.</>}</p>
      <div className="row">
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">Choose a project…</option>
          {projects.data?.items.filter((p) => p.id !== s.project?.id).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button disabled={!target} onClick={() => assign(Number(target))}>Assign</button>
        {s.project && <button onClick={() => assign(null)}>Unassign</button>}
      </div>
      <div className="row">
        <input placeholder={s.title ? `New project, e.g. "${s.title.slice(0, 40)}"` : "New project name"} value={name} onChange={(e) => setName(e.target.value)} />
        <button disabled={!name.trim()} onClick={createAndAssign}>Create project &amp; assign</button>
      </div>
      <p className="muted small">When you later create a repository for it, a Claude Code session there joins this project automatically if the folder has the same name (or add the folder as an alias on the project page).</p>
      {suggestion && (
        <div className="notice small">
          AI suggestion: <strong>{suggestion.data.decision === "new" ? `new project "${String(suggestion.data.new_project_name)}"` : String(suggestion.data.project_name)}</strong>
          {" "}({Math.round(Number(suggestion.data.confidence) * 100)}%) — {suggestion.body}
          <div className="row">
            <button onClick={() => act(`/derived/${suggestion.id}/apply`)}>{suggestion.data.decision === "new" ? "Create project & assign" : "Assign"}</button>
            <button onClick={() => act(`/derived/${suggestion.id}/dismiss`)}>Dismiss</button>
          </div>
        </div>
      )}
      {msg && <p className="small bad">{msg}</p>}
    </section>
  );
}

function Message({ e }: { e: VEvent }) {
  return (
    <div className={`msg msg-${e.actor}`}>
      <div className="msg-head"><span className="actor">{e.actor}</span><EventLinkMeta e={e} /></div>
      <div className="msg-body prewrap">{e.content}</div>
    </div>
  );
}

export default function SessionDetail() {
  const { id } = useParams();
  const [sp, setSp] = useSearchParams();
  const offset = Number(sp.get("offset") ?? 0);
  const [showSystem, setShowSystem] = useState(true);
  const sess = useFetch<Session>(`/sessions/${id}`);
  const evs = useFetch<Page<VEvent>>(`/sessions/${id}/events?limit=200&offset=${offset}`);
  const [ai, setAi] = useState("");

  const { cards, ids } = useMemo(() => {
    const items = evs.data?.items ?? [];
    const calls = new Set(items.filter((e) => e.event_type === "tool_call").map((e) => e.tool_call_id));
    const results = new Map(items.filter((e) => e.event_type === "tool_result" && e.tool_call_id).map((e) => [e.tool_call_id!, e]));
    return { cards: items.filter((e) => !(e.event_type === "tool_result" && calls.has(e.tool_call_id))), ids: results };
  }, [evs.data]);

  if (sess.error) return <ErrorBox error={sess.error} />;
  if (!sess.data) return <Loading />;
  const s = sess.data;
  async function enqueueSummary() {
    try { await api("/jobs", { method: "POST", body: { kind: "summarize_session", session_id: s.id } }); setAi("Summarisation queued (see Processing)."); }
    catch (e) { setAi(e instanceof Error ? e.message : String(e)); }
  }
  async function del() {
    if (!confirm("Permanently delete this session's stored events? A tombstone keeps collectors from re-uploading it.")) return;
    await api(`/sessions/${s.id}`, { method: "DELETE" }); window.location.href = "/sessions";
  }
  return (
    <>
      <p className="crumbs"><Link to="/sessions">Sessions</Link> / {s.project && <><Link to={`/projects/${s.project.id}`}>{s.project.name}</Link> /</>}</p>
      <h1>{s.title || s.external_session_id}</h1>
      <p className="muted">{s.source} / {s.device.label || s.device.key} · {fmtTime(s.started_at)} → {fmtTime(s.last_event_at)} · {s.event_count} events · {s.status}{s.project ? "" : ` · no project${s.project_hint ? ` (${s.project_hint})` : ""}`}</p>
      <p className="row"><button onClick={enqueueSummary}>Queue AI summary</button><button className="danger" onClick={del}>Delete session data</button>{ai && <span className="small">{ai}</span>}</p>

      <ProjectPicker s={s} onChanged={sess.reload} />

      {(s.derived?.length ?? 0) > 0 && (<><h2>Interpretations of this session</h2>{s.derived!.map((d) => <DerivedCard key={d.id} d={d} onChanged={sess.reload} />)}</>)}

      <Evidence label="Source evidence — timeline (original records, chronological)">
        <label className="check"><input type="checkbox" checked={showSystem} onChange={(e) => setShowSystem(e.target.checked)} /> Show system / session events</label>
        {evs.error && <ErrorBox error={evs.error} />}
        {!evs.data ? <Loading /> : (
          <div className="timeline">
            {cards.filter((e) => showSystem || !["system", "session_start", "session_end"].includes(e.event_type)).map((e) => {
              if (e.event_type === "tool_call") return <ToolCard key={e.id} call={e} result={ids.get(e.tool_call_id!)} />;
              if (e.event_type === "tool_result") return <ToolCard key={e.id} call={{ ...e, content: `result of ${e.tool_call_id}`, file_changes: [] }} />;
              if (e.event_type === "message") return <Message key={e.id} e={e} />;
              return (
                <div key={e.id} className="msg msg-system"><div className="msg-head"><span className="actor">{e.event_type}</span><EventLinkMeta e={e} /></div>
                  <div className="msg-body prewrap">{e.content}</div>{e.file_changes.map((f, i) => <Diff key={i} f={f} />)}</div>
              );
            })}
          </div>
        )}
        {evs.data && <Pager page={evs.data} onChange={(o) => { const n = new URLSearchParams(sp); n.set("offset", String(o)); setSp(n); }} />}
      </Evidence>
    </>
  );
}
