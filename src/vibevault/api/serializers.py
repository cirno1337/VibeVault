from __future__ import annotations

from typing import Any

from .. import models as m


def iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def project_out(p: m.Project, **extra) -> dict[str, Any]:
    return {
        "id": p.id, "slug": p.slug, "name": p.name, "description": p.description,
        "collection_enabled": p.collection_enabled, "ai_processing_enabled": p.ai_processing_enabled,
        "aliases": [a.alias for a in p.aliases], "created_at": iso(p.created_at), **extra,
    }


def session_out(s: m.Session, **extra) -> dict[str, Any]:
    return {
        "id": s.id, "external_session_id": s.external_session_id, "title": s.title,
        "source": s.source.kind, "device": {"id": s.device_id, "key": s.device.device_key, "label": s.device.label},
        "project": {"id": s.project.id, "name": s.project.name} if s.project else None,
        "project_hint": s.project_hint, "started_at": iso(s.started_at), "last_event_at": iso(s.last_event_at),
        "ended_at": iso(s.ended_at), "status": s.status, "excluded": s.excluded, "event_count": s.event_count, **extra,
    }


def tool_out(t: m.ToolExecution | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {
        "tool_call_id": t.tool_call_id, "name": t.tool_name, "command": t.command, "input": t.input,
        "output": t.output, "exit_code": t.exit_code, "is_error": t.is_error,
        "call_event_id": t.call_event_id, "result_event_id": t.result_event_id,
        "started_at": iso(t.started_at), "finished_at": iso(t.finished_at),
    }


def file_change_out(f: m.FileChange) -> dict[str, Any]:
    return {"path": f.path, "operation": f.operation, "old_path": f.old_path, "diff": f.diff,
            "before_hash": f.before_hash, "after_hash": f.after_hash}


def event_out(e: m.Event, *, raw: bool = False, tool: m.ToolExecution | None = None,
              file_changes: list[m.FileChange] | None = None) -> dict[str, Any]:
    d = {
        "id": e.id, "schema_version": e.schema_version, "source": e.source.kind,
        "device": {"id": e.device_id, "key": e.device.device_key, "label": e.device.label},
        "session_id": e.session_id, "external_session_id": e.external_session_id,
        "external_event_id": e.external_event_id, "revision": e.revision, "is_current": e.is_current,
        "project_id": e.project_id, "project_hint": e.project_hint, "event_type": e.event_type,
        "actor": e.actor, "occurred_at": iso(e.occurred_at), "ingested_at": iso(e.ingested_at),
        "ingested_by": e.ingested_by, "sequence": e.sequence, "ingest_seq": e.seq, "content": e.content,
        "metadata": e.event_metadata, "parent_external_event_id": e.parent_external_event_id,
        "tool_call_id": e.tool_call_id,
        "redaction": {"status": e.redaction_status, "count": e.redaction_count, "rules": e.redaction_rules},
        "processing_status": e.processing_status, "raw_payload_ref": e.raw_payload_ref,
        "has_raw_payload": e.raw_payload is not None,
        "tool": tool_out(tool), "file_changes": [file_change_out(f) for f in (file_changes or [])],
    }
    if raw:
        d["raw_payload"] = e.raw_payload
    return d


def derived_out(i: m.DerivedItem) -> dict[str, Any]:
    return {
        "id": i.id, "kind": i.kind, "origin": i.origin, "epistemic_status": i.epistemic_status,
        "state": i.state, "title": i.title, "body": i.body, "data": i.data, "project_id": i.project_id,
        "session_id": i.session_id, "generation_id": i.generation_id, "provider": i.provider,
        "model": i.model, "prompt_version": i.prompt_version, "confidence": i.confidence,
        "created_at": iso(i.created_at), "superseded_at": iso(i.superseded_at),
        "confirmed_by": i.confirmed_by, "confirmed_at": iso(i.confirmed_at),
        "evidence": [{"event_id": ev.event_id, "note": ev.note} for ev in i.evidence],
        "interpretation": i.origin != "deterministic",
    }


def job_out(j: m.Job) -> dict[str, Any]:
    return {
        "id": j.id, "kind": j.kind, "status": j.status, "dedupe_key": j.dedupe_key, "payload": j.payload,
        "checkpoint": j.checkpoint, "result": j.result, "attempts": j.attempts, "max_attempts": j.max_attempts,
        "run_after": iso(j.run_after), "last_error": j.last_error, "last_error_at": iso(j.last_error_at),
        "status_note": j.status_note, "created_at": iso(j.created_at), "started_at": iso(j.started_at),
        "finished_at": iso(j.finished_at),
    }
