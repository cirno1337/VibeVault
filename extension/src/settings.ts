/**
 * User settings (chrome.storage.local). Only extension pages and the service worker read these; content
 * scripts ask the service worker for a yes/no instead, so page-adjacent code never sees the token.
 */
import type { CollectionPolicy, SiteId } from "./adapters/types";

export interface Settings {
  serverUrl: string;
  token: string;
  deviceId: string;
  deviceLabel: string;
  globallyEnabled: boolean;
  siteEnabled: Record<SiteId, boolean>;
  /** `${site}:${conversationId}`, ids only. */
  excluded: string[];
  allowIncognito: boolean;
}

export const SITE_LABELS: Record<SiteId, string> = {
  claude_web: "claude.ai",
  claude_code_web: "claude.ai/code",
  chatgpt_web: "chatgpt.com",
};

export function defaultSettings(): Settings {
  return {
    serverUrl: "",
    token: "",
    deviceId: `browser-${crypto.randomUUID().slice(0, 8)}`,
    deviceLabel: "Browser",
    globallyEnabled: true,
    siteEnabled: { claude_web: true, claude_code_web: true, chatgpt_web: true },
    excluded: [],
    allowIncognito: false,
  };
}

export const exclusionKey = (site: SiteId, conversationId: string) => `${site}:${conversationId}`;

export function policyOf(s: Settings): CollectionPolicy {
  return {
    globallyEnabled: s.globallyEnabled,
    siteEnabled: s.siteEnabled,
    excludedConversations: new Set(s.excluded),
  };
}

export type Decision = "allowed" | "disabled" | "excluded";

export function decide(p: CollectionPolicy, site: SiteId, conversationId: string): Decision {
  if (!p.globallyEnabled || !p.siteEnabled[site]) return "disabled";
  if (p.excludedConversations.has(exclusionKey(site, conversationId))) return "excluded";
  return "allowed";
}

/** Normalises a user-entered server URL to an origin (no path), or throws. */
export function normalizeServerUrl(input: string): string {
  const u = new URL(input.trim());
  if (u.protocol !== "https:" && !(u.protocol === "http:" && ["localhost", "127.0.0.1"].includes(u.hostname))) {
    throw new Error("Use https:// (plain http only for localhost)");
  }
  return u.origin;
}

export async function loadSettings(): Promise<Settings> {
  const { settings } = await chrome.storage.local.get("settings");
  const stored = settings as Partial<Settings> | undefined;
  const defaults = defaultSettings();
  // Sites added in later versions default to on (stored maps predate them).
  const merged = { ...defaults, ...stored, siteEnabled: { ...defaults.siteEnabled, ...stored?.siteEnabled } };
  if (!settings) await chrome.storage.local.set({ settings: merged }); // pin the generated device id
  return merged;
}

export async function saveSettings(patch: Partial<Settings>): Promise<Settings> {
  const next = { ...(await loadSettings()), ...patch };
  await chrome.storage.local.set({ settings: next });
  return next;
}
