import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Badge, Empty, ErrorBox, Highlight, Loading, Pager } from "../components";
import { useFetch } from "../hooks";
import type { Page, Project, SearchHit, Stats } from "../types";
import { fmtTime, qs, toIso } from "../util";

const TYPES = ["message", "tool_call", "tool_result", "file_change", "error", "reasoning", "system", "session_start", "session_end", "other"];

export default function SearchPage() {
  const [sp, setSp] = useSearchParams();
  const g = (k: string) => sp.get(k) ?? "";
  const [q, setQ] = useState(g("q"));
  const path = g("q") ? `/search${qs({ q: g("q"), project_id: g("project_id"), source: g("source"), event_type: g("event_type"), actor: g("actor"), since: toIso(g("since")), until: toIso(g("until")), offset: g("offset"), limit: 20 })}` : null;
  const { data, error, loading } = useFetch<Page<SearchHit> & { fts_match: string }>(path);
  const projects = useFetch<Page<Project>>("/projects?limit=200");
  const stats = useFetch<Stats>("/stats");
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); v ? n.set(k, v) : n.delete(k); n.delete("offset"); setSp(n); };
  function submit(e: FormEvent) { e.preventDefault(); set("q", q.trim()); }
  return (
    <>
      <h1>Search</h1>
      <form onSubmit={submit} className="row searchbar">
        <input type="search" placeholder='e.g. alembic "no such column"' value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search query" autoFocus />
        <button type="submit">Search</button>
      </form>
      <p className="muted small">Terms are AND-ed; the last term matches as a prefix; use quotes for phrases. Searches original (redacted) event text, not AI summaries.</p>
      <div className="filters">
        <label>Project<select value={g("project_id")} onChange={(e) => set("project_id", e.target.value)}><option value="">All</option>{projects.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Source<select value={g("source")} onChange={(e) => set("source", e.target.value)}><option value="">All</option>{Object.keys(stats.data?.events_by_source ?? {}).map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>Type<select value={g("event_type")} onChange={(e) => set("event_type", e.target.value)}><option value="">All</option>{TYPES.map((t) => <option key={t}>{t}</option>)}</select></label>
        <label>Actor<select value={g("actor")} onChange={(e) => set("actor", e.target.value)}><option value="">All</option>{["user", "assistant", "tool", "system"].map((t) => <option key={t}>{t}</option>)}</select></label>
        <label>From<input type="date" value={g("since")} onChange={(e) => set("since", e.target.value)} /></label>
        <label>To<input type="date" value={g("until")} onChange={(e) => set("until", e.target.value)} /></label>
      </div>
      {error && <ErrorBox error={error} />}
      {path === null ? null : loading && !data ? <Loading what="Searching" /> : data && (data.items.length === 0 ? <Empty>No matches.</Empty> : (
        <>
          <p className="muted small">{data.total} matches</p>
          <ul className="results">{data.items.map((h) => (
            <li key={h.event_id}>
              <div className="small"><Badge>{h.event_type}</Badge> <Badge>{h.actor}</Badge> <span className="muted">{h.source} · {fmtTime(h.occurred_at)}{h.project_name ? ` · ${h.project_name}` : ""}</span></div>
              <div className="snippet"><Highlight snippet={h.snippet} /></div>
              <div className="small"><Link to={`/events/${h.event_id}`}>event</Link>{h.session_id && <> · <Link to={`/sessions/${h.session_id}`}>{h.session_title || `session ${h.session_id}`}</Link></>}</div>
            </li>
          ))}</ul>
          <Pager page={data} onChange={(o) => { const n = new URLSearchParams(sp); n.set("offset", String(o)); setSp(n); }} />
        </>
      ))}
    </>
  );
}
