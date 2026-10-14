import { describe, expect, it } from "vitest";
import type { QueuedBatch } from "../src/adapters/types";
import type { EventIn } from "../src/normalizer";
import { backoffMs, MAX_BATCH, Queue } from "../src/queue";
import { MemoryKV } from "../src/store";

const cfg = { url: "https://vault.example:8787", token: "tok" };
const ev = (id: string, session = "s1"): EventIn =>
  ({ external_event_id: id, external_session_id: session, source: "claude_web" }) as EventIn;

/** A fake VibeVault: idempotent by external_event_id, can be taken offline or made to fail. */
function fakeServer() {
  const stored = new Map<string, number>();
  const s = {
    stored,
    mode: "up" as "up" | "down" | "401" | "422" | "413" | "500",
    requests: 0,
    fetch: (async (url: string, init: RequestInit) => {
      s.requests++;
      expect(url).toBe("https://vault.example:8787/api/v1/events/batch");
      expect((init.headers as Record<string, string>).authorization).toBe("Bearer tok");
      if (s.mode === "down") throw new TypeError("Failed to fetch");
      const events = JSON.parse(init.body as string).events as EventIn[];
      const status = { "401": 401, "422": 422, "413": 413, "500": 500 }[s.mode as string];
      if (status === 413 && events.length === 1) return new Response("", { status: 413 });
      if (status && status !== 413) return new Response("{}", { status });
      if (status === 413 && events.length > 1) return new Response("", { status: 413 });
      let duplicate = 0;
      for (const e of events) {
        if (stored.has(e.external_event_id)) duplicate++;
        stored.set(e.external_event_id, (stored.get(e.external_event_id) ?? 0) + 1);
      }
      return Response.json({ received: events.length, created: events.length - duplicate, duplicate, rejected: 0 });
    }) as unknown as typeof fetch,
  };
  return s;
}

function setup() {
  let now = 1_000_000;
  const store = new MemoryKV<QueuedBatch>();
  const q = new Queue(store, () => now);
  return { q, store, advance: (ms: number) => (now += ms), now: () => now };
}

describe("queue", () => {
  it("splits into batches of MAX_BATCH and delivers in order", async () => {
    const { q } = setup();
    const server = fakeServer();
    await q.enqueue(Array.from({ length: MAX_BATCH + 5 }, (_, i) => ev(`e${i}`)));
    expect(await q.queuedEvents()).toBe(MAX_BATCH + 5);
    const st = await q.flush(cfg, { queued: 0 }, server.fetch);
    expect(server.requests).toBe(2);
    expect(st).toMatchObject({ queued: 0, lastError: undefined });
    expect(st.lastSuccessAt).toBeDefined();
    expect(server.stored.size).toBe(MAX_BATCH + 5);
  });

  it("offline -> online replays without duplicates or loss", async () => {
    const { q, advance } = setup();
    const server = fakeServer();
    server.mode = "down";
    await q.enqueue([ev("a"), ev("b")]);
    await q.enqueue([ev("c")]);
    let st = await q.flush(cfg, { queued: 0 }, server.fetch);
    expect(st).toMatchObject({ lastError: "network", queued: 3 });
    expect(server.requests).toBe(1); // stops at the first failure

    server.mode = "up";
    st = await q.flush(cfg, st, server.fetch); // not due yet: backoff respected
    expect(server.requests).toBe(1);
    advance(backoffMs(1));
    st = await q.flush(cfg, st, server.fetch);
    expect(st).toMatchObject({ queued: 0, lastError: undefined });
    expect([...server.stored.entries()]).toEqual([["a", 1], ["b", 1], ["c", 1]]);
  });

  it("keeps batches on auth failure and backs off exponentially", async () => {
    const { q, store, now } = setup();
    const server = fakeServer();
    server.mode = "401";
    await q.enqueue([ev("a")]);
    const st = await q.flush(cfg, { queued: 0 }, server.fetch);
    expect(st.lastError).toBe("auth");
    const [[, b]] = await store.entries();
    expect(b.attempts).toBe(1);
    expect(b.nextAttemptAt).toBe(now() + backoffMs(1));
    expect(backoffMs(2)).toBe(2 * backoffMs(1));
    expect(backoffMs(50)).toBe(30 * 60_000);
  });

  it("retries 5xx, drops only on explicit envelope rejection", async () => {
    const { q } = setup();
    const server = fakeServer();
    await q.enqueue([ev("a")]);
    server.mode = "500";
    expect((await q.flush(cfg, { queued: 0 }, server.fetch)).queued).toBe(1);
    const { q: q2 } = setup();
    server.mode = "422";
    await q2.enqueue([ev("b")]);
    expect(await q2.flush(cfg, { queued: 0 }, server.fetch)).toMatchObject({ queued: 0, lastError: "rejected" });
  });

  it("splits a batch the server finds too large", async () => {
    const { q, store } = setup();
    const server = fakeServer();
    server.mode = "413";
    await q.enqueue([ev("a"), ev("b"), ev("c")]);
    await q.flush(cfg, { queued: 0 }, server.fetch);
    expect((await store.entries()).map(([, b]) => b.events.length)).toEqual([2, 1]);
  });

  it("purges an excluded conversation's queued events", async () => {
    const { q } = setup();
    await q.enqueue([ev("a", "keep"), ev("b", "drop"), ev("c", "drop")]);
    await q.enqueue([ev("d", "drop")]);
    expect(await q.purge((e) => e.external_session_id === "drop")).toBe(3);
    expect(await q.queuedEvents()).toBe(1);
  });
});
