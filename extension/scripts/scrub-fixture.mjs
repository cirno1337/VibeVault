// Turns a raw page save (private conversation) into a committable adapter fixture: keeps the DOM
// structure, data-* attributes and timestamps the adapters rely on; replaces every letter/digit of
// content with a deterministic pseudo-word of the same length; drops sidebar, scripts, styles, links.
//
//   node scripts/scrub-fixture.mjs test/fixtures/_raw/<page>.html test/fixtures/<site>/real-<case>-<yyyymm>.html
//
// Review the output before committing (grep it for a few words from the conversation). Only the UI
// phrases in KEEP_TEXT survive verbatim, so tests can assert they never end up in message text.
import { readFileSync, writeFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

const [input, output] = process.argv.slice(2);
if (!input || !output) {
  console.error("usage: scrub-fixture.mjs <raw.html> <out.html>");
  process.exit(2);
}

const doc = new JSDOM(readFileSync(input, "utf8"), { virtualConsole: new VirtualConsole() }).window.document;

const DROP = "script,style,link,meta,noscript,template,iframe,svg,nav,aside,[data-testid='sidebar']";
const KEEP_CLASS = /^(?:whitespace-pre.*|sr-only|katex.*|language-.+|lang-.+|turn-action-controls|code-block__code)$/;
const KEEP_ATTR = /^(?:data-.*|id|role|datetime|aria-hidden|hidden|contenteditable|class|style|aria-label|alt)$/;
const KEEP_TEXT = new Set(["Show more", "Message collapsed", "Searched the web", "Copy", "Retry", "Edit", "Kopiuj", "Opcje reklamy", "Przetwarzano przez 10s"]);
// Screen-reader headings repeat the message ("You said: <text>"): keep the label, scramble the rest.
const LABEL_PREFIX = /^(\s*(?:You said|Claude responded|Powiedziałeś\(-aś\)|ChatGPT powiedział|ChatGPT said):\s*)([\s\S]*)$/;

function hash(s) {
  let h = 2166136261;
  for (const c of s) h = Math.imul(h ^ c.codePointAt(0), 16777619) >>> 0;
  return h;
}

/** Same length, same case pattern, same punctuation/whitespace; deterministic per word. */
function scramble(text) {
  return text.replace(/[\p{L}\p{N}]+/gu, (word) => {
    let h = hash(word);
    let out = "";
    for (const ch of word) {
      h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0;
      if (/\p{N}/u.test(ch)) out += String(h % 10);
      else {
        const c = String.fromCharCode(97 + (h % 26));
        out += ch !== ch.toLowerCase() ? c.toUpperCase() : c;
      }
    }
    return out;
  });
}

const languages = new Set(
  Array.from(doc.querySelectorAll("code[class*='language-']")).flatMap((c) =>
    Array.from(c.classList).filter((x) => x.startsWith("language-")).map((x) => x.slice(9).toLowerCase()),
  ),
);

const roots = Array.from(doc.querySelectorAll("main"));
if (!roots.length) throw new Error("no <main> in the page");
for (const root of roots) {
  root.querySelectorAll(DROP).forEach((el) => el.remove());
  for (const el of [root, ...root.querySelectorAll("*")]) {
    for (const a of Array.from(el.attributes)) {
      if (!KEEP_ATTR.test(a.name)) el.removeAttribute(a.name);
      else if (a.name === "class") {
        const kept = Array.from(el.classList).filter((c) => KEEP_CLASS.test(c));
        kept.length ? el.setAttribute("class", kept.join(" ")) : el.removeAttribute("class");
      } else if (a.name === "style") {
        const ws = /white-space\s*:\s*[^;]+/i.exec(a.value);
        ws ? el.setAttribute("style", ws[0]) : el.removeAttribute("style");
      } else if (a.name === "aria-label" || a.name === "alt") el.setAttribute(a.name, scramble(a.value));
      // Ids and flags stay; anything that looks like text (e.g. data-markdown-copy-text) does not.
      else if (a.name.startsWith("data-") && (/\s/.test(a.value.trim()) || a.value.length > 80)) el.setAttribute(a.name, scramble(a.value));
    }
  }
  const walker = doc.createTreeWalker(root, 4 /* SHOW_TEXT */);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.nodeValue ?? "";
    const label = LABEL_PREFIX.exec(t);
    if (label) n.nodeValue = label[1] + scramble(label[2]);
    else if (!KEEP_TEXT.has(t.trim()) && !languages.has(t.trim().toLowerCase())) n.nodeValue = scramble(t);
  }
}

const title = doc.title.replace(/^(.*?)((?: [-|—·] (?:Claude|ChatGPT))?)$/, (_, t, suffix) => scramble(t) + suffix);
const html = `<!doctype html>
<!-- Scrubbed with scripts/scrub-fixture.mjs: structure is real, text is pseudo-random. -->
<html><head><meta charset="utf-8"><title>${title.replace(/</g, "&lt;")}</title></head>
<body>
${roots.map((r) => r.outerHTML).join("\n")}
</body></html>
`;
writeFileSync(output, html);
console.log(`${output}: ${(html.length / 1024).toFixed(0)} KiB, ${roots.length} <main>`);
