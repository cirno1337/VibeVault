import { describe, expect, it } from "vitest";
import type { ParsedConversation, ParsedMessage } from "../src/adapters/types";
import { normalize, type ConversationState } from "../src/normalizer";

const ctx = (iso = "2026-10-11T10:00:00.000Z") => ({ deviceId: "brave-pc", deviceLabel: "Brave (PC)", now: new Date(iso) });
const conv = (messages: Partial<ParsedMessage>[], extra: Partial<ParsedConversation> = {}): ParsedConversation => ({
  site: "claude_web",
  conversationId: "conv1",
  url: "https://claude.ai/chat/conv1",
  title: "Title",
  streaming: false,
  messages: messages.map((m, index) => ({ role: index % 2 ? "assistant" : "user", text: `m${index}`, index, ...m })),
  ...extra,
});

describe("normalizer", () => {
  it("builds schema-1 events with deterministic ids, sequence and monotonic first-seen times", async () => {
    const { events, state } = await normalize(conv([{}, {}, { sourceMessageId: "abc" }]), {}, ctx());
    expect(events.map((e) => [e.external_event_id, e.revision, e.sequence, e.actor])).toEqual([
      ["conv1:i0", 1, 0, "user"],
      ["conv1:i1", 1, 1, "assistant"],
      ["conv1:abc", 1, 2, "user"],
    ]);
    expect(events.map((e) => e.occurred_at)).toEqual([
      "2026-10-11T10:00:00.000Z",
      "2026-10-11T10:00:00.001Z",
      "2026-10-11T10:00:00.002Z",
    ]);
    expect(events[0]).toMatchObject({
      schema_version: "1",
      source: "claude_web",
      device_id: "brave-pc",
      device_label: "Brave (PC)",
      external_session_id: "conv1",
      session_title: "Title",
      project: { key: "https://claude.ai/chat/conv1" },
      event_type: "message",
      content: "m0",
      metadata: { url: "https://claude.ai/chat/conv1", time_source: "first_seen" },
      redaction: { status: "none", count: 0, rules: {} },
    });
    expect(Object.keys(state)).toEqual(["conv1:i0", "conv1:i1", "conv1:abc"]);
  });

  it("emits nothing for unchanged messages, only new ones", async () => {
    const first = await normalize(conv([{}, {}]), {}, ctx());
    const again = await normalize(conv([{}, {}]), first.state, ctx("2026-10-11T11:00:00Z"));
    expect(again.events).toEqual([]);
    const more = await normalize(conv([{}, {}, {}]), first.state, ctx("2026-10-11T11:00:00Z"));
    expect(more.events.map((e) => [e.external_event_id, e.occurred_at])).toEqual([["conv1:i2", "2026-10-11T11:00:00.000Z"]]);
  });

  it("bumps the revision when the same message's text changes, keeping first-seen time", async () => {
    const first = await normalize(conv([{ text: "draft answer" }]), {}, ctx());
    const edited = await normalize(conv([{ text: "edited answer" }]), first.state, ctx("2026-10-11T12:00:00Z"));
    expect(edited.events).toHaveLength(1);
    expect(edited.events[0]).toMatchObject({ external_event_id: "conv1:i0", revision: 2, content: "edited answer", occurred_at: "2026-10-11T10:00:00.000Z" });
    const third = await normalize(conv([{ text: "third" }]), edited.state, ctx());
    expect(third.events[0].revision).toBe(3);
  });

  it("holds back streaming parses entirely", async () => {
    const r = await normalize(conv([{}, {}], { streaming: true }), {}, ctx());
    expect(r.events).toEqual([]);
    expect(r.state).toEqual({});
  });

  it("redacts before anything leaves the normalizer, and never stores secrets in state", async () => {
    const secret = "sk-ant-api03-" + "Q".repeat(24);
    const r = await normalize(conv([{ text: `my key is ${secret}` }], { title: `debug ${secret}` }), {}, ctx());
    expect(r.events[0].content).toBe("my key is [REDACTED:anthropic_key]");
    expect(r.events[0].session_title).toBe("debug [REDACTED:anthropic_key]");
    expect(r.events[0].redaction).toEqual({ status: "redacted", count: 2, rules: { anthropic_key: 2 } });
    expect(JSON.stringify(r)).not.toContain(secret);
  });

  it("uses the page timestamp when the adapter has one, and keeps branch info in metadata", async () => {
    const r = await normalize(conv([{ timestamp: "2026-01-01T00:00:00Z", branch: { position: 2, total: 3 } }]), {}, ctx());
    expect(r.events[0].occurred_at).toBe("2026-01-01T00:00:00Z");
    expect(r.events[0].metadata).toMatchObject({ time_source: "page", branch: { position: 2, total: 3 } });
  });

  it("skips empty messages and caps huge ones below the server limit", async () => {
    const big = "x".repeat(300_000);
    const r = await normalize(conv([{ text: "" }, { text: big }]), {}, ctx());
    expect(r.events).toHaveLength(1);
    expect(new TextEncoder().encode(r.events[0].content).length).toBeLessThan(1_000_000);
    expect(r.events[0].metadata.truncated).toBe(true);
  });

  it("uses the page's project key when the adapter has one, else the conversation URL", async () => {
    const r = await normalize(conv([{}], { projectKey: "repo:VibeVault", projectName: "VibeVault" }), {}, ctx());
    expect(r.events[0].project).toEqual({ key: "repo:VibeVault", name: "VibeVault" });
    const chat = await normalize(conv([{}]), {}, ctx());
    expect(chat.events[0].project).toEqual({ key: "https://claude.ai/chat/conv1" }); // no name: stays unresolved
  });

  it("is deterministic across runs (same state in, same ids/hashes out)", async () => {
    const a = await normalize(conv([{}, {}]), {} as ConversationState, ctx());
    const b = await normalize(conv([{}, {}]), {} as ConversationState, ctx());
    expect(a).toEqual(b);
  });
});
