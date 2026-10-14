export const HL_START = "";
export const HL_END = "";

/** Split an FTS snippet into plain and highlighted parts (markers come from the server). */
export function splitSnippet(s: string): { text: string; hit: boolean }[] {
  const out: { text: string; hit: boolean }[] = [];
  let hit = false;
  for (const part of s.split(new RegExp(`([${HL_START}${HL_END}])`))) {
    if (part === HL_START) hit = true;
    else if (part === HL_END) hit = false;
    else if (part) out.push({ text: part, hit });
  }
  return out;
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "" || v === false) continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

/** `<input type=datetime-local|date>` value → ISO string with UTC offset, or undefined. */
export function toIso(local: string): string | undefined {
  if (!local) return undefined;
  const d = new Date(local.length === 10 ? `${local}T00:00:00` : local);
  return isNaN(d.getTime()) ? undefined : d.toISOString();
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function shortId(id: string): string { return id.slice(0, 8); }
