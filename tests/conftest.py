from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from vibevault.app import create_app
from vibevault.config import Settings

ADMIN = "admin-token-0123456789abcdef"
INGEST = "ingest-token-0123456789abcdef"
READ = "read-token-0123456789abcdef"
T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


def make_settings(tmp_path, **kw) -> Settings:
    base = dict(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        admin_token=ADMIN,
        ingest_tokens=f"laptop={INGEST}",
        read_tokens=f"khoj={READ}",
        worker_enabled=False,  # tests drive the worker explicitly
        log_json=False,
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:  # runs lifespan → migrations
        c.app_ref = app
        yield c


@pytest.fixture
def admin(client):
    return {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def ingest_h():
    return {"Authorization": f"Bearer {INGEST}"}


def ev(n=1, *, session="s1", source="claude_code", device="pc-arch", typ="message", actor="user",
       content="hello world", t=None, **kw):
    d = {
        "source": source, "device_id": device, "external_session_id": session,
        "external_event_id": f"{session}-{n}", "event_type": typ, "actor": actor,
        "occurred_at": (t or T0 + timedelta(seconds=n)).isoformat(), "content": content,
    }
    d.update(kw)
    return d


def post(client, headers, *events):
    r = client.post("/api/v1/events/batch", json={"events": list(events)}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def worker(client):
    app = client.app_ref
    return app.state.worker


@pytest.fixture
def read_h():
    return {"Authorization": f"Bearer {READ}"}
