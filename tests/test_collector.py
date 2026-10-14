import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from conftest import ADMIN, INGEST, make_settings
from fastapi.testclient import TestClient

from vibevault.app import create_app
from vibevault.collectors.claude_code import handle_hook, is_excluded, parse_line, scan_transcript
from vibevault.collectors.client import SendResult
from vibevault.collectors.config import CollectorConfig, Destination
from vibevault.collectors.delivery import flush
from vibevault.collectors.spool import Spool
from vibevault.redaction import Redactor

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # built at runtime: no token-shaped literal in the repo
_RAW = Path(__file__).parent / "fixtures" / "claude_code_synthetic.jsonl"
FIXTURE = Path(tempfile.mkdtemp(prefix="vv-fixture-")) / "claude_code_synthetic.jsonl"
FIXTURE.write_text(_RAW.read_text().replace("__FAKE_GITHUB_TOKEN__", GH))


@pytest.fixture
def cfg(tmp_path):
    return CollectorConfig(device_id="pc-arch", device_label="Main PC", spool_dir=tmp_path / "spool",
                           destinations=[Destination("home", "http://vault.invalid", token_env="T")], batch_size=50)


@pytest.fixture
def spool(cfg):
    s = Spool(cfg.spool_dir)
    yield s
    s.close()


def lines():
    return [json.loads(x) for x in FIXTURE.read_text().splitlines()]


class TestParser:
    def parse_all(self, cfg):
        out, skipped = [], []
        for n, obj in enumerate(lines(), 1):
            evs, reason = parse_line(obj, cfg, path="/t.jsonl", line_no=n)
            out += evs
            skipped.append(reason)
        return out, skipped

    def test_event_mapping(self, cfg):
        evs, skipped = self.parse_all(cfg)
        kinds = [(e["event_type"], e["actor"]) for e in evs]
        assert kinds == [
            ("message", "user"), ("message", "assistant"), ("tool_call", "assistant"), ("tool_result", "tool"),
            ("tool_call", "assistant"), ("tool_call", "assistant"), ("tool_result", "tool"), ("tool_result", "tool"),
            ("message", "assistant"), ("system", "system"),
        ]
        assert skipped.count(None) == 7 and "type_summary" in skipped and "type_file-history-snapshot" in skipped and "missing_session_ts_or_uuid" in skipped

    def test_thinking_off_by_default_and_opt_in(self, cfg):
        evs, _ = self.parse_all(cfg)
        assert not any(e["event_type"] == "reasoning" for e in evs)
        cfg.capture_thinking = True
        evs, _ = self.parse_all(cfg)
        assert [e["content"] for e in evs if e["event_type"] == "reasoning"] == ["private reasoning"]

    def test_tool_pairing_ids_and_bash_command(self, cfg):
        evs, _ = self.parse_all(cfg)
        call = next(e for e in evs if e["external_event_id"] == "tool:toolu_S1:call")
        res = next(e for e in evs if e["external_event_id"] == "tool:toolu_S1:result")
        assert call["tool"]["command"] == "alembic upgrade head" and call["content"] == "Bash: alembic upgrade head"
        assert res["parent_external_event_id"] == "tool:toolu_S1:call" and res["tool"]["is_error"] is True
        assert res["tool"]["tool_call_id"] == call["tool"]["tool_call_id"]

    def test_file_changes_and_list_content_results(self, cfg):
        evs, _ = self.parse_all(cfg)
        edit = next(e for e in evs if e["external_event_id"] == "tool:toolu_S2:call")
        assert edit["file_changes"][0]["operation"] == "edit" and "+op.add_column()" in edit["file_changes"][0]["diff"]
        assert edit["metadata"]["file_change_status"] == "requested"
        read = next(e for e in evs if e["external_event_id"] == "tool:toolu_S3:call")
        assert read["file_changes"] == [{"path": "/home/demo/code/demo-api/README.md", "operation": "read"}]
        assert next(e for e in evs if e["external_event_id"] == "tool:toolu_S2:result")["content"] == "File edited"

    def test_session_project_device_and_provenance(self, cfg):
        evs, _ = self.parse_all(cfg)
        e = evs[0]
        assert e["external_session_id"] == "11111111-aaaa-bbbb-cccc-000000000001" and e["device_id"] == "pc-arch"
        assert e["project"] == {"key": "/home/demo/code/demo-api", "name": "demo-api"}
        assert e["raw_payload_ref"] == "file:///t.jsonl#L2" and e["raw_payload"]["uuid"] == "u1"
        assert evs[1]["metadata"]["model"] == "claude-synthetic" and evs[1]["metadata"]["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_project_is_the_git_root_not_the_subdirectory(self, tmp_path, monkeypatch):
        from vibevault.collectors.claude_code import project_ref, project_root

        project_root.cache_clear()
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = tmp_path / "code" / "Alpha"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "deep").mkdir(parents=True)
        (tmp_path / "loose" / "dir").mkdir(parents=True)
        (tmp_path / ".git").mkdir()  # a dotfiles repo in ~ must not swallow folders below it
        assert project_ref(str(repo / "src" / "deep")) == {"key": str(repo), "name": "Alpha"}
        assert project_root(str(repo)) == str(repo)
        assert project_root(str(tmp_path / "loose" / "dir")) == str(tmp_path / "loose" / "dir")
        assert project_root("/nonexistent/path/x") == "/nonexistent/path/x"

    def test_truncation_is_flagged(self, cfg):
        cfg.max_content_chars = 10
        evs, _ = self.parse_all(cfg)
        assert evs[0]["metadata"]["truncated"] is True and evs[0]["metadata"]["original_length"] > 10

    @pytest.mark.parametrize("obj", [[], "str", {"type": "assistant"}, {"type": "assistant", "uuid": "x", "sessionId": "s", "timestamp": "t", "message": "oops"}, {"type": "user", "uuid": "x", "sessionId": "s", "timestamp": "t", "message": {"content": [1, None, {"type": "tool_result"}]}}])
    def test_malformed_never_raises(self, cfg, obj):
        parse_line(obj, cfg, path="/t", line_no=1)


class TestRedactionBeforeSpool:
    def test_secrets_never_reach_the_spool_files(self, cfg, spool):
        tmp = cfg.spool_dir.parent / "t.jsonl"
        shutil.copy(FIXTURE, tmp)
        scan_transcript(str(tmp), cfg, spool, Redactor())
        spool.db.execute("PRAGMA wal_checkpoint(FULL)")
        blob = b"".join(p.read_bytes() for p in cfg.spool_dir.iterdir())
        assert GH.encode() not in blob and b"supersecretvalue123" not in blob
        payload = [p for _, _, p in spool.next_batch("home", 100)]
        assert payload[0]["redaction"]["status"] == "redacted" and payload[0]["redaction"]["rules"]["github_token"] >= 1
        assert "[REDACTED:github_token]" in payload[0]["content"] and "[REDACTED:github_token]" in json.dumps(payload[0]["raw_payload"])

    def test_spool_files_are_private(self, cfg, spool):
        assert oct(spool.path.stat().st_mode & 0o777) == "0o600" and oct(cfg.spool_dir.stat().st_mode & 0o777) == "0o700"


class TestIncrementalScan:
    def test_partial_line_waits_and_nothing_duplicates(self, cfg, spool, tmp_path):
        f = tmp_path / "live.jsonl"
        ls = FIXTURE.read_text().splitlines(keepends=True)
        f.write_text("".join(ls[:3]) + ls[3][:40])  # 3 full lines + a partial 4th
        s1 = scan_transcript(str(f), cfg, spool, Redactor())
        assert s1.lines == 3
        with open(f, "a") as fh:
            fh.write(ls[3][40:] + "".join(ls[4:]))
        s2 = scan_transcript(str(f), cfg, spool, Redactor())
        assert s2.lines == len(ls) - 3
        assert scan_transcript(str(f), cfg, spool, Redactor()).lines == 0  # offset advanced
        total = spool.outbox_count()
        shutil.copy(f, tmp_path / "dup.jsonl")  # rescanning identical content under a new path → local dedupe
        scan_transcript(str(tmp_path / "dup.jsonl"), cfg, spool, Redactor())
        assert spool.outbox_count() == total

    def test_truncated_file_rescans_from_start(self, cfg, spool, tmp_path):
        f = tmp_path / "t.jsonl"
        shutil.copy(FIXTURE, f)
        scan_transcript(str(f), cfg, spool, Redactor())
        f.write_text(FIXTURE.read_text().splitlines(keepends=True)[1])
        assert scan_transcript(str(f), cfg, spool, Redactor()).lines == 1

    def test_missing_file_is_not_fatal(self, cfg, spool):
        scan_transcript("/nonexistent.jsonl", cfg, spool, Redactor())


class TestHooksAndControls:
    def hook(self, cfg, spool, **payload):
        return handle_hook(json.dumps({"session_id": "S", "cwd": "/home/me/proj", "transcript_path": "/x.jsonl", **payload}), cfg, spool, Redactor())

    def test_session_start_and_end_events(self, cfg, spool):
        assert self.hook(cfg, spool, hook_event_name="SessionStart", source="startup", model="m") == "ok"
        assert self.hook(cfg, spool, hook_event_name="SessionEnd", reason="clear") == "ok"
        evs = [p for _, _, p in spool.next_batch("home", 10)]
        assert [e["event_type"] for e in evs] == ["session_start", "session_end"]
        assert evs[0]["metadata"]["model"] == "m" and evs[0]["project"]["key"] == "/home/me/proj"
        assert spool.take_notifications() == ["/x.jsonl"]

    def test_other_events_only_notify(self, cfg, spool):
        self.hook(cfg, spool, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "echo " + GH}, tool_response={"stdout": GH})
        assert spool.outbox_count() == 0 and spool.pending_notifications() == 1
        blob = spool.path.read_bytes() + (spool.path.with_suffix(".db-wal").read_bytes() if spool.path.with_suffix(".db-wal").exists() else b"")
        assert GH.encode() not in blob  # hook payload bodies are not persisted at all

    def test_disable_switch(self, cfg, spool):
        spool.disabled_marker.write_text("x")
        assert self.hook(cfg, spool, hook_event_name="SessionStart") == "disabled"
        assert spool.outbox_count() == 0 and spool.pending_notifications() == 0
        spool.disabled_marker.unlink()
        cfg.enabled = False
        assert self.hook(cfg, spool, hook_event_name="SessionStart") == "disabled"

    def test_path_exclusion_glob_and_marker_file(self, cfg, spool, tmp_path):
        cfg.exclude_paths = ["/home/me/private*"]
        assert self.hook(cfg, spool, hook_event_name="SessionStart", cwd="/home/me/private-notes") == "excluded"
        proj = tmp_path / "secret-proj" / "sub"
        proj.mkdir(parents=True)
        (tmp_path / "secret-proj" / ".vibevault-ignore").write_text("")
        assert is_excluded(str(proj), cfg)

    def test_session_exclusion_drops_queue_and_future_events(self, cfg, spool, tmp_path):
        f = tmp_path / "t.jsonl"
        shutil.copy(FIXTURE, f)
        scan_transcript(str(f), cfg, spool, Redactor())
        assert spool.outbox_count() > 0
        spool.exclude_session("11111111-aaaa-bbbb-cccc-000000000001")
        assert spool.outbox_count() == 0
        f2 = tmp_path / "t2.jsonl"
        shutil.copy(FIXTURE, f2)
        assert scan_transcript(str(f2), cfg, spool, Redactor()).queued == 0

    def test_garbage_stdin_is_harmless(self, cfg, spool):
        assert handle_hook("not json", cfg, spool, Redactor()) == "invalid_json"
        assert handle_hook("[1,2]", cfg, spool, Redactor()) == "invalid_payload"

    def test_cli_hook_prints_nothing_and_exits_zero(self, cfg, tmp_path, capsys, monkeypatch):
        import io

        from vibevault.collectors import cli

        conf = tmp_path / "c.toml"
        conf.write_text(f'[collector]\ndevice_id="d"\nspool_dir="{cfg.spool_dir}"\n[[destination]]\nname="home"\nurl="http://x.invalid"\ntoken_env="T"\n')
        monkeypatch.setattr(cli, "spawn_background_sync", lambda *_: None)
        for stdin in ('{"hook_event_name":"SessionStart","session_id":"s","cwd":"/x"}', "garbage", ""):
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
            assert cli.main(["--config", str(conf), "hook"]) == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""


class TestDelivery:
    def fill(self, cfg, spool, n=5):
        for i in range(n):
            spool.enqueue(["home"], {"external_event_id": f"e{i}", "external_session_id": "s", "source": "claude_code"})

    def sender(self, results):
        calls = []

        def send(url, token, events, timeout):
            calls.append(len(events))
            r = results.pop(0)
            return r(events) if callable(r) else r

        return send, calls

    def test_success_clears_outbox(self, cfg, spool, monkeypatch):
        monkeypatch.setenv("T", "tok")
        self.fill(cfg, spool)
        ok = lambda evs: SendResult(True, False, 200, {"results": [{"status": "created"}] * len(evs), "created": len(evs)})  # noqa: E731
        send, calls = self.sender([ok])
        st = flush(cfg, spool, send=send)
        assert st.created == 5 and spool.outbox_count() == 0

    def test_network_failure_keeps_events_and_backs_off(self, cfg, spool, monkeypatch):
        monkeypatch.setenv("T", "tok")
        self.fill(cfg, spool)
        now = [1000.0]
        send, calls = self.sender([SendResult(False, True, None, error="network")])
        flush(cfg, spool, now=lambda: now[0], send=send)
        assert spool.outbox_count() == 5 and spool.dest_state("home")["failures"] == 1
        flush(cfg, spool, now=lambda: now[0] + 1, send=send)  # still backing off: not even attempted
        assert calls == [5]
        now[0] += 3600
        ok = lambda evs: SendResult(True, False, 200, {"results": [{"status": "duplicate"}] * len(evs), "duplicate": len(evs)})  # noqa: E731
        send2, _ = self.sender([ok])
        flush(cfg, spool, now=lambda: now[0], send=send2)
        assert spool.outbox_count() == 0 and spool.dest_state("home")["failures"] == 0

    def test_413_halves_batch(self, cfg, spool, monkeypatch):
        monkeypatch.setenv("T", "tok")
        self.fill(cfg, spool, 4)
        ok = lambda evs: SendResult(True, False, 200, {"results": [{"status": "created"}] * len(evs), "created": len(evs)})  # noqa: E731
        send, calls = self.sender([SendResult(False, False, 413, error="http_413"), ok, ok])
        flush(cfg, spool, send=send)
        assert calls == [4, 2, 2] and spool.outbox_count() == 0

    def test_rejected_items_are_recorded_without_payload_and_not_retried(self, cfg, spool, monkeypatch):
        monkeypatch.setenv("T", "tok")
        self.fill(cfg, spool, 2)
        body = {"results": [{"status": "rejected", "reason": "validation_error"}, {"status": "created"}], "created": 1, "rejected": 1}
        flush(cfg, spool, send=self.sender([SendResult(True, False, 200, body)])[0])
        assert spool.outbox_count() == 0
        assert spool.db.execute("SELECT reason FROM rejected").fetchall() == [("validation_error",)]

    def test_auth_failure_and_missing_token(self, cfg, spool, monkeypatch):
        self.fill(cfg, spool, 1)
        monkeypatch.delenv("T", raising=False)
        st = flush(cfg, spool, send=lambda *a: pytest.fail("must not send without a token"))
        assert st.failed_dests == 1 and spool.dest_state("home")["last_error"] == "no_token_configured"
        monkeypatch.setenv("T", "tok")
        spool.set_dest_state("home", 0, 0, None)
        flush(cfg, spool, send=self.sender([SendResult(False, False, 401, error="http_401")])[0])
        assert spool.outbox_count() == 1 and spool.dest_state("home")["next_try"] > 0

    def test_token_file_must_be_private(self, tmp_path):
        p = tmp_path / "tok"
        p.write_text("abc")
        p.chmod(0o644)
        with pytest.raises(PermissionError):
            Destination("d", "u", token_file=str(p)).token()
        p.chmod(0o600)
        assert Destination("d", "u", token_file=str(p)).token() == "abc"

    def test_audit_log_never_contains_tokens(self, cfg, spool, monkeypatch):
        monkeypatch.setenv("T", "super-secret-token-value")
        self.fill(cfg, spool, 1)
        flush(cfg, spool, send=self.sender([SendResult(False, True, None, error="network")])[0])
        assert "super-secret-token-value" not in json.dumps(spool.db.execute("SELECT * FROM audit").fetchall())


class TestEndToEndWithServer:
    def test_two_independent_devices_into_one_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("T", INGEST)
        app = create_app(make_settings(tmp_path / "srv"))
        with TestClient(app) as srv:
            def send(url, token, events, timeout):
                r = srv.post("/api/v1/events/batch", json={"events": events}, headers={"Authorization": f"Bearer {token}"})
                return SendResult(r.status_code == 200, r.status_code >= 500, r.status_code, r.json() if r.status_code == 200 else None, None if r.status_code == 200 else f"http_{r.status_code}")

            totals = {}
            for dev in ("pc-arch", "server-ubuntu"):
                c = CollectorConfig(device_id=dev, spool_dir=tmp_path / dev, destinations=[Destination("home", "http://x", token_env="T")])
                sp = Spool(c.spool_dir)
                scan_transcript(str(FIXTURE), c, sp, Redactor())
                st = flush(c, sp, send=send)
                totals[dev] = st.created
                again = Spool(c.spool_dir)
                again.db.execute("DELETE FROM offsets")  # force a full re-read: simulates replay after a crash
                scan_transcript(str(FIXTURE), c, again, Redactor())
                assert flush(c, again, send=send).created == 0  # all duplicates
            assert totals["pc-arch"] == totals["server-ubuntu"] == 10
            h = {"Authorization": f"Bearer {ADMIN}"}
            devs = srv.get("/api/v1/devices", headers=h).json()["items"]
            assert {d["device_key"] for d in devs} == {"pc-arch", "server-ubuntu"}
            assert srv.get("/api/v1/sessions", headers=h).json()["total"] == 2  # same external id, separate devices
            hits = srv.get("/api/v1/search?q=owner_id", headers=h).json()
            assert hits["total"] == 2 and srv.get(f"/api/v1/search?q={GH[:12]}", headers=h).json()["total"] == 0
            sess = srv.get("/api/v1/sessions/1/events", headers=h).json()["items"]
            call = next(e for e in sess if e["tool_call_id"] == "toolu_S1" and e["event_type"] == "tool_call")
            assert call["tool"]["output"].startswith("sqlalchemy.exc") and call["tool"]["is_error"] is True
            assert [e["event_type"] for e in sess][:2] == ["message", "message"]
            assert sqlite3  # noqa
