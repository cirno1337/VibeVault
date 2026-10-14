import type { SiteId } from "./adapters/types";
import type { StateReply } from "./messages";
import { loadSettings, saveSettings, SITE_LABELS, type Settings } from "./settings";
import { $, ERROR_TEXT, formatTime, renderSiteSwitches, send } from "./ui";

async function activeTabId(): Promise<number | undefined> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab?.id;
}

async function render(): Promise<void> {
  const s = await loadSettings();
  const tabId = await activeTabId();
  const state = await send<StateReply>({ type: "getState", tabId });

  const global = $<HTMLInputElement>("global");
  global.checked = s.globallyEnabled;
  global.onchange = () => void update({ globallyEnabled: global.checked });
  renderSiteSwitches($("sites"), s, (siteEnabled) => void update({ siteEnabled }));

  const tab = state.tab;
  const tabText = $("tab");
  const button = $<HTMLButtonElement>("exclude");
  button.hidden = true;
  tabText.className = "muted";
  if (tab) {
    const where = SITE_LABELS[tab.site];
    const seen = tab.state === "collecting" ? ` (${tab.messages} messages seen)` : "";
    const text: Record<typeof tab.state, string> = {
      collecting: `Collecting this ${where} conversation${seen}.`,
      excluded: `This ${where} conversation is excluded.`,
      disabled: `Collection is off for ${where}.`,
      idle: `${where}: not a conversation page.`,
      adapter_outdated: `Adapter outdated: this ${where} page layout is not recognised, nothing is collected.`,
    };
    tabText.textContent = text[tab.state];
    if (tab.state === "adapter_outdated") tabText.className = "bad";
    if ((tab.state === "collecting" || tab.state === "excluded" || tab.state === "adapter_outdated") && tab.conversationId) {
      const exclude = tab.state !== "excluded";
      button.hidden = false;
      button.textContent = exclude ? "Exclude this conversation" : "Collect this conversation again";
      button.onclick = () => void toggle(tab.site, tab.conversationId!, exclude);
    }
  }

  const d = state.delivery;
  $("queued").textContent = String(d.queued);
  $("success").textContent = formatTime(d.lastSuccessAt);
  const err = $("error");
  err.textContent = d.lastError ? ERROR_TEXT[d.lastError] : "none";
  err.className = d.lastError && d.lastError !== "disabled" ? "bad" : "";

  const link = $<HTMLAnchorElement>("server");
  link.hidden = !s.serverUrl;
  if (s.serverUrl) link.href = s.serverUrl;
}

async function update(patch: Partial<Settings>): Promise<void> {
  await saveSettings(patch);
  await render();
}

async function toggle(site: SiteId, conversationId: string, exclude: boolean): Promise<void> {
  await send({ type: exclude ? "exclude" : "include", site, conversationId });
  await render();
}

$("flush").onclick = async () => {
  await send({ type: "flush" });
  await render();
};
$("options").onclick = () => void chrome.runtime.openOptionsPage();
void render();
