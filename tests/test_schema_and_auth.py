
import pytest
from conftest import ev, post
from pydantic import ValidationError

from vibevault.schemas import EventIn


class TestSchema:
    def test_minimal_valid(self):
        e = EventIn.model_validate(ev())
        assert e.revision == 1 and e.schema_version == "1"

    @pytest.mark.parametrize("patch", [
        {"event_type": "nonsense"}, {"actor": "robot"}, {"occurred_at": "2026-01-01T00:00:00"},  # naive
        {"device_id": "has space"}, {"source": "UPPER"}, {"revision": 0}, {"unknown_field": 1},
        {"external_event_id": ""},
    ])
    def test_invalid(self, patch):
        with pytest.raises(ValidationError):
            EventIn.model_validate({**ev(), **patch})

    def test_nul_stripped(self):
        assert EventIn.model_validate(ev(content="a\x00b")).content == "ab"


class TestAuth:
    def test_health_is_public(self, client):
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/readyz").json()["status"] == "ready"

    def test_missing_and_bad_token(self, client):
        assert client.get("/api/v1/stats").status_code == 401
        r = client.get("/api/v1/stats", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert r.json()["error"]["code"] == "invalid_token"

    def test_ingest_token_can_only_ingest(self, client, ingest_h, admin):
        assert client.post("/api/v1/events", json=ev(), headers=ingest_h).status_code == 200
        for path in ("/api/v1/stats", "/api/v1/sessions", "/api/v1/search?q=hello", "/api/v1/events", "/api/v1/jobs"):
            assert client.get(path, headers=ingest_h).status_code == 403, path
        assert client.get("/api/v1/stats", headers=admin).status_code == 200

    def test_me(self, client, ingest_h, admin):
        assert client.get("/api/v1/me", headers=ingest_h).json()["scope"] == "ingest"
        assert client.get("/api/v1/me", headers=admin).json()["scope"] == "admin"

    def test_refuses_to_start_without_admin_token(self, tmp_path):
        from conftest import make_settings

        from vibevault.app import create_app

        with pytest.raises(RuntimeError):
            create_app(make_settings(tmp_path, admin_token=None))

    @pytest.mark.parametrize("tok", ["short", "change-me", "changeme"])
    def test_weak_admin_tokens_rejected(self, tmp_path, tok):
        from conftest import make_settings

        with pytest.raises(ValueError):
            make_settings(tmp_path, admin_token=tok)


class TestValidationAndLimits:
    def test_validation_error_does_not_echo_payload(self, client, ingest_h):
        secret = "sk-ant-" + "api03-SHOULDNOTECHO0123456789"
        r = client.post("/api/v1/events/batch", json={"events": [ev(event_type=secret)]}, headers=ingest_h)
        assert r.status_code == 200
        item = r.json()["results"][0]
        assert item["status"] == "rejected" and item["reason"] == "validation_error"
        assert secret not in r.text

    def test_envelope_error_is_422_and_structured(self, client, ingest_h):
        r = client.post("/api/v1/events/batch", json={"evnts": []}, headers=ingest_h)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_error"

    def test_partial_batch(self, client, ingest_h):
        res = post(client, ingest_h, ev(1), {"bogus": True}, ev(2))
        assert (res["created"], res["rejected"]) == (2, 1)
        assert [r["status"] for r in res["results"]] == ["created", "rejected", "created"]

    def test_batch_too_large(self, client, ingest_h):
        client.app_ref.state.settings.max_batch_events = 3
        r = client.post("/api/v1/events/batch", json={"events": [ev(i) for i in range(1, 5)]}, headers=ingest_h)
        assert r.status_code == 413 and r.json()["error"]["code"] == "batch_too_large"

    def test_oversized_body(self, client, ingest_h):
        big = "x" * 9_000_000
        r = client.post("/api/v1/events", json=ev(content=big), headers=ingest_h)
        assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    def test_oversized_event_content_rejected_per_item(self, client, ingest_h):
        client.app_ref.state.settings.max_event_content_bytes = 100
        res = post(client, ingest_h, ev(1, content="y" * 101), ev(2))
        assert res["results"][0]["reason"] == "content_too_large" and res["created"] == 1

    def test_malformed_json(self, client, ingest_h):
        r = client.post("/api/v1/events/batch", content=b"{not json", headers={**ingest_h, "content-type": "application/json"})
        assert r.status_code == 422
