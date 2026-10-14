import { branchFrom, extractText, fillTimestamps, isoTime, outermost, titleFromDocument } from "./text";
import type { ParsedConversation, ParsedMessage, SiteAdapter, SiteId } from "./types";

// Every claude.ai selector lives in this block. The DOM changes without notice: when these stop
// matching, parse() yields no messages and the content script reports "adapter outdated".
// Layout as of 2026-10 (checked against live pages): a virtualised transcript of rows, one message
// each; the chat (/chat/<uuid>) and Claude Code on the web (/code/session_<id>) share it.
const SEL = {
  row: '[data-testid="transcript-row"]', // data-index = absolute position (rows outside the viewport are not in the DOM)
  user: '[data-testid="user-message"]',
  assistant: '[data-testid="assistant-message"]',
  turnKey: "[data-turn-key]", // stable per-message id; on the assistant element itself, on an ancestor for the user
  attachment: '[data-testid="file-thumbnail"]', // a prompt may be only an attachment (no user-message element)
  time: "time[datetime]",
  streaming: '[data-is-streaming="true"], [data-perf-row-streaming="true"]',
  // UI inside a message that is not message text: tool calls and search/status groups (Claude Code
  // interleaves several text blocks with tool cells in one reply), toolbars, code headers.
  skip: '[data-find-omitted], [data-testid="message-actions"], [role="toolbar"]',
  // Claude Code on the web: session title and repository live in the pane header, not in <title>.
  codeTitle: '[data-perf-region="header"] [data-pane-title]',
  codeRepo: '[data-testid="epitaxy-origin-label"]',
} as const;

const CHAT = /^\/chat\/([0-9a-fA-F-]{8,})\/?$/;
const CODE = /^\/code\/(session_[A-Za-z0-9]{8,})\/?$/;

/**
 * The transcript, as messages. Shared by both claude.ai adapters. `rowScopedReplyIds`: Claude Code
 * spreads one long agent turn over many rows that all carry the same turn key, so a reply's id there
 * is `<turn key>:<row position>` (else every row would look like a new revision of one message).
 */
function parseTranscript(doc: Document, rowScopedReplyIds: boolean): ParsedMessage[] {
  const messages: ParsedMessage[] = [];
  const seen = new Set<Element>();
  for (const hit of outermost(doc, `${SEL.user}, ${SEL.assistant}, ${SEL.attachment}`)) {
    const row = hit.closest(SEL.row);
    if (seen.has(row ?? hit)) continue; // one message per row (text + thumbnails are siblings)
    seen.add(row ?? hit);
    const el = row?.querySelector(`${SEL.user}, ${SEL.assistant}`) ?? hit;
    const isUser = !el.matches(SEL.assistant);
    const body = !isUser || el.matches(SEL.user) ? el : null;
    // Attachments are placeholders only: never their name or contents.
    const files = isUser ? (row ?? el).querySelectorAll(SEL.attachment).length : 0;
    const text = [...Array<string>(files).fill("[attachment]"), body ? extractText(body, { skip: SEL.skip }) : ""]
      .filter(Boolean)
      .join("\n\n");
    if (!text) continue; // e.g. a reply that is only tool activity
    const position = Number(row?.getAttribute("data-index"));
    const key = (isUser ? (el.closest(SEL.turnKey) ?? row?.querySelector(SEL.turnKey)) : el)?.getAttribute("data-turn-key")?.trim();
    const scoped = rowScopedReplyIds && !isUser && Number.isInteger(position);
    const id = key && scoped ? `${key}:${position}` : key;
    const time = isoTime(row?.querySelector(SEL.time)?.getAttribute("datetime"));
    const branch = branchFrom(row ?? el.parentElement, body ?? undefined);
    messages.push({
      ...(id ? { sourceMessageId: id } : {}),
      role: isUser ? "user" : "assistant",
      text,
      index: Number.isInteger(position) && position >= 0 ? position : messages.length,
      ...(time ? { timestamp: time } : {}),
      ...(branch ? { branch } : {}),
    });
  }
  fillTimestamps(messages);
  return messages;
}

function claudeSiteAdapter(
  site: SiteId,
  path: RegExp,
  meta: (doc: Document) => Pick<ParsedConversation, "title" | "projectKey" | "projectName">,
  rowScopedReplyIds = false,
): SiteAdapter {
  return {
    site,
    hosts: ["claude.ai"],
    isConversationUrl(url) {
      return this.hosts.includes(url.hostname) && path.test(url.pathname);
    },
    conversationIdFromUrl(url) {
      return this.isConversationUrl(url) ? path.exec(url.pathname)![1] : null;
    },
    parse(doc, url) {
      const conversationId = this.conversationIdFromUrl(url);
      if (!conversationId) return null;
      const { title, projectKey, projectName } = meta(doc);
      const conv: ParsedConversation = {
        site,
        conversationId,
        url: url.origin + url.pathname,
        ...(title ? { title } : {}),
        ...(projectKey ? { projectKey } : {}),
        ...(projectName ? { projectName } : {}),
        messages: parseTranscript(doc, rowScopedReplyIds),
        streaming: doc.querySelector(SEL.streaming) !== null,
      };
      return conv;
    },
    observationRoot(doc) {
      return doc.querySelector("main") ?? doc.body;
    },
  };
}

export const claudeAdapter = claudeSiteAdapter("claude_web", CHAT, (doc) => ({ title: titleFromDocument(doc, ["Claude"]) }));

export const claudeCodeAdapter = claudeSiteAdapter("claude_code_web", CODE, (doc) => {
  const title = doc.querySelector(SEL.codeTitle)?.textContent?.trim().slice(0, 500);
  const repo = doc.querySelector(SEL.codeRepo)?.textContent?.trim();
  return { title: title || undefined, projectKey: repo ? `repo:${repo}` : undefined, projectName: repo || undefined };
}, true);
