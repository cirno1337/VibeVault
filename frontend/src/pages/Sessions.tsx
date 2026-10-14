import { Link, useSearchParams } from "react-router-dom";
import { Badge, Empty, ErrorBox, Loading, Pager } from "../components";
import { useFetch } from "../hooks";
import type { Page, Project, Session, Stats } from "../types";
import { fmtTime, qs, toIso } from "../util";

export default function Sessions() {
  const [sp, setSp] = useSearchParams();
  const get = (k: string) => sp.get(k) ?? "";
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); v ? n.set(k, v) : n.delete(k); n.delete("offset"); setSp(n); };
  const path = `/sessions${qs({ project_id: get("project_id"), source: get("source"), unresolved: get("unresolved") === "1", since: toIso(get("since")), until: toIso(get("until")), offset: get("offset"), limit: 30 })}`;
  const { data, error, loading } = useFetch<Page<Session>>(path);
  const projects = useFetch<Page<Project>>("/projects?limit=200");
  const stats = useFetch<Stats>("/stats");
  return (
    <>
      <h1>Sessions</h1>
      <div className="filters">
        <label>Project<select value={get("project_id")} onChange={(e) => set("project_id", e.target.value)}><option value="">All</option>{projects.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Source<select value={get("source")} onChange={(e) => set("source", e.target.value)}><option value="">All</option>{Object.keys(stats.data?.events_by_source ?? {}).map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>From<input type="date" value={get("since")} onChange={(e) => set("since", e.target.value)} /></label>
        <label>To<input type="date" value={get("until")} onChange={(e) => set("until", e.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={get("unresolved") === "1"} onChange={(e) => set("unresolved", e.target.checked ? "1" : "")} /> No project</label>
      </div>
      {error && <ErrorBox error={error} />}
      {loading && !data ? <Loading /> : data && (data.items.length === 0 ? <Empty>No sessions match.</Empty> : (
        <>
          <ul className="list sessions">{data.items.map((s) => (
            <li key={s.id}>
              <Link to={`/sessions/${s.id}`} className="title">{s.title || s.preview || s.external_session_id}</Link>
              <div className="muted small">
                {s.project ? <Link to={`/projects/${s.project.id}`}>{s.project.name}</Link> : <span title={s.project_hint ?? ""}>no project{s.project_hint ? ` (${s.project_hint})` : ""}</span>}
                {" · "}{s.source} / {s.device.label || s.device.key} · {s.event_count} events · {fmtTime(s.started_at)} → {fmtTime(s.last_event_at)} {s.status === "ended" ? <Badge tone="good">ended</Badge> : <Badge>open</Badge>}
              </div>
            </li>
          ))}</ul>
          <Pager page={data} onChange={(o) => { const n = new URLSearchParams(sp); n.set("offset", String(o)); setSp(n); }} />
        </>
      ))}
    </>
  );
}
