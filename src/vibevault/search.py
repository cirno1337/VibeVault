"""Full-text search over events using SQLite FTS5 (external-content index kept in sync by triggers)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session as DbSession

from .models import UTCDateTime

HL_START, HL_END = "", ""  # private-use markers; the UI turns them into <mark>
_TOKEN = re.compile(r'"([^"]+)"|(\w+)', re.UNICODE)


class EmptyQuery(ValueError):
    pass


def build_match(q: str) -> str:
    """Turn user input into a safe FTS5 expression: AND of quoted terms; last bare term is a prefix.

    "exact phrase" is honoured; FTS operators/punctuation in input are neutralised.
    """
    parts: list[tuple[str, bool]] = []
    for m_ in _TOKEN.finditer(q):
        if m_.group(1):
            words = " ".join(re.findall(r"\w+", m_.group(1)))
            if words:
                parts.append((words, True))
        else:
            parts.append((m_.group(2), False))
    if not parts:
        raise EmptyQuery("query has no searchable terms")
    out = []
    for i, (term, phrase) in enumerate(parts):
        expr = f'"{term}"'
        if not phrase and i == len(parts) - 1 and len(term) >= 2:
            expr += "*"
        out.append(expr)
    return " ".join(out)


def search_events(
    db: DbSession,
    q: str,
    *,
    project_id: int | None = None,
    source: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_superseded: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    match = build_match(q)
    where = ["events_fts MATCH :match"]
    params: dict[str, Any] = {"match": match, "limit": limit, "offset": offset}
    if not include_superseded:
        where.append("e.is_current = 1")
    if project_id is not None:
        where.append("e.project_id = :project_id")
        params["project_id"] = project_id
    if source:
        where.append("s.kind = :source")
        params["source"] = source
    if event_type:
        where.append("e.event_type = :event_type")
        params["event_type"] = event_type
    if actor:
        where.append("e.actor = :actor")
        params["actor"] = actor
    binds = []
    if since:
        where.append("e.occurred_at >= :since")
        binds.append(bindparam("since", value=since, type_=UTCDateTime()))
    if until:
        where.append("e.occurred_at <= :until")
        binds.append(bindparam("until", value=until, type_=UTCDateTime()))
    clause = " AND ".join(where)
    frm = "events_fts JOIN events e ON e.seq = events_fts.rowid JOIN sources s ON s.id = e.source_id"

    total = db.execute(text(f"SELECT COUNT(*) FROM {frm} WHERE {clause}").bindparams(*binds), params).scalar_one()
    rows = db.execute(
        text(
            f"""SELECT e.id, e.session_id, e.project_id, e.event_type, e.actor, e.occurred_at,
                       s.kind AS source, bm25(events_fts) AS rank,
                       snippet(events_fts, 0, '{HL_START}', '{HL_END}', '…', 24) AS snippet
                FROM {frm} WHERE {clause} ORDER BY rank LIMIT :limit OFFSET :offset"""
        ).bindparams(*binds),
        params,
    ).mappings().all()
    return {"total": total, "match": match, "rows": [dict(r) for r in rows]}
