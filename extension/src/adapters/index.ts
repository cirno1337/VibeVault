import { chatgptAdapter } from "./chatgpt";
import { claudeAdapter, claudeCodeAdapter } from "./claude";
import type { SiteAdapter, SiteId } from "./types";

export const ADAPTERS: readonly SiteAdapter[] = [claudeAdapter, claudeCodeAdapter, chatgptAdapter];

/** The adapter for a conversation URL, else the first one for its host (for "idle" status). */
export function adapterForUrl(url: URL): SiteAdapter | undefined {
  return ADAPTERS.find((a) => a.isConversationUrl(url)) ?? ADAPTERS.find((a) => a.hosts.includes(url.hostname));
}

export function adapterForSite(site: SiteId): SiteAdapter | undefined {
  return ADAPTERS.find((a) => a.site === site);
}
