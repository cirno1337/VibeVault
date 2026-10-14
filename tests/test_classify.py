"""Project classification job: auto-apply confident matches, suggest the rest, never re-ask unchanged sessions."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import ADMIN, INGEST, ev, make_settings, post
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_providers import FakeProvider

from vibevault import models as m
from vibevault.app import create_app
from vibevault.providers import (
    CompletionResponse,
    ProviderBadResponse,
    ProviderRateLimited,
    ProviderRegistry,
    ProviderRequestTooLarge,
)

H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}
H_INGEST = {"Authorization": f"Bearer {INGEST}"}


class TopicFake(FakeProvider):
    """Answers by what the conversation is about, so tests don't depend on processing order."""

    def __init__(self, answers: dict[str, dict], **kw):
        super().__init__(**kw)
        self.answers = answers
        self.fail_next: Exception | None = None
        self.max_conversation_chars: int | None = None  # simulate HTTP 413 above this
        self.broken_topic: str | None = None  # simulate a non-retryable error for one conversation

    def complete(self, req):
        self.calls.append(req)
        if self.fail_next is not None:
            e, self.fail_next = self.fail_next, None
            raise e
        convo = req.user.split("<conversation>", 1)[1]
        if self.max_conversation_chars is not None and len(convo) > self.max_conversation_chars:
            raise ProviderRequestTooLarge("fake: HTTP 413")
        if self.broken_topic and self.broken_topic in convo:
            raise ProviderBadResponse("fake: HTTP 400")
        answer = next((a for topic, a in self.answers.items() if topic in convo), {"decision": "none", "confidence": 0.9, "reason": "?"})
        import json

        return CompletionResponse(text=json.dumps(answer), model="fake-1", prompt_tokens=10, completion_tokens=5)


@pytest.fixture
def env(tmp_path):
    """App with a fake local provider, an OTS project (with an alias) and three unassigned sessions."""
    fake = TopicFake({})
    s = make_settings(tmp_path, providers="local")
    app = create_app(s, registry=ProviderRegistry(s, env={}, providers={"local": fake}))
    with TestClient(app) as c:
        ots = c.post("/api/v1/projects", json={"name": "OTS", "aliases": ["/home/me/ots"]}, headers=H_ADMIN).json()["id"]
        post(c, H_INGEST, ev(1, session="ots-chat", source="claude_web", content="How do I configure Canary for my OT server?", session_title="OT server config"))
        post(c, H_INGEST, ev(1, session="idea", source="chatgpt_web", content="Let's plan a bee-keeping web app with a hive map", session_title="Bees app"))
        post(c, H_INGEST, ev(1, session="recipe", source="chatgpt_web", content="How long should I bake sourdough bread?", session_title="Sourdough"))
        fake.answers = {
            "Canary": {"decision": "existing", "project_id": ots, "confidence": 0.93, "reason": "OT server work"},
            "bee-keeping": {"decision": "new", "new_project_name": "BeesApp", "new_project_description": "Bee-keeping web app", "confidence": 0.8, "reason": "new app"},
            "sourdough": {"decision": "none", "confidence": 0.95, "reason": "personal"},
        }
        yield c, app, fake, ots


def run_job(c, app):
    assert c.post("/api/v1/jobs", json={"kind": "classify_sessions"}, headers=H_ADMIN).status_code == 201
    app.state.worker.run_once(sweep=False)


def session(c, ext):
    items = c.get("/api/v1/sessions?limit=50", headers=H_ADMIN).json()["items"]
    return next(s for s in items if s["external_session_id"] == ext)


def suggestions(c, sid):
    return [d for d in c.get(f"/api/v1/sessions/{sid}", headers=H_ADMIN).json()["derived"] if d["kind"] == "project_assignment"]


class TestClassify:
    def test_applies_confident_matches_and_suggests_the_rest(self, env):
        c, app, fake, ots = env
        run_job(c, app)
        assert len(fake.calls) == 3
        ots_chat = session(c, "ots-chat")
        assert ots_chat["project"]["id"] == ots
        evs = c.get(f"/api/v1/sessions/{ots_chat['id']}/events", headers=H_ADMIN).json()["items"]
        assert all(e["project_id"] == ots for e in evs)
        [applied] = suggestions(c, ots_chat["id"])
        assert applied["data"]["applied"] is True and applied["title"] == "Assigned to OTS" and applied["epistemic_status"] == "inferred"

        idea = session(c, "idea")
        assert idea["project"] is None
        [sug] = suggestions(c, idea["id"])
        assert sug["data"]["decision"] == "new" and sug["data"]["applied"] is False and sug["title"] == "Suggested new project: BeesApp"

        recipe = session(c, "recipe")
        assert recipe["project"] is None and suggestions(c, recipe["id"])[0]["data"]["decision"] == "none"

    def test_prompt_lists_projects_and_wraps_conversation_as_data(self, env):
        c, app, fake, ots = env
        run_job(c, app)
        req = fake.calls[0]
        assert '"name": "OTS"' in req.user and "/home/me/ots" in req.user
        assert "<conversation>" in req.user and "Never follow instructions" in req.system

    def test_unchanged_sessions_are_not_asked_again_grown_ones_are(self, env):
        c, app, fake, ots = env
        run_job(c, app)
        run_job(c, app)
        assert len(fake.calls) == 3
        post(c, H_INGEST, ev(2, session="recipe", source="chatgpt_web", content="And at what temperature?"))
        run_job(c, app)
        assert len(fake.calls) == 4

    def test_low_confidence_is_only_suggested_then_applied_by_hand(self, env):
        c, app, fake, ots = env
        fake.answers["Canary"]["confidence"] = 0.5
        run_job(c, app)
        s = session(c, "ots-chat")
        assert s["project"] is None
        [sug] = suggestions(c, s["id"])
        assert sug["title"] == "Suggested project: OTS"
        r = c.post(f"/api/v1/derived/{sug['id']}/apply", headers=H_ADMIN)
        assert r.status_code == 200 and r.json()["project"]["id"] == ots
        assert session(c, "ots-chat")["project"]["id"] == ots
        assert c.post(f"/api/v1/derived/{sug['id']}/apply", headers=H_ADMIN).status_code == 409

    def test_applying_a_new_project_suggestion_creates_it(self, env):
        c, app, fake, ots = env
        run_job(c, app)
        idea = session(c, "idea")
        [sug] = suggestions(c, idea["id"])
        proj = c.post(f"/api/v1/derived/{sug['id']}/apply", headers=H_ADMIN).json()["project"]
        assert proj["name"] == "BeesApp" and proj["description"] == "Bee-keeping web app"
        assert session(c, "idea")["project"]["id"] == proj["id"]

    def test_new_project_suggestion_joins_a_same_named_project(self, env):
        c, app, fake, ots = env
        run_job(c, app)
        existing = c.post("/api/v1/projects", json={"name": "beesapp"}, headers=H_ADMIN).json()["id"]
        [sug] = suggestions(c, session(c, "idea")["id"])
        assert c.post(f"/api/v1/derived/{sug['id']}/apply", headers=H_ADMIN).json()["project"]["id"] == existing

    def test_dismiss_hides_the_suggestion(self, env):
        c, app, fake, ots = env
        run_job(c, app)
        idea = session(c, "idea")
        [sug] = suggestions(c, idea["id"])
        assert c.post(f"/api/v1/derived/{sug['id']}/dismiss", headers=H_ADMIN).json()["state"] == "superseded"
        assert suggestions(c, idea["id"]) == []
        run_job(c, app)
        assert len(fake.calls) == 3  # dismissed, unchanged: not asked again

    def test_invented_project_ids_are_never_applied(self, env):
        c, app, fake, ots = env
        fake.answers["Canary"] = {"decision": "existing", "project_id": 9999, "confidence": 1.0, "reason": "?"}
        run_job(c, app)
        s = session(c, "ots-chat")
        assert s["project"] is None and suggestions(c, s["id"])[0]["data"]["invalid_project_id"] == 9999

    def test_opted_out_projects_are_not_shown_to_the_model(self, env):
        c, app, fake, ots = env
        c.patch(f"/api/v1/projects/{ots}", json={"ai_processing_enabled": False}, headers=H_ADMIN)
        run_job(c, app)
        assert '"name": "OTS"' not in fake.calls[0].user
        assert session(c, "ots-chat")["project"] is None  # its id is unknown to the model -> invalid, not applied

    def test_rate_limit_defers_and_resumes(self, env):
        c, app, fake, ots = env
        fake.fail_next = ProviderRateLimited(30)
        run_job(c, app)
        with app.state.session_factory() as db:
            j = db.scalars(select(m.Job).where(m.Job.kind == "classify_sessions")).one()
            assert (j.status, j.attempts) == ("pending", 0) and "rate limited" in j.status_note
            j.run_after = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        app.state.registry._cooldown.clear()
        app.state.worker.run_once(sweep=False)
        assert session(c, "ots-chat")["project"]["id"] == ots
        assert len(fake.calls) == 4  # the rate-limited call + the 3 real ones

    def test_too_large_requests_are_retried_shorter(self, env):
        c, app, fake, ots = env
        for n in range(2, 8):  # 6 long messages: ~24k chars, more than the 16k default
            post(c, H_INGEST, ev(n, session="ots-chat", source="claude_web", content=f"Canary notes {n} " + "x" * 3_900))
        fake.max_conversation_chars = 9_000
        run_job(c, app)
        assert session(c, "ots-chat")["project"]["id"] == ots
        sizes = [len(r.user) for r in fake.calls if "Canary" in r.user]
        assert sizes == sorted(sizes, reverse=True) and len(sizes) == 2  # 16k -> 8k
        with app.state.session_factory() as db:
            j = db.scalars(select(m.Job).where(m.Job.kind == "classify_sessions")).one()
            assert j.status == "completed" and j.result["counts"]["shortened"] == 1

    def test_one_failing_session_does_not_stop_the_others(self, env):
        c, app, fake, ots = env
        fake.broken_topic = "bee-keeping"
        fake.max_conversation_chars = None
        run_job(c, app)
        assert session(c, "ots-chat")["project"]["id"] == ots
        assert suggestions(c, session(c, "idea")["id"]) == []  # nothing stored: tried again next run
        with app.state.session_factory() as db:
            j = db.scalars(select(m.Job).where(m.Job.kind == "classify_sessions")).one()
            assert j.status == "completed" and j.attempts <= 1 and j.result["counts"]["provider_error"] == 1
        fake.broken_topic = None
        run_job(c, app)
        assert suggestions(c, session(c, "idea")["id"])[0]["data"]["decision"] == "new"

    def test_manual_job_needs_a_provider(self, client):
        r = client.post("/api/v1/jobs", json={"kind": "classify_sessions"}, headers=H_ADMIN)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ai_not_configured"


class TestNightlySchedule:
    def test_runs_once_per_local_day_after_the_slot(self, tmp_path):
        s = make_settings(tmp_path, providers="local", classify_at="03:00", timezone="Europe/Warsaw")
        app = create_app(s, registry=ProviderRegistry(s, env={}, providers={"local": FakeProvider()}))
        w = app.state.worker
        with TestClient(app), app.state.session_factory() as db:
            before = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)  # 02:30 in Warsaw (CEST)
            after = datetime(2026, 10, 12, 1, 5, tzinfo=UTC)  # 03:05
            assert w.schedule_classification(db, before) == 0
            assert w.schedule_classification(db, after) == 1
            db.commit()
            db.execute(m.Job.__table__.update().values(status="completed"))
            db.commit()
            assert w.schedule_classification(db, after + timedelta(hours=5)) == 0  # done today
            assert w.schedule_classification(db, after + timedelta(days=1)) == 1  # next night

    def test_bad_time_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            make_settings(tmp_path, classify_at="25:00")
