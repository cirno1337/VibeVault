from datetime import timedelta

from conftest import T0, ev, post
from sqlalchemy import func, select

from vibevault import models as m


def count(client, model):
    with client.app_ref.state.session_factory() as db:
        return db.scalar(select(func.count()).select_from(model))


class TestIdempotency:
    def test_retry_same_batch_creates_nothing_new(self, client, ingest_h):
        batch = [ev(1), ev(2, actor="assistant"), ev(3, typ="tool_call", actor="assistant", tool={"tool_call_id": "t1", "name": "Bash", "command": "ls"})]
        first = post(client, ingest_h, *batch)
        assert first["created"] == 3
        second = post(client, ingest_h, *batch)
        assert (second["created"], second["duplicate"]) == (0, 3)
        assert [r["event_id"] for r in first["results"]] == [r["event_id"] for r in second["results"]]
        assert count(client, m.Event) == 3 and count(client, m.Session) == 1 and count(client, m.ToolExecution) == 1

    def test_duplicate_inside_one_batch(self, client, ingest_h):
        res = post(client, ingest_h, ev(1), ev(1))
        assert (res["created"], res["duplicate"]) == (1, 1)

    def test_same_content_different_transport_fields_is_duplicate(self, client, ingest_h):
        post(client, ingest_h, ev(1, metadata={"a": 1}))
        res = post(client, ingest_h, ev(1, metadata={"a": 2}, redaction={"status": "none"}))
        assert res["duplicate"] == 1

    def test_conflicting_resend_keeps_original(self, client, ingest_h, admin):
        r1 = post(client, ingest_h, ev(1, content="original"))
        res = post(client, ingest_h, ev(1, content="tampered"))
        assert res["conflict"] == 1 and res["results"][0]["reason"] == "content_differs_use_new_revision"
        got = client.get(f"/api/v1/events/{r1['results'][0]['event_id']}", headers=admin).json()
        assert got["event"]["content"] == "original"

    def test_event_ids_are_deterministic_and_device_scoped(self, client, ingest_h):
        a = post(client, ingest_h, ev(1, device="pc"))["results"][0]["event_id"]
        b = post(client, ingest_h, ev(1, device="server"))["results"][0]["event_id"]
        assert a != b
        assert post(client, ingest_h, ev(1, device="pc"))["results"][0]["event_id"] == a


class TestEditsAndOrdering:
    def test_new_revision_supersedes_but_keeps_history(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="v1"))
        post(client, ingest_h, ev(1, content="v2", revision=2))
        items = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"]
        assert [i["content"] for i in items] == ["v2"]
        all_ = client.get("/api/v1/sessions/1/events?include_superseded=true", headers=admin).json()["items"]
        assert sorted(i["revision"] for i in all_) == [1, 2]
        detail = client.get(f"/api/v1/events/{items[0]['id']}", headers=admin).json()
        assert [r["revision"] for r in detail["revisions"]] == [1, 2]

    def test_event_count_counts_logical_events_not_revisions(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1), ev(2, revision=2), ev(2, revision=1))  # revisions arrive out of order
        post(client, ingest_h, ev(1, revision=2, content="edited"))
        s = client.get("/api/v1/sessions/1", headers=admin).json()
        listed = client.get("/api/v1/sessions/1/events", headers=admin).json()
        assert s["event_count"] == listed["total"] == 2

    def test_out_of_order_revisions(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="v2", revision=2))
        post(client, ingest_h, ev(1, content="v1", revision=1))
        items = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"]
        assert [i["content"] for i in items] == ["v2"]

    def test_out_of_order_events_are_returned_chronologically(self, client, ingest_h, admin):
        post(client, ingest_h, ev(3, content="third"), ev(1, content="first"))
        post(client, ingest_h, ev(2, content="second"))
        items = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"]
        assert [i["content"] for i in items] == ["first", "second", "third"]

    def test_sequence_breaks_timestamp_ties(self, client, ingest_h, admin):
        t = T0
        post(client, ingest_h, ev(2, content="b", t=t, sequence=2), ev(1, content="a", t=t, sequence=1))
        items = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"]
        assert [i["content"] for i in items] == ["a", "b"]

    def test_tool_result_before_call_is_paired(self, client, ingest_h, admin):
        tool = lambda **k: {"tool_call_id": "tc1", **k}  # noqa: E731
        post(client, ingest_h, ev(2, typ="tool_result", actor="tool", content="out", parent_external_event_id="s1-1", tool=tool(output="out", exit_code=1, is_error=True)))
        post(client, ingest_h, ev(1, typ="tool_call", actor="assistant", content="make", tool=tool(name="Bash", command="make")))
        items = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"]
        call = items[0]
        assert call["tool"]["command"] == "make" and call["tool"]["output"] == "out" and call["tool"]["exit_code"] == 1
        detail = client.get(f"/api/v1/events/{items[1]['id']}", headers=admin).json()
        assert detail["parent"]["external_event_id"] == "s1-1"  # parent resolved though it arrived later
        parent_detail = client.get(f"/api/v1/events/{call['id']}", headers=admin).json()
        assert [c["external_event_id"] for c in parent_detail["children"]] == ["s1-2"]


class TestSessionsAndProjects:
    def test_incomplete_sessions_and_no_session(self, client, ingest_h, admin):
        res = post(client, ingest_h, ev(1, session=None, external_session_id=None) if False else {**ev(1), "external_session_id": None})
        assert res["created"] == 1
        assert count(client, m.Session) == 0
        ev_list = client.get("/api/v1/events?unsessioned=true", headers=admin).json()
        assert ev_list["total"] == 1

    def test_session_without_end_stays_open_and_end_closes(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1))
        assert client.get("/api/v1/sessions/1", headers=admin).json()["status"] == "open"
        post(client, ingest_h, ev(2, typ="session_end", actor="system", content=""))
        s = client.get("/api/v1/sessions/1", headers=admin).json()
        assert s["status"] == "ended" and s["event_count"] == 2

    def test_session_bounds_follow_event_times_not_arrival(self, client, ingest_h, admin):
        post(client, ingest_h, ev(5), ev(1))
        s = client.get("/api/v1/sessions/1", headers=admin).json()
        assert s["started_at"].startswith("2026-01-15T10:00:01") and s["last_event_at"].startswith("2026-01-15T10:00:05")

    def test_project_autocreate_and_reuse(self, client, ingest_h, admin):
        proj = {"key": "/home/me/code/Alpha/", "name": "Alpha"}
        post(client, ingest_h, ev(1, project=proj), ev(2, session="s2", project={"key": "/home/me/code/alpha"}))
        projects = client.get("/api/v1/projects", headers=admin).json()
        assert projects["total"] == 1 and projects["items"][0]["session_count"] == 2

    def test_unresolved_then_resolved_via_alias(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, project={"key": "/srv/mystery"}), ev(2, session="s2"))
        s = client.get("/api/v1/sessions?unresolved=true", headers=admin).json()
        assert s["total"] == 2
        pid = client.post("/api/v1/projects", json={"name": "Mystery"}, headers=admin).json()["id"]
        r = client.post(f"/api/v1/projects/{pid}/aliases", json={"alias": "/srv/mystery/"}, headers=admin).json()
        assert r["sessions_assigned"] == 1
        s1 = client.get("/api/v1/sessions/1", headers=admin).json()
        assert s1["project"]["id"] == pid
        evs = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"]
        assert evs[0]["project_id"] == pid
        assert client.get("/api/v1/sessions?unresolved=true", headers=admin).json()["total"] == 1

    def test_sessionless_event_resolved_via_alias(self, client, ingest_h, admin):
        # Events without a session are retro-assigned too (Event's PK is seq, not the string id).
        post(client, ingest_h, ev(1, session=None, external_event_id="x-1", project={"key": "/srv/orphan"}),
             ev(2, session=None, external_event_id="x-2", project={"key": "/srv/other"}))

        def project_ids():
            with client.app_ref.state.session_factory() as db:
                return dict(db.execute(select(m.Event.external_event_id, m.Event.project_id).where(m.Event.session_id.is_(None))).all())

        assert project_ids() == {"x-1": None, "x-2": None}
        r = client.post("/api/v1/projects", json={"name": "Orphan", "aliases": ["/srv/orphan/"]}, headers=admin)
        assert r.status_code == 201, r.text
        assert project_ids() == {"x-1": r.json()["id"], "x-2": None}
        pid = client.post("/api/v1/projects", json={"name": "Other"}, headers=admin).json()["id"]
        r = client.post(f"/api/v1/projects/{pid}/aliases", json={"alias": "/srv/other"}, headers=admin)
        assert r.status_code < 300, r.text
        assert project_ids()["x-2"] == pid

    def test_alias_conflict(self, client, admin):
        a = client.post("/api/v1/projects", json={"name": "A", "aliases": ["/x"]}, headers=admin).json()
        b = client.post("/api/v1/projects", json={"name": "B"}, headers=admin).json()
        assert client.post(f"/api/v1/projects/{b['id']}/aliases", json={"alias": "/x"}, headers=admin).status_code == 409
        assert a["aliases"] == ["/x"]

    def test_same_name_joins_existing_project(self, client, ingest_h, admin):
        # The same repository on another machine, or a repo created for a project first made by hand.
        pid = client.post("/api/v1/projects", json={"name": "OTS"}, headers=admin).json()["id"]
        post(client, ingest_h, ev(1, project={"key": "/home/me/code/ots", "name": "ots"}),
             ev(1, session="s2", device="server", project={"key": "/srv/OTS", "name": "OTS"}))
        projects = client.get("/api/v1/projects", headers=admin).json()
        assert projects["total"] == 1
        assert sorted(projects["items"][0]["aliases"]) == ["/home/me/code/ots", "/srv/ots"]
        assert client.get("/api/v1/sessions/1", headers=admin).json()["project"]["id"] == pid

    def test_generic_names_do_not_join(self, client, ingest_h, admin):
        client.post("/api/v1/projects", json={"name": "src"}, headers=admin)
        post(client, ingest_h, ev(1, project={"key": "/home/me/other/src", "name": "src"}))
        assert client.get("/api/v1/projects", headers=admin).json()["total"] == 2

    def test_name_matching_can_be_disabled(self, tmp_path, ingest_h, admin):
        from conftest import make_settings
        from fastapi.testclient import TestClient

        from vibevault.app import create_app

        with TestClient(create_app(make_settings(tmp_path, match_projects_by_name=False))) as c:
            c.post("/api/v1/projects", json={"name": "OTS"}, headers=admin)
            post(c, ingest_h, ev(1, project={"key": "/srv/ots", "name": "OTS"}))
            assert c.get("/api/v1/projects", headers=admin).json()["total"] == 2

    def test_merge_moves_sessions_events_and_aliases(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, project={"key": "/pc/TenderAI", "name": "TenderAI"}),
             ev(1, session="s2", project={"key": "/srv/tender-ai", "name": "tender-ai"}))
        projects = {p["name"]: p["id"] for p in client.get("/api/v1/projects", headers=admin).json()["items"]}
        r = client.post(f"/api/v1/projects/{projects['tender-ai']}/merge", json={"into": projects["TenderAI"]}, headers=admin)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["project"]["session_count"] == 2 and sorted(body["project"]["aliases"]) == ["/pc/tenderai", "/srv/tender-ai"]
        assert body["moved"]["sessions"] == 1 and body["moved"]["events"] == 1 and body["moved"]["project_aliases"] == 1
        assert client.get(f"/api/v1/projects/{projects['tender-ai']}", headers=admin).status_code == 404
        # The moved alias keeps resolving: new events from that path land in the merged project.
        post(client, ingest_h, ev(1, session="s3", project={"key": "/srv/tender-ai"}))
        assert client.get("/api/v1/sessions/3", headers=admin).json()["project"]["id"] == projects["TenderAI"]

    def test_merge_rejects_self_and_unknown(self, client, admin):
        pid = client.post("/api/v1/projects", json={"name": "A"}, headers=admin).json()["id"]
        assert client.post(f"/api/v1/projects/{pid}/merge", json={"into": pid}, headers=admin).status_code == 422
        assert client.post(f"/api/v1/projects/{pid}/merge", json={"into": 999}, headers=admin).status_code == 404

    def test_delete_only_empty_projects(self, client, ingest_h, admin):
        empty = client.post("/api/v1/projects", json={"name": "Empty", "aliases": ["/x"]}, headers=admin).json()["id"]
        post(client, ingest_h, ev(1, project={"key": "/used", "name": "Used"}))
        used = next(p["id"] for p in client.get("/api/v1/projects", headers=admin).json()["items"] if p["name"] == "Used")
        r = client.delete(f"/api/v1/projects/{used}", headers=admin)
        assert r.status_code == 409 and r.json()["error"]["code"] == "project_not_empty"
        assert client.delete(f"/api/v1/projects/{empty}", headers=admin).json() == {"deleted": empty, "aliases_removed": ["/x"]}
        # The freed alias can be reused.
        assert client.post("/api/v1/projects", json={"name": "B", "aliases": ["/x"]}, headers=admin).status_code == 201

    def test_filters_and_pagination(self, client, ingest_h, admin):
        for i in range(1, 6):
            post(client, ingest_h, ev(1, session=f"s{i}", t=T0 + timedelta(days=i), source="claude_code" if i % 2 else "chatgpt_web"))
        page = client.get("/api/v1/sessions?limit=2&offset=2", headers=admin).json()
        assert page["total"] == 5 and len(page["items"]) == 2
        assert [s["external_session_id"] for s in page["items"]] == ["s3", "s2"]  # newest first
        since = (T0 + timedelta(days=3, hours=-1)).isoformat().replace("+00:00", "Z")
        assert client.get(f"/api/v1/sessions?since={since}", headers=admin).json()["total"] == 3
        assert client.get("/api/v1/sessions?source=chatgpt_web", headers=admin).json()["total"] == 2
        assert client.get("/api/v1/sessions?limit=500", headers=admin).status_code == 422


class TestProvenance:
    def test_source_device_and_raw_payload_preserved(self, client, ingest_h, admin):
        raw = {"original": {"nested": [1, 2, 3]}, "line": 17}
        r = post(client, ingest_h, ev(1, device="server-ubuntu", device_label="Home server", raw_payload=raw, raw_payload_ref="file:///x.jsonl#17"))
        d = client.get(f"/api/v1/events/{r['results'][0]['event_id']}", headers=admin).json()["event"]
        assert d["source"] == "claude_code" and d["device"]["key"] == "server-ubuntu" and d["device"]["label"] == "Home server"
        assert d["raw_payload"] == raw and d["raw_payload_ref"] == "file:///x.jsonl#17"
        assert d["ingested_by"] == "ingest:laptop" and d["ingested_at"]
        devices = client.get("/api/v1/devices", headers=admin).json()["items"]
        assert devices[0]["device_key"] == "server-ubuntu"

    def test_multiple_devices_same_external_ids_do_not_collide(self, client, ingest_h):
        res = post(client, ingest_h, ev(1, device="pc"), ev(1, device="server"))
        assert res["created"] == 2

    def test_file_changes_are_stored(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, typ="file_change", actor="assistant", content="edit a.py", file_changes=[{"path": "a.py", "operation": "edit", "diff": "-x\n+y"}]))
        e = client.get("/api/v1/sessions/1/events", headers=admin).json()["items"][0]
        assert e["file_changes"][0]["path"] == "a.py" and e["file_changes"][0]["diff"] == "-x\n+y"


class TestCollectionControls:
    def test_disabled_device_is_skipped(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1))
        dev = client.get("/api/v1/devices", headers=admin).json()["items"][0]
        client.patch(f"/api/v1/devices/{dev['id']}", json={"collection_enabled": False}, headers=admin)
        res = post(client, ingest_h, ev(2))
        assert res["skipped"] == 1 and res["results"][0]["reason"] == "device_collection_disabled"
        assert post(client, ingest_h, ev(1))["duplicate"] == 1  # retries stay idempotent

    def test_disabled_project_is_skipped(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, project={"key": "/p", "name": "P"}))
        client.patch("/api/v1/projects/1", json={"collection_enabled": False}, headers=admin)
        res = post(client, ingest_h, ev(2, session="other", project={"key": "/p"}))
        assert res["results"][0]["reason"] == "project_collection_disabled"

    def test_excluded_session_drops_new_events(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1))
        client.patch("/api/v1/sessions/1", json={"excluded": True}, headers=admin)
        assert post(client, ingest_h, ev(2))["results"][0]["reason"] == "session_excluded"
        assert client.get("/api/v1/sessions", headers=admin).json()["total"] == 0

    def test_delete_session_removes_evidence_and_leaves_tombstone(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="uniqueword_zebra"), ev(2))
        r = client.delete("/api/v1/sessions/1", headers=admin).json()
        assert r["deleted_events"] == 2
        assert client.get("/api/v1/search?q=uniqueword_zebra", headers=admin).json()["total"] == 0
        assert post(client, ingest_h, ev(1, content="uniqueword_zebra"))["results"][0]["reason"] == "session_excluded"
        actions = [a["action"] for a in client.get("/api/v1/audit", headers=admin).json()["items"]]
        assert "session_delete" in actions

    def test_audit_log_has_counts_not_content(self, client, ingest_h, admin):
        post(client, ingest_h, ev(1, content="sensitive-looking text zzz"))
        audit = client.get("/api/v1/audit", headers=admin)
        assert "zzz" not in audit.text and audit.json()["items"][0]["detail"]["created"] == 1
