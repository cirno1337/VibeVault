import { branchFrom, extractText, fillTimestamps, isoTime, outermost, titleFromDocument } from "./text";
import type { ParsedConversation, ParsedMessage, SiteAdapter } from "./types";

// Every chatgpt.com selector lives in this block. The DOM changes without notice: when these stop
// matching, parse() yields no messages and the content script reports "adapter outdated".
// Layout as of 2026-10 (checked against saved pages): turns hold "search units", one per message.
const SEL = {
  turn: "[data-turn-key]",
  unit: "[data-chatgpt-search-unit-key]", // "<turn>:<n>:user" | "<turn>:<n>:assistant"
  messageIds: "data-chatgpt-search-message-ids",
  selection: "[data-chatgpt-selection-conversation-id]", // carries the conversation + message id of replies
  time: "time[datetime]",
  // The page may keep other conversations mounted (several <main>s); only the one for this URL counts.
  container: "main",
  streaming: '[data-testid="stop-button"]',
  turnState: "[data-talvt-turn-state]",
  skip: '[data-markdown-copy="exclude"], .turn-action-controls',
} as const;

const BUSY_STATE = /stream|progress|generat|pending|running|typing|thinking/i;
// /c/<id>, and the same inside a custom GPT or project: /g/<gizmo-or-project>/c/<id>
const CONVERSATION = /^(?:\/g\/[^/]+)?\/c\/([A-Za-z0-9-]{8,})\/?$/;
const ROLES = new Set<ParsedMessage["role"]>(["user", "assistant", "system", "tool"]);

/** The part of the page showing `conversationId`, or null when the page shows it nowhere. */
function conversationRoot(doc: Document, conversationId: string): ParentNode | null {
  const tagged = Array.from(doc.querySelectorAll(SEL.selection));
  if (!tagged.length) return doc; // nothing to disambiguate with: assume the page shows one conversation
  const mine = tagged.find((el) => el.getAttribute("data-chatgpt-selection-conversation-id") === conversationId);
  return mine ? (mine.closest(SEL.container) ?? doc) : null;
}

export const chatgptAdapter: SiteAdapter = {
  site: "chatgpt_web",
  hosts: ["chatgpt.com"],

  isConversationUrl(url) {
    return this.hosts.includes(url.hostname) && CONVERSATION.test(url.pathname);
  },

  conversationIdFromUrl(url) {
    return this.isConversationUrl(url) ? CONVERSATION.exec(url.pathname)![1] : null;
  },

  parse(doc, url) {
    const conversationId = this.conversationIdFromUrl(url);
    if (!conversationId) return null;
    const root = conversationRoot(doc, conversationId);
    const messages: ParsedMessage[] = [];
    for (const unit of root ? outermost(root, SEL.unit) : []) {
      const role = unit.getAttribute("data-chatgpt-search-unit-key")!.split(":").pop() as ParsedMessage["role"];
      if (!ROLES.has(role)) continue;
      const text = extractText(unit, { skip: SEL.skip });
      if (!text) continue;
      const id =
        unit.querySelector(SEL.selection)?.getAttribute("data-chatgpt-selection-message-id")?.trim() ||
        unit.getAttribute(SEL.messageIds)?.trim().split(/\s+/)[0];
      const turn = unit.closest(SEL.turn);
      const time = isoTime(turn?.querySelector(SEL.time)?.getAttribute("datetime"));
      const branch = branchFrom(turn, unit);
      messages.push({
        ...(id ? { sourceMessageId: id } : {}),
        role,
        text,
        index: messages.length,
        ...(time ? { timestamp: time } : {}),
        ...(branch ? { branch } : {}),
      });
    }
    const busy = Array.from((root ?? doc).querySelectorAll(SEL.turnState)).some((el) =>
      BUSY_STATE.test(el.getAttribute("data-talvt-turn-state") ?? ""),
    );
    fillTimestamps(messages);
    const conv: ParsedConversation = {
      site: "chatgpt_web",
      conversationId,
      url: url.origin + url.pathname,
      title: titleFromDocument(doc, ["ChatGPT"]),
      messages,
      streaming: busy || doc.querySelector(SEL.streaming) !== null,
    };
    return conv;
  },

  observationRoot(doc) {
    return doc.body; // several <main>s may come and go: watch them all
  },
};
