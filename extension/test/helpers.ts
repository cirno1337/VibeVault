import { readFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

export function fixture(path: string, url: string): { doc: Document; url: URL } {
  const html = readFileSync(new URL(`./fixtures/${path}`, import.meta.url), "utf8");
  return { doc: new JSDOM(html, { url, virtualConsole: new VirtualConsole() }).window.document, url: new URL(url) };
}
