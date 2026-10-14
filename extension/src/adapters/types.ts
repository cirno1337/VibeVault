/**
 * Browser-extension adapter contracts. Implemented by src/adapters/{claude,chatgpt}.ts.
 *
 * Layering:
 *   site adapter (DOM-specific, isolated, fixture-tested)
 *     -> ParsedConversation (site-independent)
 *       -> normalizer -> EventIn (server schema_version "1", see docs/event-schema.md)
 *         -> redactor -> local queue -> uploader
 *
 * Nothing below the adapter may know about selectors or DOM structure, and the server schema
 * must never contain site-specific fields (put them in `metadata` / `raw_payload` if useful).
 */

export type SiteId = "chatgpt_web" | "claude_web" | "claude_code_web";

/** What an adapter extracts from a page: plain data, no DOM nodes. */
export interface ParsedMessage {
  /** Stable id from the page when one exists (e.g. a data attribute), else undefined. */
  sourceMessageId?: string;
  role: "user" | "assistant" | "system" | "tool";
  /** Visible text. Markdown/code blocks are preserved as text; images/attachments become placeholders. */
  text: string;
  /** 0-based position in the conversation as rendered. */
  index: number;
  /** For regenerated/edited branches when the site exposes them. */
  branch?: { id?: string; position?: number; total?: number };
  /** ISO time if the page exposes one; otherwise the normalizer uses first-seen time. */
  timestamp?: string;
}

export interface ParsedConversation {
  site: SiteId;
  /** Conversation id derived from the URL (e.g. /c/<id> or /chat/<id>). */
  conversationId: string;
  url: string;
  title?: string;
  /** Source-side project identity when the page shows one (e.g. a Claude Code session's repository);
   *  otherwise the normalizer uses the conversation URL. */
  projectKey?: string;
  /** Human name for projectKey (e.g. the repository name): lets the server join a same-named project. */
  projectName?: string;
  messages: ParsedMessage[];
  /** True while the page is still streaming a response; the normalizer waits before emitting. */
  streaming: boolean;
}

/** Everything DOM-specific lives behind this interface. Selectors must not leak out of adapters. */
export interface SiteAdapter {
  readonly site: SiteId;
  /** Hostnames this adapter may run on (must match manifest host_permissions exactly). Several adapters
   *  may share a host (claude.ai chat and Claude Code); isConversationUrl tells them apart. */
  readonly hosts: readonly string[];
  /** True for URLs that are a single conversation (not home, settings, shared links…). */
  isConversationUrl(url: URL): boolean;
  conversationIdFromUrl(url: URL): string | null;
  /** Pure function of a document: unit-testable against saved HTML fixtures with no browser. */
  parse(doc: Document, url: URL): ParsedConversation | null;
  /** Elements to observe for incremental updates; adapters may return the document body as a fallback. */
  observationRoot(doc: Document): Element | null;
}

/** Decision about what may leave the browser. Evaluated before anything is queued. */
export interface CollectionPolicy {
  globallyEnabled: boolean;
  siteEnabled: Record<SiteId, boolean>;
  /** Conversation ids the user excluded; matching conversations are never read beyond the id. */
  excludedConversations: ReadonlySet<string>;
}

export interface QueuedBatch {
  id: string;
  events: unknown[]; // EventIn[] after redaction
  attempts: number;
  nextAttemptAt: number;
}

export interface DeliveryState {
  lastSuccessAt?: number;
  /** "permission": the server origin was not granted to the extension (options page). */
  lastError?: "network" | "auth" | "rejected" | "disabled" | "permission";
  queued: number;
}

/** Per-tab collector state reported by the content script (shown in the popup). */
export type TabStatus =
  | { state: "collecting"; site: SiteId; conversationId: string; messages: number }
  | { state: "excluded" | "disabled"; site: SiteId; conversationId?: string }
  | { state: "idle"; site: SiteId } // not a conversation page
  | { state: "adapter_outdated"; site: SiteId; conversationId: string };

/** Diffing contract: the normalizer compares fresh parses with what it already emitted. */
export interface MessageFingerprint {
  externalEventId: string; // deterministic: `${conversationId}:${sourceMessageId ?? `i${index}`}`
  revision: number; // bumps when the same message's text changes (edit / finished streaming)
  contentHash: string;
}
