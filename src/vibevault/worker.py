"""Background worker: claims due jobs, runs handlers with budgets, records outcomes."""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from . import jobs
from . import models as m
from .config import Settings
from .handlers import JobContext, registry_of_handlers
from .providers import ProviderRegistry

log = logging.getLogger("vibevault.worker")


class Worker:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[DbSession],
        registry: ProviderRegistry,
        clock: Callable[[], datetime] = m.utcnow,
        worker_id: str | None = None,
    ):
        self.settings = settings
        self.sf = session_factory
        self.registry = registry
        self.clock = clock
        self.worker_id = worker_id or f"{socket.gethostname()}:{id(self) & 0xFFFF:x}"
        self.handlers = registry_of_handlers()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sweep = 0.0

    # ---- one tick -------------------------------------------------------------
    def sweep(self, db: DbSession) -> int:
        """Safety net: make sure every session/orphan with unprocessed events has a job."""
        now = self.clock()
        n = 0
        sids = db.scalars(
            select(m.Event.session_id).where(m.Event.processing_status == "pending", m.Event.session_id.is_not(None)).distinct().limit(500)
        ).all()
        for sid in sids:
            _, created = jobs.enqueue(db, self.settings, "digest_session", {"session_id": sid}, f"digest_session:{sid}", now=now)
            n += created
        orphan = db.scalar(select(m.Event.seq).where(m.Event.processing_status == "pending", m.Event.session_id.is_(None)).limit(1))
        if orphan is not None:
            _, created = jobs.enqueue(db, self.settings, "digest_orphans", {}, "digest_orphans", now=now)
            n += created
        n += self.schedule_classification(db, now)
        db.commit()
        return n

    def schedule_classification(self, db: DbSession, now: datetime) -> int:
        """Once per local day at/after VIBEVAULT_CLASSIFY_AT (catches up if the server was off then)."""
        at = self.settings.classify_at
        if not at or not self.registry.any_configured():
            return 0
        tz = ZoneInfo(self.settings.timezone)
        local = now.astimezone(tz)
        hh, mm = (int(x) for x in at.split(":"))
        slot = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if local < slot:
            return 0
        slot_utc = slot.astimezone(UTC)
        already = db.scalar(select(m.Job.id).where(m.Job.kind == "classify_sessions", m.Job.created_at >= slot_utc).limit(1))
        if already is not None:
            return 0
        _, created = jobs.enqueue(db, self.settings, "classify_sessions", {"scheduled_for": slot.isoformat()}, "classify_sessions", now=now)
        return int(created)

    def run_once(self, sweep: bool = True) -> int:
        """Recover stale jobs, optionally sweep, then run up to jobs_per_tick jobs. Returns jobs run."""
        ran = 0
        with self.sf() as db:
            jobs.recover_stale(db, self.settings, self.clock())
            if sweep:
                self.sweep(db)
            for _ in range(self.settings.jobs_per_tick):
                job = jobs.claim(db, self.worker_id, self.settings.job_lease_seconds, self.clock())
                if job is None:
                    break
                self._execute(db, job)
                ran += 1
        return ran

    def _execute(self, db: DbSession, job: m.Job) -> None:
        ctx = JobContext(settings=self.settings, registry=self.registry, now=self.clock)
        handler = self.handlers.get(job.kind)
        now = self.clock()
        if handler is None:
            jobs.fail(db, job, self.settings, f"unknown job kind '{job.kind}'", now, permanent=True)
            return
        try:
            outcome = handler(ctx, db, job)
        except jobs.PermanentError as e:
            db.rollback()
            jobs.fail(db, job, self.settings, e, self.clock(), permanent=True)
            log.warning("job failed permanently", extra={"job_id": job.id, "kind": job.kind})
            return
        except Exception as e:  # noqa: BLE001 - any handler failure is retried with backoff
            db.rollback()
            jobs.fail(db, job, self.settings, e, self.clock())
            log.warning("job attempt failed", extra={"job_id": job.id, "kind": job.kind, "attempt": job.attempts, "exc_type": type(e).__name__})
            return
        now = self.clock()
        if isinstance(outcome, jobs.Done):
            jobs.complete(db, job, outcome.result, now)
        elif isinstance(outcome, jobs.Continue):
            jobs.continue_later(db, job, outcome.delay_seconds, now)
        elif isinstance(outcome, jobs.Defer):
            job.status_note = outcome.note[:500]
            jobs.continue_later(db, job, outcome.delay_seconds, now)
        else:  # pragma: no cover
            jobs.fail(db, job, self.settings, "handler returned invalid outcome", now, permanent=True)

    # ---- thread ---------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vibevault-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        import time

        while not self._stop.is_set():
            try:
                sweep = time.monotonic() - self._last_sweep >= self.settings.sweep_interval_seconds
                if sweep:
                    self._last_sweep = time.monotonic()
                self.run_once(sweep=sweep)
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")
            self._stop.wait(self.settings.worker_poll_seconds)
