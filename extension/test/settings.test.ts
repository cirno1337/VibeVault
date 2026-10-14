import { describe, expect, it } from "vitest";
import { decide, defaultSettings, normalizeServerUrl, policyOf } from "../src/settings";

describe("collection policy", () => {
  it("allows by default, honours global, per-site and per-conversation switches", () => {
    const s = defaultSettings();
    expect(decide(policyOf(s), "claude_web", "c1")).toBe("allowed");
    expect(decide(policyOf({ ...s, globallyEnabled: false }), "claude_web", "c1")).toBe("disabled");
    expect(decide(policyOf({ ...s, siteEnabled: { ...s.siteEnabled, chatgpt_web: false } }), "chatgpt_web", "c1")).toBe("disabled");
    expect(decide(policyOf({ ...s, siteEnabled: { ...s.siteEnabled, chatgpt_web: false } }), "claude_web", "c1")).toBe("allowed");
    const ex = { ...s, excluded: ["claude_web:c1"] };
    expect(decide(policyOf(ex), "claude_web", "c1")).toBe("excluded");
    expect(decide(policyOf(ex), "chatgpt_web", "c1")).toBe("allowed"); // exclusion is per site
  });

  it("generates a server-valid device id and keeps private windows off", () => {
    const s = defaultSettings();
    expect(s.deviceId).toMatch(/^[A-Za-z0-9_.-]+$/);
    expect(s.allowIncognito).toBe(false);
  });

  it("accepts https origins (and http only for localhost)", () => {
    expect(normalizeServerUrl(" https://my-server.example-tailnet.ts.net:8787/some/path ")).toBe("https://my-server.example-tailnet.ts.net:8787");
    expect(normalizeServerUrl("http://localhost:8787")).toBe("http://localhost:8787");
    expect(() => normalizeServerUrl("http://my-server:8787")).toThrow();
    expect(() => normalizeServerUrl("not a url")).toThrow();
  });
});
