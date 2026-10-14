import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { RedactionReport, redactObj, redactText } from "../src/redactor";

interface Vector {
  input: string[];
  output: string;
  rules: Record<string, number>;
}
const vectors: Vector[] = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/redaction_vectors.json", import.meta.url), "utf8"),
);

describe("redactor (port of redaction.py)", () => {
  it.each(vectors.map((v) => [v.input.join("").slice(0, 40), v] as const))("matches Python: %s", (_, v) => {
    const rep = new RedactionReport();
    expect(redactText(v.input.join(""), rep)).toBe(v.output);
    expect(rep.counts).toEqual(v.rules);
  });

  it("redacts sensitive keys in objects and reports counts only", () => {
    const rep = new RedactionReport();
    const out = redactObj({ headers: { Authorization: "abc", cookie: "${COOKIE}" }, list: ["password=hunter2hunter2"] }, rep);
    expect(out).toEqual({
      headers: { Authorization: "[REDACTED:sensitive_key]", cookie: "${COOKIE}" },
      list: ["password=[REDACTED:secret_assignment]"],
    });
    expect(rep.toEventField()).toEqual({ status: "redacted", count: 2, rules: { sensitive_key: 1, secret_assignment: 1 } });
    expect(JSON.stringify(rep.toEventField())).not.toContain("hunter2");
  });

  it("is stateless across calls (global regexes reset)", () => {
    const key = "sk-ant-" + "x".repeat(20);
    expect(redactText(`a ${key}`)).toBe("a [REDACTED:anthropic_key]");
    expect(redactText(`${key}`)).toBe("[REDACTED:anthropic_key]");
  });
});
