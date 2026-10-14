import type { ParsedMessage } from "./types";

/**
 * DOM -> plain text (light Markdown) for rendered chat messages. Shared by the site adapters; it knows
 * generic HTML only, never site selectors (adapters pass those in via `skip`).
 *
 * Works in jsdom (no innerText / layout), so fixture tests and the browser produce the same output.
 */

export interface ExtractOptions {
  /** Extra CSS selector for elements to drop entirely (site chrome inside a message: labels, toolbars). */
  skip?: string;
}

/** Never part of a message's visible text, or never ours to read (inputs = the prompt draft). */
const ALWAYS_SKIP =
  "script,style,noscript,template,svg,button,textarea,input,select,form,[contenteditable='true'],.sr-only,[hidden]";

const BLOCK = new Set([
  "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DD", "DETAILS", "DIV", "DL", "DT", "FIELDSET", "FIGCAPTION",
  "FIGURE", "FOOTER", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HR", "LI", "MAIN", "NAV", "OL", "P",
  "SECTION", "SUMMARY", "TABLE", "TBODY", "THEAD", "TFOOT", "TR", "UL",
]);

class Out {
  private parts: string[] = [];
  private tail = ""; // last few chars, to decide on separators without joining
  private line = ""; // current (last) line

  push(s: string): void {
    if (!s) return;
    this.parts.push(s);
    this.tail = (this.tail + s).slice(-2);
    const nl = s.lastIndexOf("\n");
    this.line = nl === -1 ? this.line + s : s.slice(nl + 1);
  }

  /** Make the output end with at least `n` newlines (no-op at the very start or right after a list marker). */
  newlines(n: number): void {
    if (!this.parts.length || /^\s*(?:- |\d+\. )$/.test(this.line)) return;
    const have = this.tail.endsWith("\n\n") ? 2 : this.tail.endsWith("\n") ? 1 : 0;
    if (have < n) this.push("\n".repeat(n - have));
  }

  /** Removes the last line if it is exactly `text` (a code block's language label rendered above it). */
  dropTrailingLine(text: string): void {
    if (!text) return;
    const s = this.toString();
    const m = /(^|\n)([^\n]*)\n*$/.exec(s);
    if (!m || m[2].trim().toLowerCase() !== text.toLowerCase()) return;
    const kept = s.slice(0, m.index + m[1].length);
    this.parts = kept ? [kept] : [];
    this.tail = kept.slice(-2);
    this.line = kept.slice(kept.lastIndexOf("\n") + 1);
  }

  endsWithSpace(): boolean {
    return !this.parts.length || /\s$/.test(this.tail);
  }

  toString(): string {
    return this.parts.join("");
  }
}

function preservesWhitespace(el: Element): boolean {
  const style = el.getAttribute("style") ?? "";
  if (/white-space\s*:\s*pre/i.test(style)) return true;
  return Array.from(el.classList).some((c) => /^whitespace-pre/.test(c));
}

function codeLanguage(pre: Element): string {
  for (const el of [pre, ...Array.from(pre.querySelectorAll("code"))]) {
    for (const c of Array.from(el.classList)) {
      const m = /^(?:language|lang)-([\w+#.-]+)$/.exec(c);
      if (m) return m[1];
    }
  }
  return "";
}

function walk(node: Node, out: Out, opts: ExtractOptions, pre: boolean, listDepth: number): void {
  if (node.nodeType === 3 /* TEXT_NODE */) {
    const raw = node.nodeValue ?? "";
    if (pre) return out.push(raw);
    const text = raw.replace(/\s+/g, " ");
    if (text === " ") {
      if (!out.endsWithSpace()) out.push(" ");
      return;
    }
    out.push(out.endsWithSpace() ? text.replace(/^ /, "") : text);
    return;
  }
  if (node.nodeType !== 1 /* ELEMENT_NODE */) return;
  const el = node as Element;
  if (el.matches(ALWAYS_SKIP) || (opts.skip && el.matches(opts.skip))) return;
  if (el.getAttribute("aria-hidden") === "true" && !el.closest(".katex")) return;

  const tag = el.tagName;
  if (el.classList.contains("katex")) {
    const tex = el.querySelector("annotation[encoding='application/x-tex']")?.textContent?.trim();
    const display = !!el.closest(".katex-display");
    if (tex) {
      if (display) out.newlines(2);
      out.push(display ? `$$${tex}$$` : `$${tex}$`);
      if (display) out.newlines(2);
      return;
    }
  }
  switch (tag) {
    case "BR":
      out.push("\n");
      return;
    case "IMG": {
      const alt = el.getAttribute("alt")?.trim();
      out.push(alt ? `[image: ${alt}]` : "[image]");
      return;
    }
    case "HR":
      out.newlines(2);
      out.push("---");
      out.newlines(2);
      return;
    case "PRE": {
      const code = el.querySelector("code") ?? el;
      const body = (code.textContent ?? "").replace(/\n+$/, "");
      const lang = codeLanguage(el);
      out.dropTrailingLine(lang);
      out.newlines(2);
      out.push("```" + lang + "\n" + body + "\n```");
      out.newlines(2);
      return;
    }
    case "CODE":
      if (!pre) {
        out.push("`" + (el.textContent ?? "") + "`");
        return;
      }
      break;
  }

  const block = BLOCK.has(tag);
  const heading = /^H[1-6]$/.exec(tag);
  if (block) out.newlines(tag === "P" || heading || tag === "TABLE" || tag === "BLOCKQUOTE" ? 2 : 1);
  if (heading) out.push("#".repeat(Number(tag[1])) + " ");
  if (tag === "LI") {
    const parent = el.parentElement;
    const ordered = parent?.tagName === "OL";
    const n = ordered ? Array.from(parent!.children).filter((c) => c.tagName === "LI").indexOf(el) + 1 : 0;
    out.push("  ".repeat(Math.max(0, listDepth - 1)) + (ordered ? `${n}. ` : "- "));
  }
  if ((tag === "TD" || tag === "TH") && el.previousElementSibling) out.push(" | ");

  const innerPre = pre || preservesWhitespace(el);
  const depth = tag === "UL" || tag === "OL" ? listDepth + 1 : listDepth;
  if (tag === "BLOCKQUOTE") {
    const inner = extractText(el, opts);
    out.push(inner.split("\n").map((l) => (l ? `> ${l}` : ">")).join("\n"));
  } else {
    for (const child of Array.from(el.childNodes)) walk(child, out, opts, innerPre, depth);
  }
  if (block) out.newlines(tag === "P" || heading || tag === "TABLE" || tag === "BLOCKQUOTE" ? 2 : 1);
}

/** Visible text of `root`, with code blocks fenced, lists/headings marked, images as placeholders. */
export function extractText(root: Element, opts: ExtractOptions = {}): string {
  const out = new Out();
  for (const child of Array.from(root.childNodes)) walk(child, out, opts, preservesWhitespace(root), 0);
  return out
    .toString()
    .split("\n")
    .map((l) => l.replace(/[ \t]+$/, ""))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Elements matching `selector` that are not nested inside another match (document order). */
export function outermost(root: ParentNode, selector: string): Element[] {
  const all = Array.from(root.querySelectorAll(selector));
  return all.filter((el) => !all.some((other) => other !== el && other.contains(el)));
}

/** "Title - Site" / "Site" -> "Title" / undefined. */
export function titleFromDocument(doc: Document, siteNames: readonly string[]): string | undefined {
  let t = doc.title.trim();
  for (const name of siteNames) {
    if (t === name) return undefined;
    for (const sep of [" - ", " | ", " — ", " · "]) if (t.endsWith(sep + name)) t = t.slice(0, -(sep + name).length);
  }
  return t ? t.slice(0, 500) : undefined;
}

/**
 * Detects the "2 / 3" branch switcher that both sites render next to edited/regenerated messages.
 * Only counts a counter that sits next to a button (the prev/next arrows) and is outside `content`,
 * so a "1/2" inside the message text itself is never mistaken for one.
 */
export function branchFrom(container: Element | null, content?: Element): { position: number; total: number } | undefined {
  if (!container) return undefined;
  for (const el of Array.from(container.querySelectorAll("span,div"))) {
    if (el.children.length || (content && content.contains(el))) continue;
    const m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(el.textContent ?? "");
    if (!m || !el.parentElement?.querySelector("button")) continue;
    const [position, total] = [Number(m[1]), Number(m[2])];
    if (total > 1 && position >= 1 && position <= total) return { position, total };
  }
  return undefined;
}

/** A `datetime` attribute value if it is a timezone-aware ISO timestamp (the server rejects naive ones). */
export function isoTime(value: string | null | undefined): string | undefined {
  return value && !Number.isNaN(Date.parse(value)) && /(?:[zZ]|[+-]\d\d:?\d\d)$/.test(value) ? value : undefined;
}

/**
 * Sites show times on only some messages. Give the others their neighbour's (previous, else next) so a
 * conversation never mixes page times with first-seen times; `sequence` still orders equal times.
 */
export function fillTimestamps(messages: ParsedMessage[]): void {
  let last: string | undefined;
  for (const m of messages) last = m.timestamp ??= last;
  for (let i = messages.length - 2; i >= 0; i--) messages[i].timestamp ??= messages[i + 1].timestamp;
  for (const m of messages) if (m.timestamp === undefined) delete m.timestamp;
}
