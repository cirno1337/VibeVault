/**
 * Content script for claude.ai / chatgpt.com: observes the open conversation, parses it with the site
 * adapter once it stops changing, and hands the result to the service worker. It never reads settings or
 * the token; it asks the worker whether this conversation may be read at all before parsing it.
 */
import { adapterForUrl } from "./adapters";
import type { TabStatus } from "./adapters/types";
import { MAX_CONTENT_CHARS } from "./limits";
import type { CheckReply, ContentMessage } from "./messages";

const DEBOUNCE_MS = 1500;
const OUTDATED_AFTER_MS = 15_000; // a conversation URL with no recognisable messages after this = adapter outdated
const POLL_MS = 2000; // SPA navigation and replaced roots don't always produce observed mutations

// One page can move between adapters without a reload (claude.ai chat <-> Claude Code), so the adapter
// is chosen per URL on every tick.
if (adapterForUrl(new URL(location.href))) start();

function start(): void {
  let a = adapterForUrl(new URL(location.href))!;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastUrl = location.href;
  let navigatedAt = Date.now();
  let lastSent = "";
  let lastStatus = "";
  let root: Element | null = null;
  let alive = true;
  const observer = new MutationObserver(() => schedule());

  async function send<T>(msg: ContentMessage): Promise<T | undefined> {
    try {
      return (await chrome.runtime.sendMessage(msg)) as T;
    } catch {
      // Extension reloaded/updated: this script is orphaned, stop quietly.
      alive = false;
      observer.disconnect();
      return undefined;
    }
  }

  function report(status: TabStatus): void {
    const key = JSON.stringify(status);
    if (key === lastStatus) return;
    lastStatus = key;
    void send({ type: "status", status });
  }

  function schedule(delay = DEBOUNCE_MS): void {
    if (!alive) return;
    clearTimeout(timer);
    timer = setTimeout(() => void tick(), delay);
  }

  function observe(): void {
    const next = a.observationRoot(document);
    if (next === root && root?.isConnected) return;
    observer.disconnect();
    root = next;
    if (root) observer.observe(root, { childList: true, subtree: true, characterData: true });
  }

  async function tick(): Promise<void> {
    const url = new URL(location.href);
    a = adapterForUrl(url) ?? a;
    observe();
    const conversationId = a.conversationIdFromUrl(url);
    if (!conversationId) return report({ state: "idle", site: a.site });

    const decision = await send<CheckReply>({ type: "check", site: a.site, conversationId });
    if (decision === undefined) return;
    if (decision !== "allowed") return report({ state: decision, site: a.site, conversationId });

    const parsed = a.parse(document, url);
    if (!parsed || !parsed.messages.length) {
      if (Date.now() - navigatedAt >= OUTDATED_AFTER_MS) report({ state: "adapter_outdated", site: a.site, conversationId });
      else schedule(OUTDATED_AFTER_MS - (Date.now() - navigatedAt));
      return;
    }
    report({ state: "collecting", site: a.site, conversationId, messages: parsed.messages.length });
    if (parsed.streaming) return schedule(); // hold back until the response has finished
    // A huge paste (tens of MB) would exceed the 64 MB message limit and is cut by the normalizer
    // anyway; one extra char lets it see that the text was longer.
    for (const m of parsed.messages) if (m.text.length > MAX_CONTENT_CHARS) m.text = m.text.slice(0, MAX_CONTENT_CHARS + 1);
    const key = JSON.stringify(parsed);
    if (key === lastSent) return;
    lastSent = key;
    await send({ type: "parsed", conversation: parsed });
  }

  setInterval(() => {
    if (!alive) return;
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      navigatedAt = Date.now();
      lastSent = "";
      schedule();
    }
    if (!root?.isConnected) {
      observe();
      schedule();
    }
  }, POLL_MS);

  observe();
  schedule(500);
}
