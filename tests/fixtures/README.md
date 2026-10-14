# Fixtures

`claude_code_synthetic.jsonl` is **synthetic**: hand-written in the transcript shape observed in
practice, not captured from a real session. Passing these tests proves the parser handles that
shape; it does not prove compatibility with every Claude Code version. Re-validate against real
transcripts (see docs/collectors/claude-code.md) before relying on it.

The fixture contains the placeholder `__FAKE_GITHUB_TOKEN__`; tests substitute a runtime-built fake token so no token-shaped string is committed.
