"""Job handlers. All idempotent; all safe to re-run after a crash.

  digest_session      deterministic. Marks session events processed in bounded steps and writes a
                      factual digest (counts, tools, files). No AI involved.
  digest_orphans      same for events that have no session.
  summarize_session   AI (optional). See knowledge.py.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from . import jobs
from . import models as m
from .config import Settings
from .providers import ProviderRegistry


@dataclass
class JobContext:
    settings: Settings
    registry: ProviderRegistry
    now: Callable[[], datetime]
    monotonic: Callable[[], float] = time.monotonic


Handler = Callable[[JobContext, DbSession, m.Job], "jobs.Done | jobs.Continue | jobs.Defer"]


def _mark_processed(db: DbSession, ctx: JobContext, job: m.Job, where, label: str) -> tuple[int, bool]:
    """Mark pending events processed in bounded steps. Returns (processed_this_run, budget_hit)."""
    deadline = ctx.monotonic() + ctx.settings.job_step_budget_seconds
    total = 0
    while True:
        seqs = db.scalars(
            select(m.Event.seq).where(where, m.Event.processing_status == "pending").order_by(m.Event.seq).limit(ctx.settings.job_batch_size)
        ).all()
        if not seqs:
            return total, False
        db.execute(update(m.Event).where(m.Event.seq.in_(seqs)).values(processing_status="processed"))
        job.checkpoint = {**job.checkpoint, label: job.checkpoint.get(label, 0) + len(seqs), "last_seq": seqs[-1]}
        db.commit()
        total += len(seqs)
        jobs.heartbeat(db, job, ctx.settings.job_lease_seconds, ctx.now())
        if ctx.monotonic() > deadline:
            return total, True


def build_digest(db: DbSession, sess: m.Session) -> dict[str, Any]:
    ev = m.Event
    by_type = dict(db.execute(select(ev.event_type, func.count()).where(ev.session_id == sess.id, ev.is_current).group_by(ev.event_type)).all())
    by_actor = dict(db.execute(select(ev.actor, func.count()).where(ev.session_id == sess.id, ev.is_current).group_by(ev.actor)).all())
    tools = Counter(
        dict(db.execute(select(m.ToolExecution.tool_name, func.count()).where(m.ToolExecution.session_id == sess.id).group_by(m.ToolExecution.tool_name)).all())
    )
    tool_errors = db.scalar(select(func.count()).select_from(m.ToolExecution).where(m.ToolExecution.session_id == sess.id, m.ToolExecution.is_error.is_(True))) or 0
    files = db.execute(
        select(m.FileChange.path, m.FileChange.operation, func.count()).where(m.FileChange.session_id == sess.id).group_by(m.FileChange.path, m.FileChange.operation).limit(200)
    ).all()
    redactions = db.scalar(select(func.coalesce(func.sum(ev.redaction_count), 0)).where(ev.session_id == sess.id)) or 0
    return {
        "events_by_type": by_type,
        "events_by_actor": by_actor,
        "tools": dict(tools),
        "tool_errors": tool_errors,
        "files": [{"path": p, "operation": o, "count": c} for p, o, c in files],
        "redactions": redactions,
        "first_event_at": sess.started_at.isoformat() if sess.started_at else None,
        "last_event_at": sess.last_event_at.isoformat() if sess.last_event_at else None,
    }


def digest_session(ctx: JobContext, db: DbSession, job: m.Job):
    sess = db.get(m.Session, job.payload["session_id"])
    if sess is None:
        return jobs.Done({"skipped": "session_deleted"})
    _, budget_hit = _mark_processed(db, ctx, job, m.Event.session_id == sess.id, "processed")
    if budget_hit:
        return jobs.Continue(0)

    data = build_digest(db, sess)
    n_events = sum(data["events_by_type"].values())
    now = ctx.now()
    for old in db.scalars(select(m.DerivedItem).where(m.DerivedItem.session_id == sess.id, m.DerivedItem.kind == "digest", m.DerivedItem.state == "active")):
        old.state, old.superseded_at = "superseded", now
    bounds = db.scalars(
        select(m.Event.id).where(m.Event.session_id == sess.id, m.Event.is_current).order_by(m.Event.occurred_at, m.Event.seq)
    ).all()
    item = m.DerivedItem(
        kind="digest",
        origin="deterministic",
        epistemic_status="confirmed",  # computed directly from stored evidence, no inference
        state="active",
        title=f"{n_events} events, {sum(data['tools'].values())} tool executions",
        body="Deterministic statistics computed from stored events.",
        data=data,
        project_id=sess.project_id,
        session_id=sess.id,
        generation_id=str(uuid.uuid4()),
        job_id=job.id,
        created_at=now,
    )
    if bounds:
        item.evidence.append(m.DerivedEvidence(event_id=bounds[0], note="first event"))
        if len(bounds) > 1:
            item.evidence.append(m.DerivedEvidence(event_id=bounds[-1], note="last event"))
    db.add(item)
    db.commit()

    if ctx.settings.auto_enqueue_ai and ctx.registry.any_configured() and sess.status == "ended":
        jobs.enqueue(db, ctx.settings, "summarize_session", {"session_id": sess.id}, f"summarize_session:{sess.id}", now=now)
        db.commit()
    return jobs.Done({"events": n_events})


def digest_orphans(ctx: JobContext, db: DbSession, job: m.Job):
    _, budget_hit = _mark_processed(db, ctx, job, m.Event.session_id.is_(None), "processed")
    return jobs.Continue(0) if budget_hit else jobs.Done({"processed": job.checkpoint.get("processed", 0)})


def registry_of_handlers() -> dict[str, Handler]:
    from .classify import classify_sessions
    from .knowledge import summarize_session

    return {
        "classify_sessions": classify_sessions,
        "digest_session": digest_session,
        "digest_orphans": digest_orphans,
        "summarize_session": summarize_session,
    }
