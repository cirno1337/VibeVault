"""Operational commands: backup/restore round trip, retention, synthetic purge, seeding."""

import sqlite3
from datetime import timedelta

import pytest
from conftest import ADMIN, INGEST, ev, make_settings, post
from fastapi.testclient import TestClient

from vibevault import cli
from vibevault.app import create_app
from vibevault.synthetic import build

H = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    monkeypatch.setenv("VIBEVAULT_DATABASE_URL", s.database_url)
    monkeypatch.setenv("VIBEVAULT_ADMIN_TOKEN", ADMIN)
    from vibevault.config import get_settings

    get_settings.cache_clear()
    yield s, tmp_path
    get_settings.cache_clear()


def counts(settings):
    c = sqlite3.connect(settings.database_url.removeprefix("sqlite:///"))
    return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_backup_restore_round_trip(env, capsys):
    s, tmp = env
    with TestClient(create_app(s)) as c:
        post(c, {"Authorization": f"Bearer {INGEST}"}, ev(1, content="precious memory"), ev(2))
    cli.main(["backup", str(tmp / "bk") + "/"])  # trailing slash = directory that doesn't exist yet
    backup = next((tmp / "bk").glob("vibevault-*.db"))
    assert oct(backup.stat().st_mode & 0o777) == "0o600"
    # destroy live data, then restore
    with TestClient(create_app(s)) as c:
        c.delete("/api/v1/sessions/1", headers=H)
    assert counts(s) == 0
    cli.main(["restore", str(backup)])
    assert counts(s) == 2
    assert list(tmp.glob("test.db.pre-restore-*"))  # previous DB kept
    with TestClient(create_app(s)) as c:  # restored DB still works, including search (FTS intact)
        assert c.get("/api/v1/search?q=precious", headers=H).json()["total"] == 1
        assert c.get("/readyz").status_code == 200


def test_restore_refuses_corrupt_backup(env, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises((SystemExit, sqlite3.DatabaseError)):
        cli.main(["restore", str(bad)])


def test_prune_dry_run_then_delete(env, capsys):
    from datetime import UTC, datetime

    s, _ = env
    now = datetime.now(UTC)
    h = {"Authorization": f"Bearer {INGEST}"}
    with TestClient(create_app(s)) as c:
        post(c, h, ev(1, session="a", content="ancient", t=now - timedelta(days=900)),
             ev(1, session="b", content="middling", t=now - timedelta(days=100)),
             ev(1, session="c", content="fresh", t=now))
    cli.main(["prune", "--older-than-days", "365"])
    assert "dry run" in capsys.readouterr().out and counts(s) == 3  # nothing deleted without --yes
    cli.main(["prune", "--older-than-days", "365", "--yes"])
    assert counts(s) == 2
    cli.main(["prune", "--older-than-days", "50", "--yes"])
    assert counts(s) == 1
    with TestClient(create_app(s)) as c:
        assert c.get("/api/v1/search?q=ancient", headers=H).json()["total"] == 0  # FTS rows removed too
        assert c.get("/api/v1/search?q=fresh", headers=H).json()["total"] == 1
        assert "prune" in [a["action"] for a in c.get("/api/v1/audit", headers=H).json()["items"]]


def test_synthetic_dataset_is_labelled_complete_and_purgeable(env):
    s, _ = env
    events = build()
    assert all(e["source"] == "synthetic" and e["metadata"]["synthetic"] is True for e in events)
    with TestClient(create_app(s)) as c:
        h = {"Authorization": f"Bearer {INGEST}"}
        first = post(c, h, *events)
        assert first["rejected"] == 0 and first["conflict"] == 0
        assert post(c, h, *events)["created"] == 0  # seeding twice is harmless
        # exercises the interesting cases: unresolved project, redaction, revision, tool failure
        assert c.get("/api/v1/sessions?unresolved=true", headers=H).json()["total"] == 1
        assert c.get("/api/v1/stats", headers=H).json()["redaction"]["events_redacted"] == 1
        assert c.get("/api/v1/search?q=hunter2hunter2", headers=H).json()["total"] == 0
    cli.main(["purge-synthetic"])
    assert counts(s) == 0


def test_seed_requires_token(monkeypatch):
    monkeypatch.delenv("VIBEVAULT_TOKEN", raising=False)
    monkeypatch.delenv("VIBEVAULT_ADMIN_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["seed", "--url", "http://127.0.0.1:1"])


def test_openapi_contract_has_versioned_paths(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    assert {"/api/v1/events", "/api/v1/events/batch", "/api/v1/search", "/api/v1/sessions", "/api/v1/projects",
            "/api/v1/jobs", "/api/v1/stats", "/healthz", "/readyz"} <= paths
    assert all(p.startswith("/api/v1/") or p in ("/healthz", "/readyz") for p in paths)
