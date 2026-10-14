import { describe, expect, it } from "vitest";
import { chatgptAdapter } from "../src/adapters/chatgpt";
import { claudeAdapter, claudeCodeAdapter } from "../src/adapters/claude";
import { adapterForUrl } from "../src/adapters";
import { fixture } from "./helpers";

const CLAUDE = "https://claude.ai/chat/0b6c7e1e-3f1a-4c55-9a8e-2d9f2c1b7a10";
const GPT = "https://chatgpt.com/c/68e9d3a1-1b2c-8000-9abc-0123456789ab";

const parse = (a: typeof claudeAdapter, path: string, url: string) => {
  const f = fixture(path, url);
  return a.parse(f.doc, f.url);
};

describe("URL handling", () => {
  it.each([
    [claudeAdapter, "https://claude.ai/chat/0b6c7e1e-3f1a-4c55-9a8e-2d9f2c1b7a10", "0b6c7e1e-3f1a-4c55-9a8e-2d9f2c1b7a10"],
    [claudeAdapter, "https://claude.ai/new", null],
    [claudeAdapter, "https://claude.ai/recents", null],
    [claudeAdapter, "https://claude.ai/project/0b6c7e1e-3f1a-4c55", null],
    [claudeAdapter, "https://claude.ai/share/0b6c7e1e-3f1a-4c55", null],
    [claudeAdapter, "https://claude.ai/code/session_019w1SapwV4xQHRt4EEfDQWy", null],
    [claudeCodeAdapter, "https://claude.ai/code/session_019w1SapwV4xQHRt4EEfDQWy", "session_019w1SapwV4xQHRt4EEfDQWy"],
    [claudeCodeAdapter, "https://claude.ai/code", null],
    [claudeCodeAdapter, "https://claude.ai/code/artifacts", null],
    [claudeCodeAdapter, "https://claude.ai/chat/0b6c7e1e-3f1a-4c55-9a8e-2d9f2c1b7a10", null],
    [chatgptAdapter, "https://chatgpt.com/c/68e9d3a1-1b2c-8000-9abc-0123456789ab?model=x", "68e9d3a1-1b2c-8000-9abc-0123456789ab"],
    [chatgptAdapter, "https://chatgpt.com/g/g-p-abc123-project/c/68e9d3a1-1b2c-8000", "68e9d3a1-1b2c-8000"],
    [chatgptAdapter, "https://chatgpt.com/g/g-abc-mygpt/c/68e9d3a1-1b2c-8000", "68e9d3a1-1b2c-8000"],
    [chatgptAdapter, "https://chatgpt.com/", null],
    [chatgptAdapter, "https://chatgpt.com/share/68e9d3a1-1b2c-8000", null],
    [chatgptAdapter, "https://chatgpt.com/g/g-abc-mygpt", null],
    [chatgptAdapter, "https://evil.example/c/68e9d3a1-1b2c-8000", null],
  ])("%#: %s", (adapter, url, id) => {
    expect(adapter.conversationIdFromUrl(new URL(url))).toBe(id);
    expect(adapter.isConversationUrl(new URL(url))).toBe(id !== null);
  });

  it("maps URLs to adapters (claude.ai hosts two)", () => {
    expect(adapterForUrl(new URL(CLAUDE))).toBe(claudeAdapter);
    expect(adapterForUrl(new URL("https://claude.ai/code/session_019w1SapwV4xQHRt4EEfDQWy"))).toBe(claudeCodeAdapter);
    expect(adapterForUrl(new URL("https://claude.ai/recents"))).toBe(claudeAdapter); // idle page: first for host
    expect(adapterForUrl(new URL(GPT))).toBe(chatgptAdapter);
    expect(adapterForUrl(new URL("https://example.com/c/68e9d3a1-1b2c-8000"))).toBeUndefined();
  });

  it("does not parse non-conversation pages", () => {
    expect(parse(claudeAdapter, "claude/synthetic-plain.html", "https://claude.ai/recents")).toBeNull();
    expect(parse(chatgptAdapter, "chatgpt/synthetic-plain.html", "https://chatgpt.com/")).toBeNull();
  });
});

describe("claude.ai adapter", () => {
  it("parses a plain chat (markdown structure, user newlines, ids, time; no UI chrome)", () => {
    const c = parse(claudeAdapter, "claude/synthetic-plain.html", CLAUDE)!;
    expect(c).toMatchObject({ site: "claude_web", conversationId: "0b6c7e1e-3f1a-4c55-9a8e-2d9f2c1b7a10", url: CLAUDE, title: "Sorting a list in Python", streaming: false });
    expect(c.messages.map((m) => [m.role, m.index, m.sourceMessageId, m.timestamp])).toEqual([
      ["user", 0, "u-0001", "2026-10-11T10:00:00.000Z"],
      ["assistant", 1, "u-0001-hub-reply", "2026-10-11T10:00:00.000Z"], // inherited: replies show no time
    ]);
    expect(c.messages[0].text).toBe("How do I sort a list of dicts by a key?\nAlso descending, please.");
    expect(c.messages[1].text).toBe(
      [
        "Use `sorted` with a key function:",
        "",
        '1. Ascending: pass `key=lambda d: d["age"]`.',
        "",
        "2. Descending: add `reverse=True`.",
        "",
        "- Works on any iterable",
        "- Returns a new list",
        "  - the original is unchanged",
        "",
        "> Tip: use operator.itemgetter for speed.",
        "",
        "An image: [image: diagram]",
      ].join("\n"),
    );
  });

  it("fences code blocks once (language label dropped), keeps math as TeX", () => {
    const c = parse(claudeAdapter, "claude/synthetic-code.html", CLAUDE)!;
    expect(c.messages[1].text).toBe(
      "Here you go:\n\n```python\ndef fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n```\n\nComplexity is $O(2^n)$.",
    );
  });

  it("flags streaming", () => {
    expect(parse(claudeAdapter, "claude/synthetic-streaming.html", CLAUDE)!.streaming).toBe(true);
  });

  it("reports the branch of an edited prompt, ignoring '1/2' in message text", () => {
    const c = parse(claudeAdapter, "claude/synthetic-edited.html", CLAUDE)!;
    expect(c.messages[0].branch).toEqual({ position: 2, total: 2 });
    expect(c.messages[0].text).toBe("Explain UDP (edited, was TCP)");
    expect(c.messages[1].branch).toBeUndefined();
  });

  it("turns attachments into placeholders (never names/contents); skips tool-only replies", () => {
    const c = parse(claudeAdapter, "claude/synthetic-attachment.html", CLAUDE)!;
    expect(c.messages.map((m) => [m.index, m.role, m.sourceMessageId, m.text])).toEqual([
      [0, "user", "u-file", "[attachment]"],
      [1, "user", "u-both", "[attachment]\n\nSummarise these"],
      [2, "assistant", "u-both-hub-reply", "Summary."],
    ]);
    expect(JSON.stringify(c)).not.toMatch(/secret-report|notes\.txt|Ustalanie|Ran a command/);
  });

  it("returns no messages for an empty/unrecognised page (fails closed)", () => {
    const c = parse(claudeAdapter, "claude/synthetic-empty.html", CLAUDE)!;
    expect(c.messages).toEqual([]);
    expect(c.title).toBeUndefined();
  });
});

describe("claude.ai/code adapter (Claude Code on the web)", () => {
  const URL_ = "https://claude.ai/code/session_019w1SapwV4xQHRt4EEfDQWy";
  it("reads the whole reply around tool calls, the header title and the repository", () => {
    const c = parse(claudeCodeAdapter, "claude/synthetic-code-session.html", URL_)!;
    expect(c).toMatchObject({
      site: "claude_code_web",
      conversationId: "session_019w1SapwV4xQHRt4EEfDQWy",
      title: "Fix flaky ingest test",
      projectKey: "repo:VibeVault",
      projectName: "VibeVault",
      streaming: false,
    });
    expect(c.messages.map((m) => [m.index, m.role, m.sourceMessageId, m.timestamp])).toEqual([
      [0, "user", "msg_01USERAAAA", "2026-10-12T07:00:00.000Z"],
      [2, "assistant", "msg_01ASSTBBBB:2", "2026-10-12T07:00:00.000Z"],
      [4, "assistant", "msg_01ASSTBBBB:4", "2026-10-12T07:00:00.000Z"], // same turn key, own row → own id
    ]);
    expect(c.messages[1].text).toBe(
      "Let me look at the test first.\n\nThe race is in `ingest_batch`; I made the writer wait.\n\nAll tests pass now.",
    );
    expect(JSON.stringify(c)).not.toMatch(/Environment ready|toolu_|Edited|Read tests|DRAFT-NEVER-READ/);
  });

  it("the chat adapter ignores Claude Code pages and vice versa", () => {
    expect(parse(claudeAdapter, "claude/synthetic-code-session.html", URL_)).toBeNull();
    expect(parse(claudeCodeAdapter, "claude/synthetic-plain.html", CLAUDE)).toBeNull();
  });
});

describe("chatgpt.com adapter", () => {
  it("parses a plain chat with message ids and times, without screen-reader labels or toolbars", () => {
    const c = parse(chatgptAdapter, "chatgpt/synthetic-plain.html", GPT)!;
    expect(c).toMatchObject({ site: "chatgpt_web", conversationId: "68e9d3a1-1b2c-8000-9abc-0123456789ab", title: "Sorting dicts", streaming: false });
    expect(c.messages.map((m) => [m.role, m.sourceMessageId, m.timestamp])).toEqual([
      ["user", "aaa1-user", "2026-10-11T14:28:00.792Z"],
      ["assistant", "bbb2-asst", "2026-10-11T14:28:00.792Z"],
    ]);
    expect(c.messages[0].text).toBe("How do I sort a list of dicts?\nDescending too.");
    expect(c.messages[1].text).toBe(
      "### Answer\n\nUse `sorted(xs, key=...)`.\n\n- Ascending by default\n\n- Pass reverse=True for descending\n\narg | meaning\nkey | sort key",
    );
  });

  it("takes code from <code>, not the header/copy button", () => {
    const c = parse(chatgptAdapter, "chatgpt/synthetic-code.html", GPT)!;
    expect(c.messages[1].text).toBe(
      "Sure:\n\n```javascript\nconst fib = (n) => (n < 2 ? n : fib(n - 1) + fib(n - 2));\nconsole.log(fib(10));\n```\n\nDone.",
    );
  });

  it("flags streaming (turn state / stop button) and treats the default title as none", () => {
    const c = parse(chatgptAdapter, "chatgpt/synthetic-streaming.html", GPT)!;
    expect(c.streaming).toBe(true);
    expect(c.title).toBeUndefined();
  });

  it("keeps the regenerated answer's own id and branch", () => {
    const c = parse(chatgptAdapter, "chatgpt/synthetic-regenerated.html", GPT)!;
    expect(c.messages[1]).toMatchObject({ sourceMessageId: "a2-regen", branch: { position: 2, total: 2 }, text: "Second try haiku" });
  });

  it("returns no messages for an empty page", () => {
    expect(parse(chatgptAdapter, "chatgpt/synthetic-empty.html", GPT)!.messages).toEqual([]);
  });
});

// Scrubbed saves of real pages (2026-10). Text is pseudo-random, so these assert structure.
describe("real page layouts (2026-10)", () => {
  const UI = /Show more|Message collapsed|Searched the web|You said|Claude responded|Powiedziałeś|ChatGPT powiedział|Kopiuj|Opcje reklamy|Przetwarzano|Copy|Retry|Edit/;
  const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/;
  const checkCommon = (c: NonNullable<ReturnType<typeof parse>>) => {
    expect(c.streaming).toBe(false);
    expect(c.messages.map((m) => m.role)).toEqual(c.messages.map((_, i) => (i % 2 ? "assistant" : "user")));
    expect(new Set(c.messages.map((m) => m.sourceMessageId)).size).toBe(c.messages.length);
    for (const m of c.messages) {
      expect(m.text).not.toMatch(UI);
      expect(m.text.trim()).toBe(m.text);
      expect(m.timestamp).toMatch(ISO);
    }
    const times = c.messages.map((m) => Date.parse(m.timestamp!));
    expect(times).toEqual([...times].sort((a, b) => a - b));
  };

  it("claude.ai: rows with web search; reply ids derive from the prompt's turn key; times back-filled", () => {
    const c = parse(claudeAdapter, "claude/real-websearch-202610.html", CLAUDE)!;
    checkCommon(c);
    expect(c.messages.map((m) => m.index)).toEqual([0, 1, 2, 3, 4, 5]);
    for (let i = 1; i < 6; i += 2) expect(c.messages[i].sourceMessageId).toBe(`${c.messages[i - 1].sourceMessageId}-hub-reply`);
    expect(c.messages.slice(0, 5).map((m) => m.timestamp)).toEqual(Array(5).fill("2026-09-21T11:28:06.925Z"));
    expect(c.messages[5].timestamp).toBe("2026-09-21T11:28:30.964Z");
    expect(c.title).not.toMatch(/Claude$/);
  });

  it("claude.ai: virtualised transcript keeps absolute positions; code block fenced with its language", () => {
    const c = parse(claudeAdapter, "claude/real-code-virtualized-202610.html", CLAUDE)!;
    checkCommon(c);
    expect(c.messages.map((m) => m.index)).toEqual([12, 13, 14, 15]);
    expect(c.messages[1].text).toMatch(/\n```powershell\n[\s\S]{1000,}\n```/);
    expect(c.messages[0].text.split("\n").length).toBeGreaterThan(10); // collapsed long prompt is read in full
  });

  const A = "https://chatgpt.com/c/6acbb921-6778-83ed-b156-97c3b827fb0a";
  const B = "https://chatgpt.com/c/6acb9cde-2878-83eb-83be-513dcf84f0eb";

  it("chatgpt.com: two conversations mounted at once; only the one in the URL is read", () => {
    const a = parse(chatgptAdapter, "chatgpt/real-two-mains-a-202610.html", A)!;
    checkCommon(a);
    expect(a.messages).toHaveLength(10);
    const b = parse(chatgptAdapter, "chatgpt/real-two-mains-b-202610.html", B)!;
    checkCommon(b);
    expect(b.messages).toHaveLength(8);
    const idsA = new Set(a.messages.map((m) => m.sourceMessageId));
    expect(b.messages.filter((m) => idsA.has(m.sourceMessageId))).toEqual([]);
    expect(b.messages.some((m) => /^#+ /m.test(m.text) && /^- /m.test(m.text))).toBe(true); // headings + lists
  });

  it("chatgpt.com: a conversation that is not on the page yields nothing (fails closed)", () => {
    const other = "https://chatgpt.com/c/00000000-0000-0000-0000-000000000000";
    expect(parse(chatgptAdapter, "chatgpt/real-two-mains-a-202610.html", other)!.messages).toEqual([]);
  });
});

describe("never reads the prompt draft", () => {
  it.each([
    [claudeAdapter, "claude/synthetic-plain.html", CLAUDE],
    [claudeAdapter, "claude/synthetic-code.html", CLAUDE],
    [chatgptAdapter, "chatgpt/synthetic-plain.html", GPT],
    [chatgptAdapter, "chatgpt/synthetic-code.html", GPT],
    [claudeAdapter, "claude/real-websearch-202610.html", CLAUDE],
    [chatgptAdapter, "chatgpt/real-two-mains-b-202610.html", "https://chatgpt.com/c/6acb9cde-2878-83eb-83be-513dcf84f0eb"],
  ])("%#", (adapter, path, url) => {
    expect(JSON.stringify(parse(adapter, path, url))).not.toContain("DRAFT-NEVER-READ");
  });
});
