/** Small helpers shared by the popup and options pages. */
import type { DeliveryState, SiteId } from "./adapters/types";
import type { PageMessage } from "./messages";
import { SITE_LABELS, type Settings } from "./settings";

export const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

export function send<T>(msg: PageMessage): Promise<T> {
  return chrome.runtime.sendMessage(msg) as Promise<T>;
}

export const ERROR_TEXT: Record<NonNullable<DeliveryState["lastError"]>, string> = {
  network: "server unreachable (will retry)",
  auth: "token rejected - check settings",
  rejected: "server rejected some events",
  disabled: "not configured (no server URL or token)",
  permission: "no permission for the server URL - save settings again",
};

export function formatTime(ms?: number): string {
  return ms ? new Date(ms).toLocaleString() : "never";
}

/** Renders one checkbox per site into `container`, calling `onChange` with the new map. */
export function renderSiteSwitches(container: HTMLElement, s: Settings, onChange: (v: Settings["siteEnabled"]) => void): void {
  container.replaceChildren();
  for (const site of Object.keys(SITE_LABELS) as SiteId[]) {
    const label = document.createElement("label");
    label.className = "row";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = s.siteEnabled[site];
    box.disabled = !s.globallyEnabled;
    box.addEventListener("change", () => onChange({ ...s.siteEnabled, [site]: box.checked }));
    label.append(`… on ${SITE_LABELS[site]}`, box);
    container.append(label);
  }
}
