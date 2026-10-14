import { Link, useParams } from "react-router-dom";
import { DerivedCard, Diff, ErrorBox, Evidence, Loading, RedactionBadge } from "../components";
import { useFetch } from "../hooks";
import type { Derived, VEvent } from "../types";
import { fmtTime } from "../util";

interface Detail {
  event: VEvent; parent: VEvent | null; children: VEvent[];
  revisions: { id: string; revision: number; is_current: boolean; ingested_at: string }[]; derived: Derived[];
}

function Row({ k, children }: { k: string; children: React.ReactNode }) { return <><dt>{k}</dt><dd>{children}</dd></>; }

export default function EventDetail() {
  const { id } = useParams();
  const { data, error, reload } = useFetch<Detail>(`/events/${id}`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading />;
  const { event: e } = data;
  return (
    <>
      <p className="crumbs">{e.session_id ? <Link to={`/sessions/${e.session_id}`}>← Session</Link> : "Event without a session"}</p>
      <h1>{e.event_type} <span className="muted">by {e.actor}</span></h1>

      <Evidence label="Source evidence — exactly as stored (after redaction)">
        <pre className="out prewrap">{e.content || "(empty)"}</pre>
        {e.tool && (
          <div className="toolbody">
            <h3>Tool execution</h3>
            <dl>
              <Row k="Tool">{e.tool.name ?? "—"}</Row>
              {e.tool.command && <Row k="Command"><code>{e.tool.command}</code></Row>}
              <Row k="Exit code">{e.tool.exit_code ?? "—"} {e.tool.is_error ? "(error)" : ""}</Row>
            </dl>
            {e.tool.input && <details><summary>Input</summary><pre className="out">{JSON.stringify(e.tool.input, null, 2)}</pre></details>}
            {e.tool.output != null && <details open><summary>Output</summary><pre className="out">{e.tool.output}</pre></details>}
          </div>
        )}
        {e.file_changes.map((f, i) => <Diff key={i} f={f} />)}
        <h3>Provenance</h3>
        <dl>
          <Row k="Event ID"><code>{e.id}</code></Row>
          <Row k="Source">{e.source} · device <code>{e.device.key}</code>{e.device.label ? ` (${e.device.label})` : ""}</Row>
          <Row k="External IDs">session <code>{e.external_session_id ?? "—"}</code> · event <code>{e.external_event_id}</code> · revision {e.revision}{e.is_current ? "" : " (superseded)"}</Row>
          <Row k="Occurred">{fmtTime(e.occurred_at)} · sequence {e.sequence ?? "—"}</Row>
          <Row k="Ingested">{fmtTime(e.ingested_at)} by <code>{e.ingested_by}</code> · order #{e.ingest_seq}</Row>
          <Row k="Redaction"><RedactionBadge e={e} /> {e.redaction.status === "none" && "no secrets detected"} {Object.entries(e.redaction.rules).map(([k, v]) => <code key={k} className="chip">{k}×{v}</code>)}</Row>
          <Row k="Processing">{e.processing_status}</Row>
          {e.raw_payload_ref && <Row k="Original reference"><code>{e.raw_payload_ref}</code></Row>}
          {e.project_id && <Row k="Project"><Link to={`/projects/${e.project_id}`}>#{e.project_id}</Link></Row>}
          {e.project_hint && <Row k="Project identity (as reported)"><code>{e.project_hint}</code></Row>}
        </dl>
        {Object.keys(e.metadata).length > 0 && <details><summary>Metadata</summary><pre className="out">{JSON.stringify(e.metadata, null, 2)}</pre></details>}
        {e.raw_payload != null && <details><summary>Original payload (as sent by the collector, secrets redacted)</summary><pre className="out">{JSON.stringify(e.raw_payload, null, 2)}</pre></details>}
        {data.revisions.length > 1 && (
          <p className="small">Revisions: {data.revisions.map((r) => <Link key={r.id} className="chip" to={`/events/${r.id}`}>r{r.revision}{r.is_current ? " (current)" : ""}</Link>)}</p>
        )}
        {data.parent && <p className="small">Parent: <Link to={`/events/${data.parent.id}`}>{data.parent.event_type} — {data.parent.content.slice(0, 80)}</Link></p>}
        {data.children.length > 0 && <p className="small">Children: {data.children.map((c) => <Link key={c.id} className="chip" to={`/events/${c.id}`}>{c.event_type}</Link>)}</p>}
      </Evidence>

      <h2>Interpretations citing this event</h2>
      {data.derived.length === 0 ? <p className="muted">None. Derived knowledge is generated separately and never alters this record.</p> : data.derived.map((d) => <DerivedCard key={d.id} d={d} onChanged={reload} />)}
    </>
  );
}
