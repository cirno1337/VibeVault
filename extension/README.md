# VibeVault browser extension

Manifest V3 extension (Chromium: Brave, Chrome, Edge) that archives your own **claude.ai** (chat and Claude Code on the web) and
**chatgpt.com** conversations to your VibeVault server. Spec and design: [docs/collectors/browser-extension.md](../docs/collectors/browser-extension.md).

## Build

```sh
cd extension
npm install
npm run build      # type-check + bundle into dist/
npm test           # Vitest: adapters (HTML fixtures), normalizer, redactor, queue, policy
```

## Install (Brave)

1. `brave://extensions` → enable **Developer mode** → **Load unpacked** → choose `extension/dist`.
2. The settings page opens from the popup (**Settings**). Fill in:
   * **VibeVault URL**: e.g. `https://my-server.example-tailnet.ts.net:8787`
   * **Ingest token**: an *ingest* token from the server's `VIBEVAULT_INGEST_TOKENS` (e.g. the `browser` one), never the admin token
   * **Device label**: e.g. `Brave (PC)`
3. **Save** → allow access to the server when Brave asks (needed to send anything) → **Test connection**.
4. Open a conversation on claude.ai or chatgpt.com; the popup shows "Collecting … (N messages seen)" and the
   queue drains after the answer finishes.

After `npm run build`, press the reload icon on the extension card. Content scripts in already-open tabs keep
running the old code until the tab is reloaded.

## When a site changes its layout

The popup says **adapter outdated** and nothing is collected. Save the page, scrub it into a fixture
([test/fixtures/README.md](test/fixtures/README.md)), add a test, and fix the selectors in
`src/adapters/<site>.ts`. Those are the only files that know the DOM.
