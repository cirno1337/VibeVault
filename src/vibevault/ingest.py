"""Event ingestion: validation → redaction → idempotent persistence → follow-up jobs.

Semantics
  * Identity of an event is (source, device, external_event_id, revision). Re-sending the same
    identity with the same semantic content is a no-op ("duplicate").
  * Re-sending the same identity with *different* content is a "conflict": the original evidence
    is kept untouched. Collectors that observe an edit must send a higher `revision`.
  * Events may arrive in any order; sessions are created on demand; project/session are optional.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from . import models as m
from .config import Settings
from .redaction import RedactionReport, Redactor
from .schemas import BatchResult, EventIn, ItemResult, sanitize_validation_errors

EVENT_NAMESPACE = uuid.UUID("6f1c3a52-2b0e-5c1e-9a55-76696265766c")  # fixed; do not change


def event_uuid(source: str, device: str, external_event_id: str, revision: int) -> str:
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{source}\x1f{device}\x1f{external_event_id}\x1f{revision}"))


def semantic_hash(ev: EventIn) -> str:
    """Hash of the evidence itself (not of transport details such as redaction bookkeeping)."""
    body = {
        "t": ev.event_type,
        "a": ev.actor,
        "c": ev.content,
        "tool": ev.tool.model_dump(mode="json") if ev.tool else None,
        "fc": [f.model_dump(mode="json") for f in ev.file_changes],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _slugify(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:80] or "project"


def normalize_alias(key: str) -> str:
    return key.strip().rstrip("/").lower()


@dataclass
class _Ctx:
    db: DbSession
    settings: Settings
    redactor: Redactor
    principal: str
    sources: dict[str, m.Source] = field(default_factory=dict)
    devices: dict[tuple[int, str], m.SourceDevice] = field(default_factory=dict)
    sessions: dict[tuple[int, int, str], m.Session] = field(default_factory=dict)
    touched_sessions: set[int] = field(default_factory=set)


def _get_source(ctx: _Ctx, kind: str) -> m.Source:
    if kind not in ctx.sources:
        src = ctx.db.scalar(select(m.Source).where(m.Source.kind == kind))
        if src is None:
            src = m.Source(kind=kind)
            ctx.db.add(src)
            ctx.db.flush()
        ctx.sources[kind] = src
    return ctx.sources[kind]


def _get_device(ctx: _Ctx, src: m.Source, key: str, label: str | None, now: datetime) -> m.SourceDevice:
    k = (src.id, key)
    if k not in ctx.devices:
        dev = ctx.db.scalar(
            select(m.SourceDevice).where(m.SourceDevice.source_id == src.id, m.SourceDevice.device_key == key)
        )
        if dev is None:
            dev = m.SourceDevice(source_id=src.id, device_key=key, label=label, first_seen_at=now)
            ctx.db.add(dev)
            ctx.db.flush()
        ctx.devices[k] = dev
    dev = ctx.devices[k]
    dev.last_seen_at = now
    if label and not dev.label:
        dev.label = label
    return dev


# Directory names too generic to identify a project by name alone.
GENERIC_PROJECT_NAMES = frozenset(
    "src app apps lib libs docs doc scripts script test tests frontend backend server client api web ui "
    "deploy config configs components packages tmp temp build dist home desktop downloads".split()
)


def resolve_project(ctx_db: DbSession, settings: Settings, ref) -> tuple[m.Project | None, str | None]:
    """Return (project, hint). Unknown keys join a same-named project, else stay unresolved unless a
    name allows auto-creation. Either way the key becomes an alias, so later events resolve directly."""
    if ref is None:
        return None, None
    alias = normalize_alias(ref.key)
    row = ctx_db.scalar(select(m.ProjectAlias).where(m.ProjectAlias.alias == alias))
    if row is not None:
        return row.project, ref.key
    name = (ref.name or "").strip()
    if settings.match_projects_by_name and name and _slugify(name) not in GENERIC_PROJECT_NAMES:
        same = ctx_db.scalar(select(m.Project).where(func.lower(m.Project.name) == name.lower()).order_by(m.Project.id).limit(1))
        if same is not None:
            same.aliases.append(m.ProjectAlias(alias=alias))
            ctx_db.flush()
            return same, ref.key
    if settings.auto_create_projects and ref.name:
        slug = _slugify(ref.name)
        base, n = slug, 1
        while ctx_db.scalar(select(m.Project.id).where(m.Project.slug == slug)) is not None:
            n += 1
            slug = f"{base}-{n}"
        proj = m.Project(slug=slug, name=ref.name)
        proj.aliases.append(m.ProjectAlias(alias=alias))
        ctx_db.add(proj)
        ctx_db.flush()
        return proj, ref.key
    return None, ref.key


def _redact_event(ctx: _Ctx, ev: EventIn) -> tuple[EventIn, RedactionReport]:
    """Server-side defence in depth. Merges with whatever the collector already reported."""
    report = RedactionReport()
    for rule, n in ev.redaction.rules.items():
        report.counts[rule] += n
    if not ctx.settings.server_redaction:
        return ev, report
    rd = ctx.redactor
    server = RedactionReport()
    data = ev.model_dump(mode="python")
    data["content"] = rd.redact_text(ev.content, server)
    data["metadata"] = rd.redact_obj(ev.metadata, server)
    if ev.tool:
        data["tool"] = rd.redact_obj(data["tool"], server)
    data["file_changes"] = rd.redact_obj(data["file_changes"], server)
    if ev.raw_payload is not None:
        data["raw_payload"] = rd.redact_obj(ev.raw_payload, server)
    if ev.session_title:
        data["session_title"] = rd.redact_text(ev.session_title, server)
    report.merge(server)
    return EventIn.model_validate(data), report


def _upsert_session(ctx: _Ctx, ev: EventIn, src, dev, project, hint, now) -> m.Session:
    k = (src.id, dev.id, ev.external_session_id)
    sess = ctx.sessions.get(k)
    if sess is None:
        sess = ctx.db.scalar(
            select(m.Session).where(
                m.Session.source_id == src.id,
                m.Session.device_id == dev.id,
                m.Session.external_session_id == ev.external_session_id,
            )
        )
        if sess is None:
            sess = m.Session(
                source_id=src.id, device_id=dev.id, external_session_id=ev.external_session_id, created_at=now
            )
            ctx.db.add(sess)
            ctx.db.flush()
        ctx.sessions[k] = sess
    return sess


def _apply_to_session(sess: m.Session, ev: EventIn, project, hint, new_logical_event: bool) -> None:
    if project is not None and sess.project_id is None:
        sess.project_id = project.id
    if hint and not sess.project_hint:
        sess.project_hint = hint
    if ev.session_title and not sess.title:
        sess.title = ev.session_title
    t = ev.occurred_at
    if sess.started_at is None or t < sess.started_at:
        sess.started_at = t
    if sess.last_event_at is None or t > sess.last_event_at:
        sess.last_event_at = t
    if ev.event_type == "session_end":
        sess.status = "ended"
        if sess.ended_at is None or t > sess.ended_at:
            sess.ended_at = t
    if new_logical_event:  # edits (higher revisions) replace an event rather than add one
        sess.event_count = (sess.event_count or 0) + 1


def _upsert_tool_execution(ctx: _Ctx, ev: EventIn, src, dev, sess, event_id: str) -> None:
    tb = ev.tool
    assert tb is not None
    row = ctx.db.scalar(
        select(m.ToolExecution).where(
            m.ToolExecution.source_id == src.id,
            m.ToolExecution.device_id == dev.id,
            m.ToolExecution.tool_call_id == tb.tool_call_id,
        )
    )
    if row is None:
        row = m.ToolExecution(source_id=src.id, device_id=dev.id, tool_call_id=tb.tool_call_id)
        ctx.db.add(row)
    if sess is not None and row.session_id is None:
        row.session_id = sess.id
    for attr in ("name", "command", "input", "output", "exit_code", "is_error", "started_at", "finished_at"):
        val = getattr(tb, attr)
        if val is not None:
            setattr(row, "tool_name" if attr == "name" else attr, val)
    if ev.event_type == "tool_call":
        row.call_event_id = event_id
        row.started_at = row.started_at or ev.occurred_at
    elif ev.event_type == "tool_result":
        row.result_event_id = event_id
        row.finished_at = row.finished_at or ev.occurred_at


def _ingest_one(ctx: _Ctx, idx: int, ev: EventIn, now: datetime) -> ItemResult:
    db, s = ctx.db, ctx.settings
    base = dict(index=idx, external_event_id=ev.external_event_id)

    if len(ev.content.encode()) > s.max_event_content_bytes:
        return ItemResult(**base, status="rejected", reason="content_too_large")
    if len(json.dumps(ev.metadata, default=str)) > s.max_metadata_bytes:
        return ItemResult(**base, status="rejected", reason="metadata_too_large")
    if ev.raw_payload is not None and len(json.dumps(ev.raw_payload, default=str)) > s.max_raw_payload_bytes:
        return ItemResult(**base, status="rejected", reason="raw_payload_too_large")

    src = _get_source(ctx, ev.source)
    dev = _get_device(ctx, src, ev.device_id, ev.device_label, now)
    eid = event_uuid(ev.source, ev.device_id, ev.external_event_id, ev.revision)
    chash = semantic_hash(ev)

    existing = db.scalar(select(m.Event).where(m.Event.id == eid))
    if existing is not None:  # idempotent retry (checked before collection controls on purpose)
        status = "duplicate" if existing.content_hash == chash else "conflict"
        return ItemResult(**base, status=status, event_id=eid, reason=None if status == "duplicate" else "content_differs_use_new_revision")

    if not dev.collection_enabled:
        return ItemResult(**base, status="skipped", reason="device_collection_disabled")

    project, hint = resolve_project(db, s, ev.project)
    sess = None
    if ev.external_session_id:
        sess = _upsert_session(ctx, ev, src, dev, project, hint, now)
        if sess.excluded:
            return ItemResult(**base, status="skipped", reason="session_excluded")
        project = project or (sess.project if sess.project_id else None)
    if project is not None and not project.collection_enabled:
        return ItemResult(**base, status="skipped", reason="project_collection_disabled")

    ev_r, report = _redact_event(ctx, ev)

    # Edits: only the highest revision is "current".
    max_rev = db.scalar(
        select(func.max(m.Event.revision)).where(
            m.Event.source_id == src.id,
            m.Event.device_id == dev.id,
            m.Event.external_event_id == ev.external_event_id,
        )
    )
    is_current = max_rev is None or ev.revision > max_rev
    if is_current and max_rev is not None:
        db.execute(
            update(m.Event)
            .where(
                m.Event.source_id == src.id,
                m.Event.device_id == dev.id,
                m.Event.external_event_id == ev.external_event_id,
            )
            .values(is_current=False)
        )

    row = m.Event(
        id=eid,
        schema_version=ev_r.schema_version,
        source_id=src.id,
        device_id=dev.id,
        session_id=sess.id if sess else None,
        external_session_id=ev_r.external_session_id,
        external_event_id=ev_r.external_event_id,
        revision=ev_r.revision,
        is_current=is_current,
        project_id=project.id if project else None,
        project_hint=hint,
        event_type=ev_r.event_type,
        actor=ev_r.actor,
        occurred_at=ev_r.occurred_at,
        ingested_at=now,
        ingested_by=ctx.principal,
        sequence=ev_r.sequence,
        content=ev_r.content,
        content_hash=chash,
        event_metadata=ev_r.metadata,
        parent_external_event_id=ev_r.parent_external_event_id,
        tool_call_id=ev_r.tool.tool_call_id if ev_r.tool else None,
        redaction_status=(
            "redacted"
            if report.total
            else "none" if ctx.settings.server_redaction else ev.redaction.status
        ),
        redaction_count=report.total,
        redaction_rules=report.as_dict(),
        processing_status="pending",
        raw_payload=ev_r.raw_payload,
        raw_payload_ref=ev_r.raw_payload_ref,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # concurrent retry won the race
        winner = db.scalar(select(m.Event).where(m.Event.id == eid))
        if winner is not None:
            st = "duplicate" if winner.content_hash == chash else "conflict"
            return ItemResult(**base, status=st, event_id=eid)
        raise

    if sess is not None:
        _apply_to_session(sess, ev_r, project, hint, new_logical_event=max_rev is None)
        ctx.touched_sessions.add(sess.id)
    if ev_r.tool is not None and is_current:
        _upsert_tool_execution(ctx, ev_r, src, dev, sess, eid)
    for fc in ev_r.file_changes:
        db.add(
            m.FileChange(
                event_id=eid,
                session_id=sess.id if sess else None,
                path=fc.path,
                operation=fc.operation,
                old_path=fc.old_path,
                diff=fc.diff,
                before_hash=fc.before_hash,
                after_hash=fc.after_hash,
            )
        )
    return ItemResult(**base, status="created", event_id=eid)


def ingest_batch(
    db: DbSession,
    settings: Settings,
    redactor: Redactor,
    items: list[dict[str, Any]],
    principal: str,
    now: datetime,
) -> BatchResult:
    from . import jobs  # local import: jobs depends on models only, avoid cycles at import time

    ctx = _Ctx(db=db, settings=settings, redactor=redactor, principal=principal)
    results: list[ItemResult] = []
    for idx, raw in enumerate(items):
        try:
            ev = EventIn.model_validate(raw)
        except ValidationError as e:
            ext = raw.get("external_event_id") if isinstance(raw, dict) else None
            results.append(
                ItemResult(
                    index=idx,
                    external_event_id=ext if isinstance(ext, str) else None,
                    status="rejected",
                    reason="validation_error",
                    errors=sanitize_validation_errors(e.errors()),
                )
            )
            continue
        with db.begin_nested():
            results.append(_ingest_one(ctx, idx, ev, now))

    for sid in sorted(ctx.touched_sessions):
        jobs.enqueue(
            db,
            settings,
            kind="digest_session",
            payload={"session_id": sid},
            dedupe_key=f"digest_session:{sid}",
            now=now,
        )

    counts = {k: sum(1 for r in results if r.status == k) for k in ("created", "duplicate", "conflict", "rejected", "skipped")}
    db.add(
        m.AuditLog(
            ts=now,
            actor=principal,
            action="ingest_batch",
            target=None,
            detail={"received": len(items), **counts},
        )
    )
    db.commit()
    return BatchResult(received=len(items), results=results, **counts)
