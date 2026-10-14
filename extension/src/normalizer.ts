/**
 * ParsedConversation -> EventIn[] (server schema "1", docs/event-schema.md), emitting only what is new
 * or changed since the last parse. Pure apart from the hash; state is passed in and returned.
 */
import type { MessageFingerprint, ParsedConversation, ParsedMessage } from "./adapters/types";
import { MAX_CONTENT_CHARS } from "./limits";
import { RedactionReport, redactObj } from "./redactor";


export interface EventIn {
  schema_version: "1";
  source: string;
  device_id: string;
  device_label?: string;
  external_event_id: string;
  revision: number;
  external_session_id: string;
  session_title?: string;
  project: { key: string; name?: string };
  event_type: "message";
  actor: "user" | "assistant" | "tool" | "system";
  occurred_at: string;
  sequence: number;
  content: string;
  metadata: Record<string, unknown>;
  redaction: { status: "none" | "redacted"; count: number; rules: Record<string, number> };
}

/** What we remember per message: the spec's fingerprint plus first-seen time (for `occurred_at`). */
export interface StoredFingerprint extends MessageFingerprint {
  firstSeenAt: string;
}

/** Fingerprints of one conversation, keyed by externalEventId. */
export type ConversationState = Record<string, StoredFingerprint>;

export interface NormalizeContext {
  deviceId: string;
  deviceLabel?: string;
  now: Date;
}

export function externalEventId(conversationId: string, m: ParsedMessage): string {
  return `${conversationId}:${m.sourceMessageId ?? `i${m.index}`}`;
}

export async function contentHash(role: string, text: string): Promise<string> {
  const data = new TextEncoder().encode(`${role}\u0000${text}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Returns the events to send (already redacted) and the updated state. Streaming parses emit nothing:
 * the content script holds them back, and this is the second line of defence.
 */
export async function normalize(
  conv: ParsedConversation,
  state: ConversationState,
  ctx: NormalizeContext,
): Promise<{ events: EventIn[]; state: ConversationState }> {
  if (conv.streaming) return { events: [], state };
  const next: ConversationState = { ...state };
  const events: EventIn[] = [];
  // occurred_at: the page timestamp if any, else first-seen time, kept strictly increasing in
  // message order so a conversation imported in one go still sorts correctly.
  let lastMs = 0;
  for (const m of conv.messages) {
    if (!m.text) continue;
    const id = externalEventId(conv.conversationId, m);
    const hash = await contentHash(m.role, m.text);
    const prev = next[id];
    const firstSeen = prev ? Date.parse(prev.firstSeenAt) : Math.max(ctx.now.getTime(), lastMs + 1);
    lastMs = Math.max(lastMs, firstSeen);
    if (prev?.contentHash === hash) continue;
    const revision = prev ? prev.revision + 1 : 1;
    const firstSeenAt = new Date(firstSeen).toISOString();
    next[id] = { externalEventId: id, revision, contentHash: hash, firstSeenAt };

    const report = new RedactionReport();
    let content = redactObj(m.text, report);
    if (content.length > MAX_CONTENT_CHARS) content = content.slice(0, MAX_CONTENT_CHARS) + "\n[truncated by VibeVault extension]";
    const title = conv.title ? redactObj(conv.title, report) : undefined;
    events.push({
      schema_version: "1",
      source: conv.site,
      device_id: ctx.deviceId,
      ...(ctx.deviceLabel ? { device_label: ctx.deviceLabel } : {}),
      external_event_id: id,
      revision,
      external_session_id: conv.conversationId,
      ...(title ? { session_title: title } : {}),
      project: conv.projectKey ? { key: conv.projectKey, ...(conv.projectName ? { name: conv.projectName } : {}) } : { key: conv.url },
      event_type: "message",
      actor: m.role,
      occurred_at: m.timestamp ?? firstSeenAt,
      sequence: m.index,
      content,
      metadata: {
        url: conv.url,
        ...(m.branch ? { branch: m.branch } : {}),
        time_source: m.timestamp ? "page" : "first_seen",
        ...(m.text.length > MAX_CONTENT_CHARS ? { truncated: true } : {}),
      },
      redaction: report.toEventField(),
    });
  }
  return { events, state: next };
}
