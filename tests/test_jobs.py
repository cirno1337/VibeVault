from datetime import timedelta

import pytest
from conftest import T0, ev, make_settings, post
from sqlalchemy import select, update

from vibevault import jobs
from vibevault import models as m
from vibevault.worker import Worker

NOW = T0 + timedelta(days=1)


@pytest.fixture
def sf(client):
    return client.app_ref.state.session_factory


@pytest.fixture
def cfg(settings):
    return settings


def mk_worker(client, clock):
    app = client.app_ref
    return Worker(app.state.settings, app.state.session_factory, app.state.registry, clock=clock, worker_id="t")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def register(w, kind, fn):
    w.handlers[kind] = fn


class TestQueueBasics:
    def test_dedupe_active_only(self, sf, cfg):
        with sf() as db:
            j1, c1 = jobs.enqueue(db, cfg, "k", {}, dedupe_key="a", now=NOW)
            j2, c2 = jobs.enqueue(db, cfg, "k", {}, dedupe_key="a", now=NOW)
            db.commit()
            assert (c1, c2, j1.id == j2.id) == (True, False, True)
            jobs.complete(db, j1, None, NOW)
            j3, c3 = jobs.enqueue(db, cfg, "k", {}, dedupe_key="a", now=NOW)
            assert c3 and j3.id != j1.id

    def test_backoff_is_bounded_exponential(self):
        assert [jobs.backoff_seconds(n, 30, 300) for n in range(1, 7)] == [30, 60, 120, 240, 300, 300]

    def test_ingest_enqueues_one_digest_job_per_session(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1), ev(2), ev(1, session="s2"))
        post(client, ingest_h, ev(3))
        r = client.get("/api/v1/jobs", headers=admin).json()
        assert r["total"] == 2 and {j["dedupe_key"] for j in r["items"]} == {"digest_session:1", "digest_session:2"}


class TestRetries:
    def test_retry_with_backoff_then_fail_then_manual_retry(self, client, sf, cfg, admin):
        clock = Clock(NOW)
        w = mk_worker(client, clock)
        calls = []

        def boom(ctx, db, job):
            calls.append(clock())
            raise RuntimeError("kaput")

        register(w, "flaky", boom)
        with sf() as db:
            job, _ = jobs.enqueue(db, cfg, "flaky", {}, max_attempts=3, now=NOW)
            db.commit()
            jid = job.id

        def state():
            with sf() as db:
                j = db.get(m.Job, jid)
                return j.status, j.attempts, j.run_after

        w.run_once(sweep=False)
        st, attempts, run_after = state()
        assert (st, attempts) == ("pending", 1) and run_after == NOW + timedelta(seconds=cfg.job_backoff_base_seconds)
        assert w.run_once(sweep=False) == 0  # not due yet
        clock.advance(seconds=cfg.job_backoff_base_seconds)
        w.run_once(sweep=False)
        assert state()[:2] == ("pending", 2)
        assert state()[2] == clock() + timedelta(seconds=2 * cfg.job_backoff_base_seconds)
        clock.advance(hours=1)
        w.run_once(sweep=False)
        assert state()[:2] == ("failed", 3) and len(calls) == 3

        failed = client.get("/api/v1/jobs?status=failed", headers=admin).json()
        assert failed["total"] == 1 and "RuntimeError: kaput" in failed["items"][0]["last_error"]
        assert client.post(f"/api/v1/jobs/{jid}/retry", headers=admin).status_code == 200
        assert state()[:2] == ("pending", 0)
        assert client.post(f"/api/v1/jobs/{jid}/retry", headers=admin).status_code == 409  # no longer failed

    def test_permanent_error_fails_immediately(self, client, sf, cfg):
        clock = Clock(NOW)
        w = mk_worker(client, clock)
        register(w, "bad", lambda c, d, j: (_ for _ in ()).throw(jobs.PermanentError("never")))
        with sf() as db:
            jobs.enqueue(db, cfg, "bad", {}, now=NOW)
            db.commit()
        w.run_once(sweep=False)
        with sf() as db:
            j = db.scalars(select(m.Job)).one()
            assert (j.status, j.attempts) == ("failed", 1)

    def test_error_text_is_redacted_and_truncated(self, client, sf, cfg):
        clock = Clock(NOW)
        w = mk_worker(client, clock)
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

        def leak(ctx, db, job):
            raise RuntimeError(f"auth failed with {secret} " + "x" * 5000)

        register(w, "leak", leak)
        with sf() as db:
            jobs.enqueue(db, cfg, "leak", {}, now=NOW)
            db.commit()
        w.run_once(sweep=False)
        with sf() as db:
            err = db.scalars(select(m.Job)).one().last_error
            assert secret not in err and len(err) <= jobs.MAX_ERROR_CHARS

    def test_unknown_kind_fails_permanently(self, client, sf, cfg):
        w = mk_worker(client, Clock(NOW))
        with sf() as db:
            jobs.enqueue(db, cfg, "nope", {}, now=NOW)
            db.commit()
        w.run_once(sweep=False)
        with sf() as db:
            assert db.scalars(select(m.Job)).one().status == "failed"


class TestRecovery:
    def test_crashed_job_is_requeued_and_resumes_from_checkpoint(self, client, sf, cfg):
        clock = Clock(NOW)
        seen = []

        def resumable(ctx, db, job):
            start = job.checkpoint.get("done", 0)
            seen.append(start)
            for i in range(start, 5):
                job.checkpoint = {"done": i + 1}
                db.commit()
                if i == 2 and len(seen) == 1:
                    raise SystemExit  # simulates process death mid-job (BaseException)
            return jobs.Done({"total": 5})

        with sf() as db:
            jobs.enqueue(db, cfg, "resumable", {}, now=NOW)
            db.commit()
        w1 = mk_worker(client, clock)
        register(w1, "resumable", resumable)
        with pytest.raises(SystemExit):
            w1.run_once(sweep=False)
        with sf() as db:
            j = db.scalars(select(m.Job)).one()
            assert j.status == "running" and j.checkpoint == {"done": 3}

        # "restart": a brand-new worker after the lease expired
        clock.advance(seconds=cfg.job_lease_seconds + 1)
        w2 = mk_worker(client, clock)
        register(w2, "resumable", resumable)
        w2.run_once(sweep=False)
        assert w2.run_once(sweep=False) == 0
        clock.advance(hours=1)
        w2.run_once(sweep=False)
        with sf() as db:
            j = db.scalars(select(m.Job)).one()
            assert j.status == "completed" and j.attempts == 2 and j.result == {"total": 5}
        assert seen == [0, 3]  # second run resumed at the checkpoint, not from scratch

    def test_live_lease_is_not_stolen(self, client, sf, cfg):
        with sf() as db:
            jobs.enqueue(db, cfg, "x", {}, now=NOW)
            db.commit()
            job = jobs.claim(db, "other", 300, NOW)
            assert job.status == "running"
            assert jobs.recover_stale(db, cfg, NOW + timedelta(seconds=10)) == 0
            assert jobs.claim(db, "me", 300, NOW) is None

    def test_pending_jobs_survive_app_restart(self, tmp_path):
        from conftest import ADMIN, INGEST
        from fastapi.testclient import TestClient

        from vibevault.app import create_app

        s = make_settings(tmp_path)
        with TestClient(create_app(s)) as c:
            post(c, {"Authorization": f"Bearer {INGEST}"}, ev(1))
        with TestClient(create_app(s)) as c2:  # new process, same database file
            r = c2.get("/api/v1/jobs?status=pending", headers={"Authorization": f"Bearer {ADMIN}"}).json()
            assert r["total"] == 1 and r["items"][0]["kind"] == "digest_session"


class TestBudgetsAndDigest:
    def test_batch_size_and_continue_with_checkpoint(self, tmp_path):
        from conftest import INGEST
        from fastapi.testclient import TestClient

        from vibevault.app import create_app

        s = make_settings(tmp_path, job_batch_size=10, job_step_budget_seconds=0.0, jobs_per_tick=1)
        app = create_app(s)
        with TestClient(app) as c:
            post(c, {"Authorization": f"Bearer {INGEST}"}, *[ev(i) for i in range(1, 26)])
            w = app.state.worker
            w.run_once(sweep=False)  # budget 0 → one batch of 10, then Continue
            with app.state.session_factory() as db:
                j = db.scalars(select(m.Job)).one()
                assert j.status == "pending" and j.checkpoint["processed"] == 10 and j.attempts == 0  # continue ≠ attempt
                assert db.scalars(select(m.Event.processing_status).where(m.Event.processing_status == "pending")).all().__len__() == 15
            for _ in range(5):
                w.run_once(sweep=False)
            with app.state.session_factory() as db:
                j = db.scalars(select(m.Job)).one()
                assert j.status == "completed"
                assert not db.scalars(select(m.Event).where(m.Event.processing_status == "pending")).all()

    def test_digest_is_deterministic_confirmed_and_cites_evidence(self, client, ingest_h, admin, worker):
        post(client, ingest_h,
             ev(1, typ="tool_call", actor="assistant", tool={"tool_call_id": "a", "name": "Bash", "command": "ls"}),
             ev(2, typ="tool_result", actor="tool", tool={"tool_call_id": "a", "output": "x", "is_error": True}),
             ev(3, typ="file_change", actor="assistant", file_changes=[{"path": "f.py", "operation": "edit"}]))
        worker.run_once()
        items = client.get("/api/v1/derived?session_id=1", headers=admin).json()["items"]
        assert len(items) == 1
        d = items[0]
        assert d["kind"] == "digest" and d["origin"] == "deterministic" and d["epistemic_status"] == "confirmed"
        assert d["data"]["tools"] == {"Bash": 1} and d["data"]["tool_errors"] == 1 and d["data"]["files"][0]["path"] == "f.py"
        assert len(d["evidence"]) == 2 and d["interpretation"] is False
        assert client.get("/api/v1/stats", headers=admin).json()["processing"]["events_pending"] == 0

    def test_new_events_after_processing_get_reprocessed_and_digest_superseded(self, client, ingest_h, admin, worker):
        post(client, ingest_h, ev(1))
        worker.run_once()
        post(client, ingest_h, ev(2))
        worker.run_once()
        active = client.get("/api/v1/derived?session_id=1", headers=admin).json()["items"]
        old = client.get("/api/v1/derived?session_id=1&state=superseded", headers=admin).json()["items"]
        assert len(active) == 1 and len(old) == 1 and active[0]["title"].startswith("2 events")

    def test_sweeper_recreates_missing_jobs(self, client, ingest_h, worker, sf):
        post(client, ingest_h, ev(1), {**ev(1), "external_session_id": None, "external_event_id": "orphan"})
        with sf() as db:
            db.execute(update(m.Job).values(status="completed"))
            db.commit()
        worker.run_once()
        with sf() as db:
            assert not db.scalars(select(m.Event).where(m.Event.processing_status == "pending")).all()

    def test_worker_thread_runs_in_background(self, tmp_path):
        import time

        from conftest import INGEST
        from fastapi.testclient import TestClient

        from vibevault.app import create_app

        s = make_settings(tmp_path, worker_enabled=True, worker_poll_seconds=0.05)
        with TestClient(create_app(s)) as c:
            post(c, {"Authorization": f"Bearer {INGEST}"}, ev(1))
            for _ in range(100):
                time.sleep(0.05)
                if c.app.state.worker and not any(True for _ in []):
                    with c.app.state.session_factory() as db:
                        if db.scalar(select(m.Job.status)) == "completed":
                            return
            raise AssertionError("worker thread did not complete the job")
