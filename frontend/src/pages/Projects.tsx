import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { DerivedCard, Empty, ErrorBox, Loading } from "../components";
import { ProjectSuggestions } from "./ProjectSuggestions";
import { useFetch } from "../hooks";
import type { Page, Project, Session } from "../types";
import { ago, fmtTime } from "../util";

export function Projects() {
  const { data, error } = useFetch<Page<Project> & { unresolved_sessions: number }>("/projects?limit=200");
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading />;
  return (
    <>
      <h1>Projects</h1>
      <ProjectSuggestions />
      {data.unresolved_sessions > 0 && (
        <div className="notice"><Link to="/sessions?unresolved=1">{data.unresolved_sessions} sessions</Link> are not assigned to a project. Open a session to assign it to a project (or start a new one from it), or add the source path/URL as an alias on a project.</div>
      )}
      {data.items.length === 0 ? <Empty>No projects yet. Projects are created from collector metadata or manually via the API.</Empty> : (
        <div className="tablewrap"><table>
          <thead><tr><th>Project</th><th>Sessions</th><th>Events</th><th>Last activity</th><th>Collection</th></tr></thead>
          <tbody>{data.items.map((p) => (
            <tr key={p.id}>
              <td><Link to={`/projects/${p.id}`}>{p.name}</Link><div className="muted small">{p.aliases.join(", ")}</div></td>
              <td>{p.session_count}</td><td>{p.event_count}</td><td>{ago(p.last_event_at)}</td>
              <td>{p.collection_enabled ? "on" : <span className="bad">off</span>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </>
  );
}

export function ProjectDetail() {
  const { id } = useParams();
  const proj = useFetch<Project>(`/projects/${id}`);
  const sessions = useFetch<Page<Session>>(`/sessions?project_id=${id}&limit=50`);
  const all = useFetch<Page<Project>>("/projects?limit=200");
  const [alias, setAlias] = useState("");
  const [into, setInto] = useState("");
  const [msg, setMsg] = useState("");
  if (proj.error) return <ErrorBox error={proj.error} />;
  if (!proj.data) return <Loading />;
  const p = proj.data;
  async function patch(body: object) { try { await api(`/projects/${id}`, { method: "PATCH", body }); await proj.reload(); } catch (e) { setMsg(String(e)); } }
  async function addAlias() {
    try {
      const r = await api<{ sessions_assigned: number }>(`/projects/${id}/aliases`, { method: "POST", body: { alias } });
      setMsg(`Alias added; ${r.sessions_assigned} sessions assigned.`); setAlias(""); await proj.reload(); await sessions.reload();
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  async function merge() {
    const target = all.data?.items.find((x) => String(x.id) === into);
    if (!target || !confirm(`Move all sessions, events and aliases of "${p.name}" into "${target.name}" and delete "${p.name}"?`)) return;
    try { await api(`/projects/${id}/merge`, { method: "POST", body: { into: target.id } }); window.location.href = `/projects/${target.id}`; }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  async function remove() {
    if (!confirm(`Delete the empty project "${p.name}" and its aliases?`)) return;
    try { await api(`/projects/${id}`, { method: "DELETE" }); window.location.href = "/projects"; }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  return (
    <>
      <p className="crumbs"><Link to="/projects">Projects</Link> /</p>
      <h1>{p.name}</h1>
      <p className="muted">{p.session_count} sessions · {p.event_count} events · last activity {ago(p.last_event_at)}</p>
      <section className="card">
        <h2>Controls</h2>
        <label className="check"><input type="checkbox" checked={p.collection_enabled} onChange={(e) => patch({ collection_enabled: e.target.checked })} /> Accept new events for this project (off = server drops them)</label>
        <label className="check"><input type="checkbox" checked={p.ai_processing_enabled} onChange={(e) => patch({ ai_processing_enabled: e.target.checked })} /> Allow AI processing of this project's events</label>
        <p className="small">Aliases (source identities mapped here): {p.aliases.length ? p.aliases.map((a) => <code key={a} className="chip">{a}</code>) : "none"}</p>
        <div className="row"><input placeholder="/path/or/url as reported by collectors" value={alias} onChange={(e) => setAlias(e.target.value)} /><button disabled={!alias.trim()} onClick={addAlias}>Add alias</button></div>
        <div className="row">
          <select value={into} onChange={(e) => setInto(e.target.value)}>
            <option value="">Merge into another project…</option>
            {all.data?.items.filter((x) => x.id !== p.id).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
          <button disabled={!into} onClick={merge}>Merge</button>
          {p.session_count === 0 && <button className="danger" onClick={remove}>Delete empty project</button>}
        </div>
        {msg && <p className="small">{msg}</p>}
      </section>
      {(p.derived?.length ?? 0) > 0 && (<><h2>Derived knowledge</h2>{p.derived!.map((d) => <DerivedCard key={d.id} d={d} onChanged={proj.reload} />)}</>)}
      <h2>Sessions</h2>
      {!sessions.data ? <Loading /> : sessions.data.items.length === 0 ? <Empty>No sessions.</Empty> : (
        <ul className="list">{sessions.data.items.map((s) => (
          <li key={s.id}><Link to={`/sessions/${s.id}`}>{s.title || s.preview || s.external_session_id}</Link>
            <span className="muted small"> · {s.source} / {s.device.label || s.device.key} · {s.event_count} events · {fmtTime(s.last_event_at)}</span></li>
        ))}</ul>
      )}
    </>
  );
}
