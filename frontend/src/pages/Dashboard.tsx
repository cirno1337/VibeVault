import { Link } from "react-router-dom";
import { Empty, ErrorBox, Loading } from "../components";
import { useFetch } from "../hooks";
import type { Page, Session, Stats } from "../types";
import { ago, fmtTime } from "../util";

function Bars({ data, link }: { data: Record<string, number>; link?: (k: string) => string }) {
  const max = Math.max(1, ...Object.values(data));
  const rows = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (!rows.length) return <Empty>No data yet.</Empty>;
  return (
    <div className="bars">
      {rows.map(([k, v]) => (
        <div key={k} className="bar-row">
          <span className="bar-label">{link ? <Link to={link(k)}>{k}</Link> : k}</span>
          <span className="bar-track"><span className="bar-fill" style={{ width: `${(v / max) * 100}%` }} /></span>
          <span className="bar-val">{v}</span>
        </div>
      ))}
    </div>
  );
}

export default function Dashboard() {
  const stats = useFetch<Stats>("/stats", 10000);
  const recent = useFetch<Page<Session>>("/sessions?limit=8", 10000);
  if (stats.error) return <ErrorBox error={stats.error} />;
  if (!stats.data) return <Loading />;
  const s = stats.data;
  const jobs = s.processing.jobs;
  return (
    <>
      <h1>Dashboard</h1>
      {s.totals.events === 0 && (
        <div className="notice">
          The archive is empty. Point a collector at this server, or load the clearly-labelled synthetic dataset for development:
          <pre>VIBEVAULT_TOKEN=&lt;token&gt; vibevault seed --url {window.location.origin}</pre>
        </div>
      )}
      <div className="tiles">
        <Link className="tile" to="/search"><b>{s.totals.events.toLocaleString()}</b><span>events</span></Link>
        <Link className="tile" to="/sessions"><b>{s.totals.sessions.toLocaleString()}</b><span>sessions</span></Link>
        <Link className="tile" to="/projects"><b>{s.totals.projects}</b><span>projects</span></Link>
        <Link className="tile" to="/sources"><b>{s.totals.devices}</b><span>source devices</span></Link>
      </div>
      <div className="grid2">
        <section className="card">
          <h2>Processing backlog</h2>
          <p><b>{s.processing.events_pending}</b> events awaiting deterministic processing · <b>{s.processing.events_processed}</b> processed</p>
          <p>
            <Link to="/jobs?status=pending">{jobs.pending ?? 0} pending</Link> · <Link to="/jobs?status=running">{jobs.running ?? 0} running</Link> ·{" "}
            <Link to="/jobs?status=completed">{jobs.completed ?? 0} done</Link> ·{" "}
            <Link to="/jobs?status=failed" className={jobs.failed ? "bad" : ""}>{jobs.failed ?? 0} failed</Link>
          </p>
          <p className="muted small">AI processing: {s.ai.enabled ? "a provider list is configured (see Sources & AI)" : "disabled — archive, search and browsing work without it"}.</p>
        </section>
        <section className="card">
          <h2>Ingestion</h2>
          <p>Last event received {ago(s.last_ingested_at)} · {s.events_last_24h} in 24 h · {s.events_last_7d} in 7 d</p>
          <p className="small">Redaction: {s.redaction.events_redacted} events had secrets redacted · {s.redaction.events_unscanned > 0 ? <span className="bad">{s.redaction.events_unscanned} unscanned</span> : "0 unscanned"}</p>
          {s.totals.unresolved_sessions > 0 && <p><Link to="/sessions?unresolved=1">{s.totals.unresolved_sessions} sessions have no project</Link></p>}
        </section>
        <section className="card"><h2>Events by source</h2><Bars data={s.events_by_source} link={(k) => `/sessions?source=${k}`} /></section>
        <section className="card"><h2>Events by type</h2><Bars data={s.events_by_type} /></section>
      </div>
      <h2>Recent sessions</h2>
      {recent.data && recent.data.items.length === 0 ? <Empty>No sessions yet.</Empty> : (
        <ul className="list">
          {recent.data?.items.map((x) => (
            <li key={x.id}>
              <Link to={`/sessions/${x.id}`}>{x.title || x.preview || x.external_session_id}</Link>
              <span className="muted small"> · {x.project?.name ?? "no project"} · {x.source} / {x.device.label || x.device.key} · {x.event_count} events · {fmtTime(x.last_event_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
