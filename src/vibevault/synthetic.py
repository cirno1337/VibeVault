"""Small, clearly synthetic dataset for development and UI testing.

Everything is tagged source="synthetic", device "synthetic-dev" and metadata.synthetic=true.
It is generated, not captured, and must never be mixed into real archives (remove it with
`vibevault purge-synthetic`). Includes: a bash failure → fix, an out-of-order tool result, an
edit (revision 2), a file change, an unresolved-project session, and a fake secret that the
redactor must catch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

SOURCE = "synthetic"
DEVICE = "synthetic-dev"


def _ev(sess, n, typ, actor, t, content, **kw):
    d = {
        "source": SOURCE, "device_id": DEVICE, "device_label": "Synthetic dataset",
        "external_session_id": sess, "external_event_id": f"{sess}-{n}", "event_type": typ, "actor": actor,
        "occurred_at": t.isoformat(), "sequence": n, "content": content,
        "metadata": {"synthetic": True},
    }
    d.update(kw)
    return d


def build(now: datetime | None = None) -> list[dict]:
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    out: list[dict] = []

    # Session 1: debugging a failing migration in project "demo-api"
    s, t0, proj = "synth-sess-001", now - timedelta(days=2), {"key": "/home/demo/code/demo-api", "name": "demo-api"}
    steps = [
        ("session_start", "system", "Session started", {}),
        ("message", "user", "The alembic upgrade fails with 'no such column: owner_id'. Can you find out why?", {}),
        ("message", "assistant", "Let me look at the migration history and run it to see the exact failure.", {}),
        ("tool_call", "assistant", "alembic upgrade head", {"tool": {"tool_call_id": "toolu_syn_1", "name": "Bash", "command": "alembic upgrade head"}}),
        ("tool_result", "tool", "sqlalchemy.exc.OperationalError: no such column: owner_id", {"parent_external_event_id": f"{s}-4", "tool": {"tool_call_id": "toolu_syn_1", "output": "sqlalchemy.exc.OperationalError: no such column: owner_id", "exit_code": 1, "is_error": True}}),
        ("message", "assistant", "Migration 0004 references owner_id before 0005 adds it. I will reorder by making 0004 depend on 0005 is wrong; the correct fix is to add the column in 0004.", {}),
        ("tool_call", "assistant", "Edit migrations/versions/0004_add_index.py", {"tool": {"tool_call_id": "toolu_syn_2", "name": "Edit"}}),
        ("file_change", "assistant", "Edit migrations/versions/0004_add_index.py", {"file_changes": [{"path": "migrations/versions/0004_add_index.py", "operation": "edit", "diff": "-    op.create_index('ix_owner', 'items', ['owner_id'])\n+    op.add_column('items', sa.Column('owner_id', sa.Integer()))\n+    op.create_index('ix_owner', 'items', ['owner_id'])"}]}),
        ("tool_call", "assistant", "alembic upgrade head && pytest -q", {"tool": {"tool_call_id": "toolu_syn_3", "name": "Bash", "command": "alembic upgrade head && pytest -q"}}),
        ("tool_result", "tool", "INFO Running upgrade -> 0005\n42 passed in 3.1s", {"parent_external_event_id": f"{s}-9", "tool": {"tool_call_id": "toolu_syn_3", "output": "INFO Running upgrade -> 0005\n42 passed in 3.1s", "exit_code": 0, "is_error": False}}),
        ("message", "assistant", "Fixed: migration 0004 now adds the owner_id column before indexing it. All 42 tests pass.", {}),
        ("session_end", "system", "Session ended", {}),
    ]
    for i, (typ, actor, content, extra) in enumerate(steps, 1):
        out.append(_ev(s, i, typ, actor, t0 + timedelta(minutes=i), content, project=proj, session_title="Fix failing alembic migration" if i == 1 else None, **extra))
    # deliver tool_result 5 before tool_call 4 to exercise out-of-order handling
    a, b = next(i for i, e in enumerate(out) if e["external_event_id"] == f"{s}-4"), next(i for i, e in enumerate(out) if e["external_event_id"] == f"{s}-5")
    out[a], out[b] = out[b], out[a]
    # an edited message (revision 2 of event 2)
    out.append(_ev(s, 2, "message", "user", t0 + timedelta(minutes=2), "The alembic upgrade fails with 'no such column: owner_id' on a fresh database. Can you find out why?", project=proj, revision=2))

    # Session 2: a web chat about design choices (decision with rationale), no project known
    s2, t1 = "synth-chat-001", now - timedelta(days=1, hours=3)
    chat = [
        ("user", "Should I use SQLite or PostgreSQL for a single-user archive of chat transcripts?"),
        ("assistant", "SQLite is the pragmatic choice: one file, trivial backups, FTS5 gives full-text search. Revisit PostgreSQL if you need concurrent writers or multi-user access."),
        ("user", "Decision: SQLite with WAL mode. I will keep SQL portable so a PostgreSQL migration stays possible."),
        ("assistant", "Sounds good. Open question: how long to retain raw tool output?"),
    ]
    for i, (actor, content) in enumerate(chat, 1):
        e = _ev(s2, i, "message", actor, t1 + timedelta(minutes=2 * i), content)
        e["source"] = "synthetic"
        e["project"] = {"key": "https://example.invalid/chat/synth-chat-001"}  # hint only: stays unresolved
        out.append(e)

    # Session 3: secret handling (fake key) so redaction is visible in the UI
    s3, t2 = "synth-sess-002", now - timedelta(hours=5)
    out.append(_ev(s3, 1, "message", "user", t2, "My config has API_KEY=synthetic_fake_key_1234567890abcd and password: hunter2hunter2 in .env, is that okay?", project={"key": "/home/demo/code/demo-api", "name": "demo-api"}, session_title="Secrets in config"))
    out.append(_ev(s3, 2, "message", "assistant", t2 + timedelta(minutes=1), "Move secrets to environment variables or a secrets manager and keep .env out of version control.", project={"key": "/home/demo/code/demo-api"}))
    return out
