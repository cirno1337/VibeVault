import type { DeliveryState, ParsedConversation, SiteId, TabStatus } from "./adapters/types";
import type { Decision } from "./settings";

/** Content script -> service worker. */
export type ContentMessage =
  | { type: "check"; site: SiteId; conversationId: string }
  | { type: "parsed"; conversation: ParsedConversation }
  | { type: "status"; status: TabStatus };

/** Extension pages (popup, options) -> service worker. */
export type PageMessage =
  | { type: "getState"; tabId?: number }
  | { type: "flush" }
  | { type: "exclude" | "include"; site: SiteId; conversationId: string }
  | { type: "testConnection" }
  /** The server URL changed: what was "already sent" refers to the old server. */
  | { type: "serverChanged" };

export interface StateReply {
  delivery: DeliveryState;
  tab?: TabStatus;
}

export interface TestReply {
  ok: boolean;
  message: string;
}

export type CheckReply = Decision;
