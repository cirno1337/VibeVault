/**
 * Offline queue + delivery. Batches live in IndexedDB until the server accepts them; network/auth
 * failures retry with exponential backoff, and nothing is dropped except on explicit server rejection.
 */
import type { DeliveryState, QueuedBatch } from "./adapters/types";
import type { EventIn } from "./normalizer";
import type { KV } from "./store";

export const MAX_BATCH = 100; // server accepts 500; smaller batches keep one bad request cheap
const BASE_DELAY_MS = 30_000;
const MAX_DELAY_MS = 30 * 60_000;

export interface ServerConfig {
  url: string; // e.g. https://my-server.example-tailnet.ts.net:8787
  token: string;
}

export function backoffMs(attempts: number): number {
  return Math.min(BASE_DELAY_MS * 2 ** Math.max(0, attempts - 1), MAX_DELAY_MS);
}

let seq = 0;
const batchId = (now: number) => `${now.toString().padStart(15, "0")}-${(seq++ % 1e6).toString().padStart(6, "0")}`;

export class Queue {
  constructor(
    readonly store: KV<QueuedBatch>,
    private readonly now: () => number = Date.now,
  ) {}

  async enqueue(events: EventIn[]): Promise<void> {
    for (let i = 0; i < events.length; i += MAX_BATCH) {
      const id = batchId(this.now());
      await this.store.put(id, { id, events: events.slice(i, i + MAX_BATCH), attempts: 0, nextAttemptAt: 0 });
    }
  }

  async queuedEvents(): Promise<number> {
    return (await this.store.entries()).reduce((n, [, b]) => n + b.events.length, 0);
  }

  /** Drop queued events matching `pred` (used when a conversation gets excluded). */
  async purge(pred: (e: EventIn) => boolean): Promise<number> {
    let removed = 0;
    for (const [id, b] of await this.store.entries()) {
      const keep = (b.events as EventIn[]).filter((e) => !pred(e));
      removed += b.events.length - keep.length;
      if (!keep.length) await this.store.delete(id);
      else if (keep.length !== b.events.length) await this.store.put(id, { ...b, events: keep });
    }
    return removed;
  }

  /**
   * Sends every due batch in order; stops at the first transport/auth failure (no point hammering a
   * server that is down). Returns the new delivery state.
   */
  async flush(cfg: ServerConfig, state: DeliveryState, fetchFn: typeof fetch = fetch): Promise<DeliveryState> {
    const next: DeliveryState = { ...state };
    const entries = await this.store.entries();
    for (const [i, [id, batch]] of entries.entries()) {
      if (batch.nextAttemptAt > this.now()) continue;
      const outcome = await deliver(batch.events, cfg, fetchFn);
      if (outcome.kind === "ok") {
        await this.store.delete(id);
        next.lastSuccessAt = this.now();
        next.lastError = outcome.rejected ? "rejected" : undefined;
      } else if (outcome.kind === "split") {
        await this.store.delete(id);
        await this.enqueueSplit(batch);
      } else if (outcome.kind === "drop") {
        await this.store.delete(id);
        next.lastError = "rejected";
      } else {
        const attempts = batch.attempts + 1;
        const retryAt = this.now() + backoffMs(attempts);
        await this.store.put(id, { ...batch, attempts, nextAttemptAt: retryAt });
        // The server is unreachable or refusing us: hold the rest of the queue back too, instead of
        // probing it once per batch.
        for (const [laterId, later] of entries.slice(i + 1)) {
          if (later.nextAttemptAt < retryAt) await this.store.put(laterId, { ...later, nextAttemptAt: retryAt });
        }
        next.lastError = outcome.error;
        break;
      }
    }
    next.queued = await this.queuedEvents();
    return next;
  }

  private async enqueueSplit(batch: QueuedBatch): Promise<void> {
    const half = Math.ceil(batch.events.length / 2);
    for (const part of [batch.events.slice(0, half), batch.events.slice(half)]) {
      const id = batchId(this.now());
      await this.store.put(id, { id, events: part, attempts: batch.attempts, nextAttemptAt: 0 });
    }
  }

  /** Earliest time any batch is due, for scheduling the retry alarm. */
  async nextDueAt(): Promise<number | undefined> {
    const times = (await this.store.entries()).map(([, b]) => b.nextAttemptAt);
    return times.length ? Math.min(...times) : undefined;
  }
}

export type Outcome =
  | { kind: "ok"; rejected: number }
  | { kind: "retry"; error: "network" | "auth" }
  | { kind: "split" } // 413 with more than one event: retry as two halves
  | { kind: "drop" }; // the server rejected the whole envelope; retrying cannot help

export async function deliver(events: unknown[], cfg: ServerConfig, fetchFn: typeof fetch): Promise<Outcome> {
  let res: Response;
  try {
    res = await fetchFn(new URL("/api/v1/events/batch", cfg.url).toString(), {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${cfg.token}` },
      body: JSON.stringify({ events }),
    });
  } catch {
    return { kind: "retry", error: "network" };
  }
  if (res.ok) {
    const body = (await res.json().catch(() => null)) as { rejected?: number } | null;
    return { kind: "ok", rejected: body?.rejected ?? 0 };
  }
  if (res.status === 401 || res.status === 403) return { kind: "retry", error: "auth" };
  if (res.status === 413) return events.length > 1 ? { kind: "split" } : { kind: "drop" };
  if (res.status === 400 || res.status === 422) return { kind: "drop" };
  return { kind: "retry", error: "network" }; // 5xx, 429, proxies, captive portals...
}
