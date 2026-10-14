import { describe, expect, it } from "vitest";
import { HL_END, HL_START, qs, splitSnippet, toIso } from "./util";

describe("splitSnippet", () => {
  it("separates highlighted terms", () => {
    expect(splitSnippet(`a ${HL_START}b${HL_END} c`)).toEqual([
      { text: "a ", hit: false }, { text: "b", hit: true }, { text: " c", hit: false },
    ]);
  });
  it("never treats content as markup", () => {
    expect(splitSnippet("<script>x</script>")).toEqual([{ text: "<script>x</script>", hit: false }]);
  });
});

describe("qs", () => {
  it("drops empty values and encodes", () => {
    expect(qs({ q: "a b", x: "", y: undefined, n: 0, f: false, t: true })).toBe("?q=a+b&n=0&t=true");
    expect(qs({})).toBe("");
  });
});

describe("toIso", () => {
  it("handles empty and invalid input", () => {
    expect(toIso("")).toBeUndefined();
    expect(toIso("garbage")).toBeUndefined();
    expect(toIso("2026-01-02")).toMatch(/^2026-01-0[12]T/);
  });
});
