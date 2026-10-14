from datetime import timedelta

import pytest
from conftest import T0, ev, post

from vibevault.search import EmptyQuery, build_match


def search(client, admin, **params):
    return client.get("/api/v1/search", params=params, headers=admin).json()


class TestQueryBuilding:
    def test_basic(self):
        assert build_match("alembic migration") == '"alembic" "migration"*'

    def test_phrase_and_operators_are_neutralised(self):
        assert build_match('"no such column" OR drop; --') == '"no such column" "OR" "drop"*'
        assert build_match("a AND NEAR(b c)") == '"a" "AND" "NEAR" "b" "c"'

    def test_empty(self):
        with pytest.raises(EmptyQuery):
            build_match("!!! ---")


class TestSearch:
    def test_finds_relevant_with_highlight(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="The alembic migration failed: no such column owner_id"), ev(2, content="unrelated chatter about lunch"))
        r = search(client, admin, q="alembic migration")
        assert r["total"] == 1
        assert "alembic" in r["items"][0]["snippet"]

    def test_prefix_and_stemming(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="running migrations on startup"))
        assert search(client, admin, q="migrat")["total"] == 1
        assert search(client, admin, q="run")["total"] == 1

    def test_phrase(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="no such column"), ev(2, content="such is life, no column here"))
        assert search(client, admin, q='"no such column"')["total"] == 1

    def test_hostile_input_does_not_error(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="hello"))
        for q in ['"', "AND OR NOT", "hello*(", "a:b", "col:value NEAR/3", "'; DROP TABLE events; --"]:
            r = client.get("/api/v1/search", params={"q": q}, headers=admin)
            assert r.status_code in (200, 400), q
        assert client.get("/api/v1/search", params={"q": "!!!"}, headers=admin).status_code == 400

    def test_filters(self, client, ingest_h, admin):
        post(client, ingest_h,
             ev(1, content="deploy failed", project={"key": "/a", "name": "A"}, source="claude_code"),
             ev(1, session="c1", content="deploy question", source="chatgpt_web", actor="user", t=T0 + timedelta(days=10)),
             ev(2, content="deploy output", typ="tool_result", actor="tool", project={"key": "/a"}))
        assert search(client, admin, q="deploy")["total"] == 3
        assert search(client, admin, q="deploy", source="chatgpt_web")["total"] == 1
        assert search(client, admin, q="deploy", project_id=1)["total"] == 2
        assert search(client, admin, q="deploy", event_type="tool_result")["total"] == 1
        assert search(client, admin, q="deploy", actor="tool")["total"] == 1
        since = (T0 + timedelta(days=5)).isoformat().replace("+00:00", "Z")
        assert search(client, admin, q="deploy", since=since)["total"] == 1
        until = (T0 + timedelta(days=5)).isoformat().replace("+00:00", "Z")
        assert search(client, admin, q="deploy", until=until)["total"] == 2

    def test_superseded_revisions_hidden_by_default(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="old wording kumquat"))
        post(client, ingest_h, ev(1, content="new wording", revision=2))
        assert search(client, admin, q="kumquat")["total"] == 0
        assert search(client, admin, q="kumquat", include_superseded="true")["total"] == 1

    def test_pagination(self, client, ingest_h, admin):
        post(client, ingest_h, *[ev(i, content=f"needle number {i}") for i in range(1, 8)])
        r = search(client, admin, q="needle", limit=3, offset=3)
        assert r["total"] == 7 and len(r["items"]) == 3

    def test_result_links_to_session_and_project(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="zebra", session_title="Zoo session", project={"key": "/z", "name": "Zoo"}))
        item = search(client, admin, q="zebra")["items"][0]
        assert item["session_title"] == "Zoo session" and item["project_name"] == "Zoo"
