import { Badge, ErrorBox, Loading } from "../components";
import { api } from "../api";
import { useFetch } from "../hooks";
import type { Device, Page, ProviderStatus } from "../types";
import { ago, fmtTime } from "../util";

interface AuditRow { id: number; ts: string; actor: string; action: string; target: string | null; detail: Record<string, unknown> }

export default function Sources() {
  const devices = useFetch<{ items: Device[] }>("/devices");
  const providers = useFetch<{ ai_enabled: boolean; allow_external_ai: boolean; providers: ProviderStatus[] }>("/providers");
  const audit = useFetch<Page<AuditRow>>("/audit?limit=15");
  async function toggle(d: Device) {
    await api(`/devices/${d.id}`, { method: "PATCH", body: { collection_enabled: !d.collection_enabled } });
    await devices.reload();
  }
  return (
    <>
      <h1>Sources &amp; AI</h1>
      <h2>Source devices</h2>
      <p className="muted small">Each collector identifies itself with a source kind and device ID. Turning a device off makes the server drop its new events (retries of already-stored events stay idempotent). Collectors also have their own local disable switch.</p>
      {devices.error && <ErrorBox error={devices.error} />}
      {!devices.data ? <Loading /> : (
        <div className="tablewrap"><table>
          <thead><tr><th>Source</th><th>Device</th><th>Events</th><th>Last seen</th><th>Collection</th></tr></thead>
          <tbody>{devices.data.items.map((d) => (
            <tr key={d.id}><td>{d.source}</td><td>{d.label || d.device_key}<div className="muted small">{d.device_key}</div></td><td>{d.event_count}</td><td>{ago(d.last_seen_at)}</td>
              <td><label className="check"><input type="checkbox" checked={d.collection_enabled} onChange={() => toggle(d)} /> {d.collection_enabled ? "on" : "off"}</label></td></tr>
          ))}{devices.data.items.length === 0 && <tr><td colSpan={5} className="empty">No devices have reported yet.</td></tr>}</tbody>
        </table></div>
      )}
      <h2>AI providers</h2>
      {providers.data && (
        <>
          <p>{providers.data.ai_enabled ? "AI processing is configured." : "No AI provider is configured — AI processing is disabled. Everything else works."} External (cloud) providers {providers.data.allow_external_ai ? <b>are permitted</b> : "are blocked"} (<code>VIBEVAULT_ALLOW_EXTERNAL_AI</code>).</p>
          {providers.data.providers.length > 0 && (
            <div className="tablewrap"><table>
              <thead><tr><th>Provider</th><th>State</th><th>Model</th><th>Today</th><th>Notes</th></tr></thead>
              <tbody>{providers.data.providers.map((p) => (
                <tr key={p.name}><td>{p.name} {p.local ? <Badge>local</Badge> : <Badge tone="warn">external</Badge>}</td>
                  <td><Badge tone={p.state === "ready" ? "good" : "warn"}>{p.state}</Badge></td><td>{p.model ?? "—"}</td>
                  <td>{p.requests_today}{p.daily_request_budget != null ? ` / ${p.daily_request_budget}` : ""}</td><td className="small">{p.detail ?? (p.retry_after_seconds ? `retry in ${Math.ceil(p.retry_after_seconds)}s` : "")}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
      <h2>Audit log</h2>
      <p className="muted small">Operations only (counts and identifiers); never event content or tokens.</p>
      {audit.data && (
        <div className="tablewrap"><table>
          <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr></thead>
          <tbody>{audit.data.items.map((a) => <tr key={a.id}><td>{fmtTime(a.ts)}</td><td>{a.actor}</td><td>{a.action}{a.target ? ` #${a.target}` : ""}</td><td className="small"><code>{JSON.stringify(a.detail)}</code></td></tr>)}</tbody>
        </table></div>
      )}
    </>
  );
}
