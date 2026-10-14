import { ReactNode, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import type { Derived, FileChange, Page, VEvent } from "./types";
import { fmtTime, shortId, splitSnippet } from "./util";

export function Loading({ what = "Loading" }: { what?: string }) { return <p className="muted" role="status">{what}…</p>; }
export function ErrorBox({ error }: { error: string }) { return <div className="error" role="alert">{error}</div>; }
export function Empty({ children }: { children: ReactNode }) { return <p className="empty">{children}</p>; }

export function Badge({ children, tone = "neutral", title }: { children: ReactNode; tone?: string; title?: string }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export function Pager({ page, onChange }: { page: Page<unknown>; onChange: (offset: number) => void }) {
  const from = page.total === 0 ? 0 : page.offset + 1;
  const to = Math.min(page.offset + page.limit, page.total);
  return (
    <div className="pager">
      <button disabled={page.offset <= 0} onClick={() => onChange(Math.max(0, page.offset - page.limit))}>← Prev</button>
      <span className="muted">{from}–{to} of {page.total}</span>
      <button disabled={to >= page.total} onClick={() => onChange(page.offset + page.limit)}>Next →</button>
    </div>
  );
}

export function Highlight({ snippet }: { snippet: string }) {
  return <>{splitSnippet(snippet).map((p, i) => (p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}</>;
}

/** Wrapper that marks everything inside as ORIGINAL source data. */
export function Evidence({ label = "Source evidence", children, className = "" }: { label?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`evidence ${className}`}>
      <header><span className="tag tag-evidence">● {label}</span></header>
      {children}
    </section>
  );
}

const EPI: Record<string, string> = { confirmed: "good", inferred: "warn", hypothesis: "warn", unresolved: "bad" };

export function DerivedCard({ d, onChanged }: { d: Derived; onChanged?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const computed = d.origin === "deterministic";
  async function confirm() {
    setBusy(true); setErr(null);
    try { await api(`/derived/${d.id}/confirm`, { method: "POST" }); onChanged?.(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  return (
    <section className={`derived ${computed ? "derived-computed" : "derived-ai"}`}>
      <header>
        <span className="tag tag-derived">{computed ? "◆ Computed from evidence" : "◆ AI interpretation — not source evidence"}</span>
        <Badge tone="neutral">{d.kind}</Badge>
        <Badge tone={EPI[d.epistemic_status] ?? "neutral"} title="Epistemic status">{d.epistemic_status}</Badge>
        {d.state !== "active" && <Badge tone="neutral">{d.state}</Badge>}
      </header>
      <h4>{d.title}</h4>
      {d.body && <p className="prewrap">{d.body}</p>}
      {computed && d.kind === "digest" && <DigestData data={d.data} />}
      <footer className="muted small">
        {d.origin === "model" ? `${d.provider ?? "?"} / ${d.model ?? "?"} · prompt ${d.prompt_version ?? "?"}${d.confidence != null ? ` · model confidence ${(d.confidence * 100).toFixed(0)}% (self-reported)` : ""}` : d.origin}
        {" · "}{fmtTime(d.created_at)}
        {d.confirmed_at && <> · confirmed by {d.confirmed_by} {fmtTime(d.confirmed_at)}</>}
      </footer>
      {d.evidence.length > 0 && (
        <p className="small">Evidence:{" "}
          {d.evidence.slice(0, 12).map((e) => <Link key={e.event_id} className="chip" to={`/events/${e.event_id}`} title={e.note ?? ""}>{shortId(e.event_id)}</Link>)}
          {d.evidence.length > 12 && <span className="muted"> +{d.evidence.length - 12} more</span>}
        </p>
      )}
      {!computed && d.epistemic_status !== "confirmed" && d.state === "active" && (
        <p><button onClick={confirm} disabled={busy}>Mark as confirmed (I have verified this)</button> {err && <span className="error-inline">{err}</span>}</p>
      )}
    </section>
  );
}

function DigestData({ data }: { data: Record<string, unknown> }) {
  const byType = (data.events_by_type ?? {}) as Record<string, number>;
  const tools = (data.tools ?? {}) as Record<string, number>;
  const files = (data.files ?? []) as { path: string; operation: string }[];
  return (
    <div className="small">
      <div>{Object.entries(byType).map(([k, v]) => <Badge key={k}>{k} {v}</Badge>)}</div>
      {Object.keys(tools).length > 0 && <div>Tools: {Object.entries(tools).map(([k, v]) => <Badge key={k}>{k || "?"} ×{v}</Badge>)}</div>}
      {files.length > 0 && <details><summary>{files.length} file operations</summary><ul>{files.map((f, i) => <li key={i}><code>{f.operation}</code> {f.path}</li>)}</ul></details>}
    </div>
  );
}

export function RedactionBadge({ e }: { e: VEvent }) {
  if (e.redaction.status === "redacted") return <Badge tone="warn" title={JSON.stringify(e.redaction.rules)}>redacted ×{e.redaction.count}</Badge>;
  if (e.redaction.status === "unscanned") return <Badge tone="bad" title="This event was not scanned for secrets">unscanned</Badge>;
  return null;
}

export function Diff({ f }: { f: FileChange }) {
  return (
    <details className="filechange">
      <summary><code>{f.operation}</code> {f.path}{f.old_path ? ` (from ${f.old_path})` : ""}</summary>
      {f.diff ? <pre className="diff">{f.diff.split("\n").map((ln, i) => <div key={i} className={ln.startsWith("+") ? "add" : ln.startsWith("-") ? "del" : ""}>{ln || " "}</div>)}</pre> : <p className="muted small">No diff captured.</p>}
    </details>
  );
}

export function EventLinkMeta({ e }: { e: VEvent }) {
  return (
    <span className="meta">
      {fmtTime(e.occurred_at)} · <Link to={`/events/${e.id}`}>details</Link>
      {e.revision > 1 && <> · <Badge tone="warn">revision {e.revision}</Badge></>} <RedactionBadge e={e} />
    </span>
  );
}
