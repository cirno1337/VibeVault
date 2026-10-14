/**
 * Service worker: the only place that holds the token and talks to the VibeVault server.
 * content script -> (policy check) -> normalizer (+ redaction) -> IndexedDB queue -> uploader.
 */
import { adapterForSite } from "./adapters";
import type { DeliveryState, ParsedConversation, TabStatus } from "./adapters/types";
import type { CheckReply, ContentMessage, PageMessage, StateReply, TestReply } from "./messages";
import { normalize, type ConversationState } from "./normalizer";
import { deliver, Queue } from "./queue";
import { decide, exclusionKey, loadSettings, policyOf, saveSettings, type Settings } from "./settings";
import { IdbKV } from "./store";

const queue = new Queue(new IdbKV("batches"));
const fingerprints = new IdbKV<ConversationState>("fingerprints");
const FLUSH_ALARM = "vibevault-flush";

// Parses from several tabs may arrive at once; fingerprint read-modify-write must not interleave.
let chain: Promise<unknown> = Promise.resolve();
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn, fn);
  chain = run.catch(() => undefined);
  return run;
}

chrome.runtime.onInstalled.addListener(() => void setup());
chrome.runtime.onStartup.addListener(() => void setup());

async function setup(): Promise<void> {
  await loadSettings(); // creates defaults (and the device id) on first run
  await chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 1 });
  await updateBadge(await getDelivery());
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === FLUSH_ALARM) void serial(flush);
});

chrome.tabs.onRemoved.addListener((tabId) => void chrome.storage.session.remove(`tab:${tabId}`));

chrome.runtime.onMessage.addListener((msg: ContentMessage | PageMessage, sender, reply) => {
  const fromExtensionPage = sender.url?.startsWith(chrome.runtime.getURL("")) ?? false;
  const handled = fromExtensionPage ? onPageMessage(msg as PageMessage) : onContentMessage(msg as ContentMessage, sender);
  handled.then(reply, (e: unknown) => {
    console.error("[VibeVault]", e);
    reply(undefined);
  });
  return true; // async reply
});

async function onContentMessage(msg: ContentMessage, sender: chrome.runtime.MessageSender): Promise<unknown> {
  const tab = sender.tab;
  if (!tab?.id || !sender.url) return undefined;
  const pageUrl = new URL(sender.url);
  const site = msg.type === "check" ? msg.site : msg.type === "parsed" ? msg.conversation.site : msg.status.site;
  const adapter = adapterForSite(site);
  // A content script may only speak for the site it actually runs on.
  if (!adapter?.hosts.includes(pageUrl.hostname)) return undefined;

  switch (msg.type) {
    case "check":
      return checkConversation(msg.site, msg.conversationId, tab);
    case "status":
      await chrome.storage.session.set({ [`tab:${tab.id}`]: msg.status });
      await updateTabBadge(tab.id, msg.status);
      return undefined;
    case "parsed": {
      const conv = msg.conversation;
      // No check of the id against sender.url: after in-app (pushState) navigation it still holds the
      // URL the tab was loaded with, which silently dropped every conversation opened from the sidebar.
      if (!adapter.conversationIdFromUrl(new URL(conv.url))) return undefined;
      if ((await checkConversation(conv.site, conv.conversationId, tab)) !== "allowed") return undefined;
      return serial(() => ingest(conv));
    }
  }
}

async function checkConversation(site: ParsedConversation["site"], conversationId: string, tab: chrome.tabs.Tab): Promise<CheckReply> {
  const s = await loadSettings();
  if (tab.incognito && !s.allowIncognito) return "disabled";
  return decide(policyOf(s), site, conversationId);
}

async function ingest(conv: ParsedConversation): Promise<void> {
  const s = await loadSettings();
  const key = exclusionKey(conv.site, conv.conversationId);
  const { events, state } = await normalize(conv, (await fingerprints.get(key)) ?? {}, {
    deviceId: s.deviceId,
    deviceLabel: s.deviceLabel,
    now: new Date(),
  });
  if (!events.length) return;
  // Queue before saving fingerprints: a crash in between re-sends, and the server dedupes.
  await queue.enqueue(events);
  await fingerprints.put(key, state);
  await flush();
}

async function getDelivery(): Promise<DeliveryState> {
  const { delivery } = await chrome.storage.local.get("delivery");
  return (delivery as DeliveryState | undefined) ?? { queued: 0 };
}

async function serverConfig(s: Settings): Promise<{ url: string; token: string } | DeliveryState["lastError"]> {
  if (!s.serverUrl || !s.token) return "disabled";
  const granted = await chrome.permissions.contains({ origins: [`${s.serverUrl}/*`] });
  return granted ? { url: s.serverUrl, token: s.token } : "permission";
}

async function flush(): Promise<DeliveryState> {
  const s = await loadSettings();
  let delivery = await getDelivery();
  const cfg = await serverConfig(s);
  if (typeof cfg === "object") delivery = await queue.flush(cfg, delivery);
  else delivery = { ...delivery, lastError: cfg, queued: await queue.queuedEvents() };
  await chrome.storage.local.set({ delivery });
  await updateBadge(delivery);
  return delivery;
}

async function onPageMessage(msg: PageMessage): Promise<StateReply | TestReply | DeliveryState | undefined> {
  switch (msg.type) {
    case "getState": {
      const tabKey = `tab:${msg.tabId}`;
      const tab = msg.tabId === undefined ? undefined : ((await chrome.storage.session.get(tabKey))[tabKey] as TabStatus | undefined);
      return { delivery: { ...(await getDelivery()), queued: await queue.queuedEvents() }, tab };
    }
    case "flush":
      return serial(flush);
    case "exclude":
    case "include":
      return serial(() => setExcluded(msg.site, msg.conversationId, msg.type === "exclude"));
    case "testConnection":
      return testConnection();
    case "serverChanged":
      // Fingerprints say what the *previous* server already has; forget them so every conversation
      // is sent in full to the new one as it is opened again.
      return serial(async () => {
        for (const [key] of await fingerprints.entries()) await fingerprints.delete(key);
        return flush();
      });
  }
}

async function setExcluded(site: ParsedConversation["site"], conversationId: string, exclude: boolean): Promise<DeliveryState> {
  const s = await loadSettings();
  const key = exclusionKey(site, conversationId);
  const excluded = s.excluded.filter((k) => k !== key);
  if (exclude) {
    excluded.push(key);
    // Keep only the id: forget content fingerprints and anything not yet sent.
    await fingerprints.delete(key);
    await queue.purge((e) => e.source === site && e.external_session_id === conversationId);
  }
  await saveSettings({ excluded });
  const delivery = { ...(await getDelivery()), queued: await queue.queuedEvents() };
  await chrome.storage.local.set({ delivery });
  await updateBadge(delivery);
  return delivery;
}

async function testConnection(): Promise<TestReply> {
  const cfg = await serverConfig(await loadSettings());
  if (cfg === "disabled") return { ok: false, message: "Set the server URL and token first." };
  if (cfg === "permission") return { ok: false, message: "The extension has no permission for this server; save the settings again and allow access." };
  if (typeof cfg !== "object") return { ok: false, message: "Not configured." };
  const outcome = await deliver([], cfg, fetch); // an empty batch: authenticates without storing anything
  if (outcome.kind === "ok") return { ok: true, message: "Connected; the token was accepted." };
  if (outcome.kind === "retry" && outcome.error === "auth") return { ok: false, message: "The server rejected the token (401/403)." };
  if (outcome.kind === "retry") return { ok: false, message: "Could not reach the server (network error or 5xx)." };
  return { ok: false, message: "The server rejected the request." };
}

async function updateBadge(d: DeliveryState): Promise<void> {
  const problem = d.lastError === "auth" || d.lastError === "permission" || d.lastError === "rejected";
  await chrome.action.setBadgeBackgroundColor({ color: problem ? "#c62828" : "#555" });
  await chrome.action.setBadgeText({ text: problem ? "!" : d.queued ? String(Math.min(d.queued, 999)) : "" });
}

async function updateTabBadge(tabId: number, status: TabStatus): Promise<void> {
  await chrome.action.setTitle({
    tabId,
    title:
      status.state === "adapter_outdated"
        ? "VibeVault: this page's layout is not recognised (adapter outdated) — nothing is collected"
        : `VibeVault: ${status.state}`,
  });
}
