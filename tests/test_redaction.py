import json
import logging

import pytest
from conftest import ev, post

from vibevault.redaction import RedactionReport, Redactor

FAKE = {
    "anthropic_key": "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0",
    "github_token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "aws_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "google_api_key": "AIza" + "SyA-1234567890abcdefghijklmnopqrstu",
    "private_key": "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEAxyz\n-----END RSA " + "PRIVATE KEY-----",
    "jwt": "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".abcdefghijklmnop",
    "slack_token": "xox" + "b-1234567890-abcdefghij",
}


class TestRedactor:
    @pytest.mark.parametrize("rule", sorted(FAKE))
    def test_known_patterns(self, rule):
        rep = RedactionReport()
        out = Redactor().redact_text(f"before {FAKE[rule]} after", rep)
        assert FAKE[rule] not in out and rep.counts[rule] == 1
        assert out.startswith("before ") and out.endswith(" after") or rule == "private_key"

    def test_bearer_and_assignments_keep_context(self):
        rep = RedactionReport()
        out = Redactor().redact_text("Authorization: Bearer abcdef0123456789xyz\nexport DB_PASSWORD=sup3rs3cretvalue\nurl=postgres://bob:hunter2pw@db/x", rep)
        assert "abcdef0123456789xyz" not in out and "sup3rs3cretvalue" not in out and "hunter2pw" not in out
        assert "DB_PASSWORD=" in out and "postgres://bob:" in out and "@db/x" in out
        assert set(rep.counts) == {"bearer_token", "secret_assignment", "url_credentials"}

    def test_placeholders_and_normal_text_untouched(self):
        txt = "password: ${PASSWORD}\nset token=<your-token>\napi_key=xxxxxxxx\nThe tokenizer splits tokens."
        rep = RedactionReport()
        assert Redactor().redact_text(txt, rep) == txt and rep.total == 0

    def test_structured_data_and_sensitive_keys(self):
        rep = RedactionReport()
        out = Redactor().redact_obj({"headers": {"Authorization": "Basic Zm9vOmJhcg=="}, "env": [{"password": "pw-123456"}], "n": 5, "ok": "fine"}, rep)
        assert "Zm9vOmJhcg" not in json.dumps(out) and "pw-123456" not in json.dumps(out)
        assert out["n"] == 5 and out["ok"] == "fine" and rep.counts["sensitive_key"] == 2

    def test_custom_rules(self, tmp_path):
        f = tmp_path / "rules.json"
        f.write_text(json.dumps([{"name": "ticket", "pattern": "INTERNAL-\\d{4}"}]))
        rep = RedactionReport()
        assert Redactor.from_file(f).redact_text("see INTERNAL-1234", rep) == "see [REDACTED:ticket]"
        assert rep.counts["ticket"] == 1

    def test_report_contains_no_secret(self):
        rep = RedactionReport()
        Redactor().redact_text(FAKE["github_token"], rep)
        assert FAKE["github_token"] not in json.dumps(rep.as_dict())


class TestServerSideRedaction:
    def test_secrets_never_stored_anywhere(self, client, ingest_h, admin):
        secret = FAKE["github_token"]
        r = post(client, ingest_h, ev(
            1, content=f"token is {secret}", metadata={"env": f"X={secret}"}, raw_payload={"line": secret},
            session_title=f"title {secret}", tool={"tool_call_id": "t", "command": f"curl -H 'Authorization: Bearer {secret}1234'", "output": secret},
            file_changes=[{"path": "a", "operation": "edit", "diff": f"+KEY={secret}"}], typ="tool_call", actor="assistant",
        ))
        eid = r["results"][0]["event_id"]
        blob = client.get(f"/api/v1/events/{eid}", headers=admin).text + client.get("/api/v1/sessions/1", headers=admin).text
        assert secret not in blob
        e = client.get(f"/api/v1/events/{eid}", headers=admin).json()["event"]
        assert e["redaction"]["status"] == "redacted" and e["redaction"]["count"] >= 5
        assert "github_token" in e["redaction"]["rules"]
        assert client.get("/api/v1/search", params={"q": secret[:20]}, headers=admin).json()["total"] == 0

    def test_clean_event_marked_none(self, client, ingest_h, admin):
        r = post(client, ingest_h, ev(1, content="nothing secret here"))
        e = client.get(f"/api/v1/events/{r['results'][0]['event_id']}", headers=admin).json()["event"]
        assert e["redaction"] == {"status": "none", "count": 0, "rules": {}}

    def test_collector_reported_redaction_is_merged(self, client, ingest_h, admin):
        r = post(client, ingest_h, ev(1, content="[REDACTED:openai_key] used", redaction={"status": "redacted", "count": 1, "rules": {"openai_key": 1}}))
        e = client.get(f"/api/v1/events/{r['results'][0]['event_id']}", headers=admin).json()["event"]
        assert e["redaction"]["status"] == "redacted" and e["redaction"]["rules"] == {"openai_key": 1}

    def test_server_redaction_can_be_disabled_and_status_reflects_it(self, tmp_path):
        from conftest import ADMIN, INGEST, make_settings
        from fastapi.testclient import TestClient

        from vibevault.app import create_app

        with TestClient(create_app(make_settings(tmp_path, server_redaction=False))) as c:
            r = post(c, {"Authorization": f"Bearer {INGEST}"}, ev(1, content="hello"))
            e = c.get(f"/api/v1/events/{r['results'][0]['event_id']}", headers={"Authorization": f"Bearer {ADMIN}"}).json()["event"]
            assert e["redaction"]["status"] == "unscanned"

    def test_logs_do_not_contain_payloads_or_tokens(self, client, ingest_h, caplog):
        secret = FAKE["anthropic_key"]
        with caplog.at_level(logging.DEBUG):
            post(client, ingest_h, ev(1, content=secret), {"bad": secret})
            client.post("/api/v1/events", json={"x": secret}, headers={"Authorization": "Bearer wrong-" + secret})
        text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
        assert secret not in text and "ingest-token" not in text


def test_shared_vectors_match_browser_port():
    """tests/fixtures/redaction_vectors.json is also asserted by extension/test/redactor.test.ts."""
    from pathlib import Path

    vectors = json.loads((Path(__file__).parent / "fixtures" / "redaction_vectors.json").read_text())
    for v in vectors:
        rep = RedactionReport()
        assert Redactor().redact_text("".join(v["input"]), rep) == v["output"]
        assert rep.as_dict() == v["rules"]
