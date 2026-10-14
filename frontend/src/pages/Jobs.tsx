import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Empty, ErrorBox, Loading, Pager } from "../components";
import { useFetch } from "../hooks";
import type { Job, Page } from "../types";
import { fmtTime } from "../util";

const TONE: Record<string, string> = { completed: "good", failed: "bad", running: "warn", pending: "neutral" };

export default function Jobs() {
  const [sp, setSp] = useSearchParams();
  const status = sp.get("status") ?? "";
  const offset = Number(sp.get("offset") ?? 0);
  const { data, error, reload } = useFetch<Page<Job> & { counts: Record<string, number> }>(`/jobs?limit=25&offset=${offset}${status ? `&status=${status}` : ""}`, 5000);
  const [msg, setMsg] = useState("");
  async function retry(id: number) {
    try { await api(`/jobs/${id}/retry`, { method: "POST" }); setMsg(`Job ${id} re-queued.`); await reload(); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }
  const setStatus = (s: string) => { const n = new URLSearchParams(); if (s) n.set("status", s); setSp(n); };
  return (
    <>
      <h1>Processing queue</h1>
      <p className="muted small">Deterministic jobs run always; AI jobs only when a provider is configured and permitted. Failed jobs stay here until retried. Auto-refreshes every 5 s.</p>
      {error && <ErrorBox error={error} />}
      {!data ? <Loading /> : (
        <>
          <div className="tabs">
            {["", "pending", "running", "completed", "failed"].map((s) => (
              <button key={s} className={status === s ? "active" : ""} onClick={() => setStatus(s)}>{s || "all"}{s ? ` (${data.counts[s] ?? 0})` : ""}</button>
            ))}
          </div>
          {msg && <p className="small">{msg}</p>}
          {data.items.length === 0 ? <Empty>No jobs.</Empty> : (
            <div className="tablewrap"><table>
              <thead><tr><th>#</th><th>Kind</th><th>Status</th><th>Attempts</th><th>Next run / finished</th><th>Details</th></tr></thead>
              <tbody>{data.items.map((j) => (
                <tr key={j.id}>
                  <td>{j.id}</td><td>{j.kind}<div className="muted small">{JSON.stringify(j.payload)}</div></td>
                  <td><Badge tone={TONE[j.status]}>{j.status}</Badge></td>
                  <td>{j.attempts}/{j.max_attempts}</td>
                  <td>{j.status === "pending" ? fmtTime(j.run_after) : fmtTime(j.finished_at)}</td>
                  <td>
                    {j.status_note && <div className="small">{j.status_note}</div>}
                    {Object.keys(j.checkpoint).length > 0 && j.status !== "completed" && <div className="muted small">checkpoint {JSON.stringify(j.checkpoint)}</div>}
                    {j.result && <div className="muted small">{JSON.stringify(j.result)}</div>}
                    {j.last_error && <details><summary className="bad small">error · {fmtTime(j.last_error_at)}</summary><pre className="out out-bad">{j.last_error}</pre></details>}
                    {j.status === "failed" && <button onClick={() => retry(j.id)}>Retry</button>}
                  </td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
          <Pager page={data} onChange={(o) => { const n = new URLSearchParams(sp); n.set("offset", String(o)); setSp(n); }} />
        </>
      )}
    </>
  );
}
