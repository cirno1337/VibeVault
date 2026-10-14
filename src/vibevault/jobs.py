"""Durable job queue on the application database.

Guarantees
  * Jobs live in SQLite; a restart loses nothing. `running` jobs whose lease expired are
    re-queued (`recover_stale`).
  * Retries use bounded exponential backoff; after `max_attempts` a job becomes `failed` and
    stays visible until an operator retries it.
  * Deduplication: at most one pending/running job per `dedupe_key` (partial unique index).
  * `Continue` (more work, progress checkpointed) and `Defer` (blocked, e.g. provider rate
    limited) do not consume an attempt.
  * Handlers must be idempotent: a job can run again after a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from . import models as m
from .config import Settings
from .redaction import default_redactor

log = logging.getLogger("vibevault.jobs")

MAX_ERROR_CHARS = 2000


# ---- handler outcomes -------------------------------------------------------
@dataclass
class Done:
    result: dict[str, Any] | None = None


@dataclass
class Continue:
    """More work remains; checkpoint is already saved. Re-run soon."""

    delay_seconds: float = 0.0


@dataclass
class Defer:
    """Cannot make progress now (no provider, rate limit). Retry later, no attempt consumed."""

    delay_seconds: float
    note: str


class RetryableError(Exception):
    pass


class PermanentError(Exception):
    """Fail immediately without retrying."""


def backoff_seconds(attempts: int, base: float, cap: float) -> float:
    """attempts is the number of attempts already made (>=1)."""
    return min(cap, base * (2 ** max(0, attempts - 1)))


def sanitize_error(exc: BaseException | str) -> str:
    text_ = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    return default_redactor().redact_text(text_)[:MAX_ERROR_CHARS]


def enqueue(
    db: DbSession,
    settings: Settings,
    kind: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int | None = None,
    priority: int = 0,
    now: datetime | None = None,
) -> tuple[m.Job, bool]:
    """Create a job unless an active one with the same dedupe_key exists. Returns (job, created)."""
    now = now or m.utcnow()
    if dedupe_key:
        existing = db.scalar(
            select(m.Job).where(m.Job.dedupe_key == dedupe_key, m.Job.status.in_(("pending", "running")))
        )
        if existing is not None:
            return existing, False
    job = m.Job(
        kind=kind,
        payload=payload or {},
        dedupe_key=dedupe_key,
        run_after=run_after or now,
        max_attempts=max_attempts or settings.job_max_attempts,
        priority=priority,
        created_at=now,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(m.Job).where(m.Job.dedupe_key == dedupe_key, m.Job.status.in_(("pending", "running")))
        )
        if existing is None:
            raise
        return existing, False
    return job, True


def claim(db: DbSession, worker_id: str, lease_seconds: int, now: datetime) -> m.Job | None:
    """Atomically move the next due pending job to running."""
    row = db.execute(
        text(
            """
            UPDATE jobs SET status='running', locked_by=:w, attempts=attempts+1,
                   started_at=:now, lease_expires_at=:lease
            WHERE id = (SELECT id FROM jobs WHERE status='pending' AND run_after <= :now
                        ORDER BY priority DESC, run_after, id LIMIT 1)
              AND status='pending'
            RETURNING id
            """
        ).bindparams(
            _dt("now", now),
            _dt("lease", now + timedelta(seconds=lease_seconds)),
            w=worker_id,
        )
    ).first()
    db.commit()
    if row is None:
        return None
    db.expire_all()
    return db.get(m.Job, row[0])


def _dt(name: str, value: datetime):
    from sqlalchemy import bindparam

    return bindparam(name, value=value, type_=m.UTCDateTime())


def heartbeat(db: DbSession, job: m.Job, lease_seconds: int, now: datetime | None = None) -> None:
    job.lease_expires_at = (now or m.utcnow()) + timedelta(seconds=lease_seconds)
    db.commit()


def save_checkpoint(db: DbSession, job: m.Job, checkpoint: dict[str, Any]) -> None:
    job.checkpoint = dict(checkpoint)  # reassign so the JSON change is detected
    db.commit()


def complete(db: DbSession, job: m.Job, result: dict[str, Any] | None, now: datetime) -> None:
    job.status = "completed"
    job.result = result
    job.finished_at = now
    job.lease_expires_at = None
    job.locked_by = None
    job.status_note = None
    db.commit()


def continue_later(db: DbSession, job: m.Job, delay: float, now: datetime, consume_attempt: bool = False) -> None:
    job.status = "pending"
    job.run_after = now + timedelta(seconds=delay)
    job.lease_expires_at = None
    job.locked_by = None
    if not consume_attempt:
        job.attempts = max(0, job.attempts - 1)
    db.commit()


def fail(db: DbSession, job: m.Job, settings: Settings, exc: BaseException | str, now: datetime, permanent: bool = False) -> None:
    job.last_error = sanitize_error(exc)
    job.last_error_at = now
    job.lease_expires_at = None
    job.locked_by = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = "failed"
        job.finished_at = now
    else:
        job.status = "pending"
        job.run_after = now + timedelta(
            seconds=backoff_seconds(job.attempts, settings.job_backoff_base_seconds, settings.job_backoff_max_seconds)
        )
    db.commit()


def recover_stale(db: DbSession, settings: Settings, now: datetime) -> int:
    """Re-queue jobs whose worker vanished (lease expired). The aborted attempt still counts."""
    stale = db.scalars(select(m.Job).where(m.Job.status == "running", m.Job.lease_expires_at < now)).all()
    for job in stale:
        fail(db, job, settings, "worker lease expired (crash or restart); job re-queued", now)
    return len(stale)


def retry_failed(db: DbSession, job_id: int, now: datetime | None = None) -> m.Job | None:
    now = now or m.utcnow()
    job = db.get(m.Job, job_id)
    if job is None or job.status != "failed":
        return None
    active = (
        db.scalar(select(m.Job).where(m.Job.dedupe_key == job.dedupe_key, m.Job.status.in_(("pending", "running"))))
        if job.dedupe_key
        else None
    )
    if active is not None:
        return None
    job.status = "pending"
    job.attempts = 0
    job.run_after = now
    job.finished_at = None
    job.status_note = "manually retried"
    db.commit()
    return job


def counts_by_status(db: DbSession) -> dict[str, int]:
    out = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for status, n in db.execute(text("SELECT status, COUNT(*) FROM jobs GROUP BY status")):
        out[status] = n
    return out


def mark_job_status_note(db: DbSession, job: m.Job, note: str | None) -> None:
    db.execute(update(m.Job).where(m.Job.id == job.id).values(status_note=note))
    db.commit()
