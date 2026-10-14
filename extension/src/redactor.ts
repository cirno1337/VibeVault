/**
 * Port of src/vibevault/redaction.py (same rule names, patterns and placeholder handling), applied in the
 * browser before anything is queued. Both implementations are checked against the shared vectors in
 * tests/fixtures/redaction_vectors.json.
 *
 * Best-effort pattern matching, not a guarantee (docs/privacy.md); the server re-scans everything anyway.
 * Known difference: JS `\w`/`\b` are ASCII-only, Python's are Unicode-aware.
 */

interface Rule {
  name: string;
  pattern: RegExp; // must have the "g" and "d" flags
  group: number; // which capture group holds the secret (0 = whole match)
}

const r = (name: string, pattern: string, group = 0, flags = ""): Rule => ({
  name,
  pattern: new RegExp(pattern, "gd" + flags),
  group,
});

export const DEFAULT_RULES: readonly Rule[] = [
  r(
    "private_key",
    String.raw`-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|$)`,
  ),
  r("anthropic_key", String.raw`\bsk-ant-[A-Za-z0-9_\-]{16,}`),
  r("openai_key", String.raw`\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}`),
  r("github_token", String.raw`\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})`),
  r("gitlab_token", String.raw`\bglpat-[A-Za-z0-9_\-]{20,}`),
  r("aws_access_key", String.raw`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`),
  r("google_api_key", String.raw`\bAIza[0-9A-Za-z_\-]{35}\b`),
  r("groq_key", String.raw`\bgsk_[A-Za-z0-9]{20,}`),
  r("slack_token", String.raw`\bxox[abprs]-[A-Za-z0-9\-]{10,}`),
  r("stripe_key", String.raw`\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}`),
  r("jwt", String.raw`\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}`),
  r("bearer_token", String.raw`\bbearer\s+([A-Za-z0-9._~+/\-]{16,}=*)`, 1, "i"),
  r("url_credentials", String.raw`\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:([^\s/@]{3,})@`, 1, "i"),
  r(
    "secret_assignment",
    String.raw`\b[\w.\-]*(?:password|passwd|pwd|secret|api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token|token|credential)s?\b` +
      String.raw`\s*[:=]\s*["']?([^\s"'` + "`" + String.raw`,;&}\]]{6,})`,
    1,
    "i",
  ),
];

const SENSITIVE_KEYS = new RegExp(
  String.raw`^(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|password|passwd|` +
    String.raw`secret|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|private[_-]?key)$`,
  "i",
);

const PLACEHOLDER = new RegExp(
  String.raw`^(?:\*+|x{3,}|<[^>]*>|\$\{?[A-Z_]+\}?|\$[A-Za-z_]+|\[REDACTED[^\]]*\]|your[_\-].*|changeme|change-me|example|none|null|true|false)$`,
  "i",
);

const marker = (rule: string) => `[REDACTED:${rule}]`;

export class RedactionReport {
  readonly counts: Record<string, number> = {};

  add(rule: string): void {
    this.counts[rule] = (this.counts[rule] ?? 0) + 1;
  }

  get total(): number {
    return Object.values(this.counts).reduce((a, b) => a + b, 0);
  }

  /** The EventIn `redaction` block: rule names and counts only, never matched text. */
  toEventField(): { status: "none" | "redacted"; count: number; rules: Record<string, number> } {
    return { status: this.total ? "redacted" : "none", count: this.total, rules: { ...this.counts } };
  }
}

export function redactText(text: string, report = new RedactionReport(), rules = DEFAULT_RULES): string {
  for (const rule of rules) {
    rule.pattern.lastIndex = 0;
    let out = "";
    let last = 0;
    for (const m of text.matchAll(rule.pattern)) {
      const secret = m[rule.group];
      if (secret === undefined || PLACEHOLDER.test(secret)) continue;
      report.add(rule.name);
      const [start, end] = m.indices![rule.group]!;
      // Only the secret's span is replaced; the rest of the match (e.g. "password=") is kept.
      out += text.slice(last, start) + marker(rule.name);
      last = end;
    }
    text = out + text.slice(last);
  }
  return text;
}

/** Recursively redact strings in JSON-like data; values under sensitive keys are always redacted. */
export function redactObj<T>(obj: T, report = new RedactionReport()): T {
  if (typeof obj === "string") return redactText(obj, report) as T;
  if (Array.isArray(obj)) return obj.map((v) => redactObj(v, report)) as T;
  if (obj && typeof obj === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj)) {
      if (SENSITIVE_KEYS.test(k) && typeof v === "string" && v && !PLACEHOLDER.test(v)) {
        report.add("sensitive_key");
        out[k] = marker("sensitive_key");
      } else {
        out[k] = redactObj(v, report);
      }
    }
    return out as T;
  }
  return obj;
}
