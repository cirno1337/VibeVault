import type { TestReply } from "./messages";
import { loadSettings, normalizeServerUrl, saveSettings, SITE_LABELS, type Settings } from "./settings";
import type { SiteId } from "./adapters/types";
import { $, renderSiteSwitches, send } from "./ui";

const status = (text: string, cls = "muted") => {
  const el = $("saveStatus");
  el.textContent = text;
  el.className = cls;
};

async function render(): Promise<void> {
  const s = await loadSettings();
  $<HTMLInputElement>("serverUrl").value = s.serverUrl;
  $<HTMLInputElement>("token").placeholder = s.token ? "saved (leave empty to keep)" : "paste the ingest token";
  $<HTMLInputElement>("deviceLabel").value = s.deviceLabel;
  $<HTMLInputElement>("deviceId").value = s.deviceId;
  renderToggles(s);
  renderExcluded(s);
}

function renderToggles(s: Settings): void {
  const global = $<HTMLInputElement>("global");
  global.checked = s.globallyEnabled;
  global.onchange = () => void patch({ globallyEnabled: global.checked });
  renderSiteSwitches($("sites"), s, (siteEnabled) => void patch({ siteEnabled }));
  const incognito = $<HTMLInputElement>("incognito");
  incognito.checked = s.allowIncognito;
  incognito.onchange = () => void patch({ allowIncognito: incognito.checked });
}

function renderExcluded(s: Settings): void {
  const list = $("excluded");
  list.replaceChildren();
  if (!s.excluded.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "None.";
    list.append(li);
  }
  for (const key of s.excluded) {
    const [site, id] = key.split(/:(.*)/s) as [SiteId, string];
    const li = document.createElement("li");
    const remove = document.createElement("button");
    remove.textContent = "Collect again";
    remove.onclick = async () => {
      await send({ type: "include", site, conversationId: id });
      await render();
    };
    li.append(`${SITE_LABELS[site] ?? site}: ${id}`, remove);
    list.append(li);
  }
}

async function patch(p: Partial<Settings>): Promise<void> {
  const s = await saveSettings(p);
  renderToggles(s);
}

$("save").onclick = async () => {
  try {
    const raw = $<HTMLInputElement>("serverUrl").value;
    const serverUrl = raw.trim() ? normalizeServerUrl(raw) : "";
    const deviceId = $<HTMLInputElement>("deviceId").value.trim();
    if (!/^[A-Za-z0-9_.-]{1,128}$/.test(deviceId)) throw new Error("Device id may only contain letters, digits, _ . -");
    // Must run inside the click handler (user gesture) for the permission prompt to appear.
    if (serverUrl && !(await chrome.permissions.request({ origins: [`${serverUrl}/*`] }))) {
      throw new Error("Access to the server was not granted; nothing can be sent without it.");
    }
    const token = $<HTMLInputElement>("token").value.trim();
    const previousUrl = (await loadSettings()).serverUrl;
    await saveSettings({
      serverUrl,
      deviceId,
      deviceLabel: $<HTMLInputElement>("deviceLabel").value.trim().slice(0, 200) || "Browser",
      ...(token ? { token } : {}),
    });
    $<HTMLInputElement>("token").value = "";
    await render();
    if (previousUrl && previousUrl !== serverUrl) {
      await send({ type: "serverChanged" });
      status("Saved. Server changed: conversations will be sent in full again as you open them.", "ok");
    } else {
      status("Saved.", "ok");
      await send({ type: "flush" });
    }
  } catch (e) {
    status(e instanceof Error ? e.message : String(e), "bad");
  }
};

$("test").onclick = async () => {
  status("Testing…");
  const r = await send<TestReply>({ type: "testConnection" });
  status(r.message, r.ok ? "ok" : "bad");
};

void render();
