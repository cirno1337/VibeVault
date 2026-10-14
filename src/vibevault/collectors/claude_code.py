"""Claude Code collector: transcript parsing + hook handling.

What is VERIFIED against official documentation (code.claude.com/docs/en/hooks), fetched while
building this:
  * hooks receive JSON on stdin with common fields session_id, transcript_path, cwd,
    hook_event_name (+ event specific ones, e.g. SessionStart.source/model);
  * transcript_path "may lag the in-memory conversation" (written asynchronously);
  * SessionStart / UserPromptSubmit stdout is not safe to use (can be injected into context),
    so the hook never prints.
What is NOT verified here: the transcript JSONL line schema is not part of the documented
contract. This parser follows the format observed in practice (type/uuid/parentUuid/sessionId/
timestamp/cwd/message{role,content[]}) and is deliberately defensive: unknown line types or
shapes are skipped and counted, never fatal. Fixtures in tests/fixtures are SYNTHETIC.
Re-validate against real files before relying on it (docs/collectors/claude-code.md).
"""

from __future__ import annotations

import fcntl
import fnmatch
import functools
import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..redaction import RedactionReport, Redactor
from .config import CollectorConfig
from .spool import Spool

EDIT_TOOLS = {"Edit": "edit", "MultiEdit": "edit", "Write": "create", "NotebookEdit": "edit", "Read": "read"}


@dataclass
class ScanStats:
    lines: int = 0
    events: int = 0
    queued: int = 0
    skipped: Counter = field(default_factory=Counter)


# ----------------------------------------------------------------------------- parsing
def _text_of(content: Any) -> str:
    """Flatten tool_result content (string or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, dict):
                parts.append(f"[{b.get('type', 'block')} omitted]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)


def _diff_for(tool: str, inp: dict) -> str | None:
    if tool == "Edit" and "old_string" in inp:
        return "".join(f"-{ln}\n" for ln in str(inp["old_string"]).splitlines()) + "".join(f"+{ln}\n" for ln in str(inp.get("new_string", "")).splitlines())
    if tool == "MultiEdit":
        out = []
        for e in inp.get("edits") or []:
            if isinstance(e, dict):
                out.append("".join(f"-{ln}\n" for ln in str(e.get("old_string", "")).splitlines()) + "".join(f"+{ln}\n" for ln in str(e.get("new_string", "")).splitlines()))
        return "@@\n".join(out) or None
    if tool == "Write":
        return "".join(f"+{ln}\n" for ln in str(inp.get("content", "")).splitlines())
    return None


def parse_line(obj: dict, cfg: CollectorConfig, *, path: str, line_no: int) -> tuple[list[dict], str | None]:
    """Convert one transcript record into normalized events. Returns (events, skip_reason)."""
    if not isinstance(obj, dict):
        return [], "not_object"
    typ = obj.get("type")
    sid, ts, uuid = obj.get("sessionId"), obj.get("timestamp"), obj.get("uuid")
    if typ not in ("user", "assistant", "system"):
        return [], f"type_{typ}" if typ else "no_type"
    if not (sid and ts and uuid):
        return [], "missing_session_ts_or_uuid"
    cwd = obj.get("cwd")
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    meta_base = {
        "line_uuid": uuid, "parent_line_uuid": obj.get("parentUuid"), "git_branch": obj.get("gitBranch"),
        "client_version": obj.get("version"), "is_sidechain": obj.get("isSidechain", False),
        "is_meta": obj.get("isMeta", False), "cwd": cwd,
    }
    if msg.get("model"):
        meta_base["model"] = msg["model"]
    if isinstance(msg.get("usage"), dict):
        meta_base["usage"] = {k: v for k, v in msg["usage"].items() if isinstance(v, int)}
    meta_base = {k: v for k, v in meta_base.items() if v is not None}

    def make(idx: int, etype: str, actor: str, content: str, **extra) -> dict:
        truncated = len(content) > cfg.max_content_chars
        md = {**meta_base, **extra.pop("metadata", {})}
        if truncated:
            md.update(truncated=True, original_length=len(content))
            content = content[: cfg.max_content_chars] + "\n…[truncated by collector]"
        e = {
            "source": "claude_code", "device_id": cfg.device_id, "external_session_id": sid,
            "external_event_id": f"{uuid}:{idx}", "event_type": etype, "actor": actor, "occurred_at": ts,
            "sequence": line_no * 1000 + idx, "content": content, "metadata": md,
            "raw_payload_ref": f"file://{path}#L{line_no}",
        }
        if cfg.device_label:
            e["device_label"] = cfg.device_label
        if cwd:
            e["project"] = project_ref(cwd)
        e.update(extra)
        return e

    events: list[dict] = []
    if typ == "system":
        text = obj.get("content") if isinstance(obj.get("content"), str) else _text_of(msg.get("content"))
        if text:
            events.append(make(0, "system", "system", text, metadata={"subtype": obj.get("subtype")}))
    else:
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content if isinstance(content, list) else [])
        actor = "user" if typ == "user" else "assistant"
        for i, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                if b.get("text"):
                    events.append(make(i, "message", actor, str(b["text"])))
            elif bt == "thinking":
                if cfg.capture_thinking and b.get("thinking"):
                    events.append(make(i, "reasoning", "assistant", str(b["thinking"])))
            elif bt == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                name, tid = str(b.get("name", "")), str(b.get("id", ""))
                if not tid:
                    continue
                cmd = inp.get("command") if name == "Bash" and isinstance(inp.get("command"), str) else None
                tool = {"tool_call_id": tid, "name": name, "input": inp}
                if cmd:
                    tool["command"] = cmd
                summary = cmd or inp.get("file_path") or inp.get("notebook_path") or inp.get("pattern") or inp.get("url") or inp.get("description") or ""
                e = make(i, "tool_call", "assistant", f"{name}: {summary}".strip(": "), tool=tool)
                e["external_event_id"] = f"tool:{tid}:call"
                path_ = inp.get("file_path") or inp.get("notebook_path")
                if name in EDIT_TOOLS and isinstance(path_, str):
                    e["file_changes"] = [{"path": path_, "operation": EDIT_TOOLS[name], **({"diff": d} if (d := _diff_for(name, inp)) else {})}]
                    e["metadata"]["file_change_status"] = "requested"  # outcome is on the matching tool_result
                events.append(e)
            elif bt == "tool_result":
                tid = str(b.get("tool_use_id", ""))
                if not tid:
                    continue
                out = _text_of(b.get("content"))
                tur = obj.get("toolUseResult") if isinstance(obj.get("toolUseResult"), dict) else {}
                tool = {"tool_call_id": tid, "output": out, "is_error": bool(b.get("is_error", False))}
                if isinstance(tur.get("exitCode"), int):
                    tool["exit_code"] = tur["exitCode"]
                md = {}
                if tur.get("interrupted"):
                    md["interrupted"] = True
                e = make(i, "tool_result", "tool", out, tool=tool, metadata=md)
                e["external_event_id"] = f"tool:{tid}:result"
                e["parent_external_event_id"] = f"tool:{tid}:call"
                events.append(e)
            elif bt in ("image", "document"):
                events.append(make(i, "message", actor, f"[{bt} omitted by collector]", metadata={"omitted": bt}))
    if events and cfg.include_raw_payload:
        raw = json.dumps(obj, ensure_ascii=False)
        if len(raw) <= cfg.max_raw_payload_chars:
            events[0]["raw_payload"] = obj
        else:
            events[0]["metadata"]["raw_payload_omitted"] = "too_large"
    return events, None if events else "no_events"


def redact_event(ev: dict, redactor: Redactor) -> dict:
    """Redact in place of the whole normalized event before it touches the spool or network."""
    report = RedactionReport()
    out = redactor.redact_obj(ev, report)
    out["redaction"] = {"status": report.status, "count": report.total, "rules": report.as_dict()}
    return out


# ----------------------------------------------------------------------------- policy
def is_excluded(cwd: str | None, cfg: CollectorConfig) -> bool:
    if not cwd:
        return False
    if any(fnmatch.fnmatch(cwd, pat) for pat in cfg.exclude_paths):
        return True
    p = Path(cwd)
    for d in (p, *p.parents):
        if (d / ".vibevault-ignore").exists():
            return True
    return False


def collection_allowed(cfg: CollectorConfig, spool: Spool) -> bool:
    return cfg.enabled and not spool.is_disabled()


# ----------------------------------------------------------------------------- scanning
def scan_transcript(path: str, cfg: CollectorConfig, spool: Spool, redactor: Redactor) -> ScanStats:
    """Read new complete lines since the last offset; queue redacted events; advance offset atomically."""
    stats = ScanStats()
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        spool.audit("scan_missing_file")
        return stats
    inode, offset, line_no = spool.get_offset(path)
    if inode is not None and (inode != st.st_ino or st.st_size < offset):
        offset, line_no = 0, 0  # rotated or truncated: start over (server dedupes by event identity)
    dests = [d.name for d in cfg.destinations]
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        spool.set_offset(path, st.st_ino, offset, line_no)
        return stats
    complete = data[: end + 1]
    with spool.transaction():
        for raw in complete.split(b"\n")[:-1]:
            line_no += 1
            if not raw.strip():
                continue
            stats.lines += 1
            try:
                obj = json.loads(raw)
            except ValueError:
                stats.skipped["invalid_json"] += 1
                continue
            events, reason = parse_line(obj, cfg, path=path, line_no=line_no)
            if reason:
                stats.skipped[reason] += 1
                continue
            sid = events[0]["external_session_id"]
            if is_excluded(obj.get("cwd"), cfg) or spool.session_excluded(sid):
                stats.skipped["excluded"] += 1
                continue
            for e in events:
                stats.events += 1
                stats.queued += spool.enqueue(dests, redact_event(e, redactor))
        spool.set_offset(path, st.st_ino, offset + len(complete), line_no)
        spool.audit("scan", file=p.name, lines=stats.lines, events=stats.events, queued=stats.queued, skipped=dict(stats.skipped))
    return stats


def discover_transcripts(cfg: CollectorConfig, since_days: float | None = None) -> list[str]:
    root = cfg.projects_dir
    if not root.exists():
        return []
    cutoff = time.time() - since_days * 86400 if since_days else 0
    return sorted(str(p) for p in root.rglob("*.jsonl") if p.stat().st_mtime >= cutoff)


# ----------------------------------------------------------------------------- hooks
def hook_events(payload: dict, cfg: CollectorConfig) -> list[dict]:
    """Events derivable from the hook payload alone (session boundaries). Content comes from transcripts."""
    name, sid = payload.get("hook_event_name"), payload.get("session_id")
    if not sid or name not in ("SessionStart", "SessionEnd"):
        return []
    cwd = payload.get("cwd")
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int((now % 1) * 1000):03d}+00:00"
    keep = {k: payload[k] for k in ("source", "model", "reason", "permission_mode", "session_title") if k in payload}
    e = {
        "source": "claude_code", "device_id": cfg.device_id, "external_session_id": sid,
        "external_event_id": f"hook:{sid}:{name}:{payload.get('prompt_id') or int(now * 1000)}",
        "event_type": "session_start" if name == "SessionStart" else "session_end", "actor": "system",
        "occurred_at": iso, "content": f"{name}" + (f" ({payload['source']})" if payload.get("source") else ""),
        "metadata": {**keep, "origin": "hook", **({"cwd": cwd} if cwd else {})},
    }
    if cfg.device_label:
        e["device_label"] = cfg.device_label
    if cwd:
        e["project"] = project_ref(cwd)
    return [e]


@functools.lru_cache(maxsize=512)
def project_root(cwd: str) -> str:
    """The git work tree containing ``cwd``, else ``cwd`` itself.

    Claude often ``cd``s into subdirectories mid-session; keying projects by the raw cwd made every
    subdirectory its own project. The home directory never counts as a root for paths below it (a
    dotfiles repo in ~ must not swallow every non-repo folder).
    """
    path, home = Path(cwd), Path.home()
    for d in (path, *path.parents):
        if d == home and path != home:
            break
        if (d / ".git").exists():
            return str(d)
    return cwd


def project_ref(cwd: str) -> dict[str, str]:
    root = project_root(cwd)
    return {"key": root, "name": Path(root).name or root}


def handle_hook(stdin_text: str, cfg: CollectorConfig, spool: Spool, redactor: Redactor) -> str:
    """Fast path: parse, apply policy, write to the local spool, return. No network, no scanning."""
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        spool.audit("hook_ignored", reason="invalid_json")
        return "invalid_json"
    if not isinstance(payload, dict):
        return "invalid_payload"
    if not collection_allowed(cfg, spool):
        return "disabled"
    if is_excluded(payload.get("cwd"), cfg) or (payload.get("session_id") and spool.session_excluded(payload["session_id"])):
        spool.audit("hook_ignored", reason="excluded", event=payload.get("hook_event_name"))
        return "excluded"
    dests = [d.name for d in cfg.destinations]
    with spool.transaction():
        for e in hook_events(payload, cfg):
            spool.enqueue(dests, redact_event(e, redactor))
        spool.notify(payload.get("transcript_path"))
        spool.audit("hook", event=payload.get("hook_event_name"))
    return "ok"


def spawn_background_sync(config_path: str | None) -> None:
    """Start `vibevault-collector sync` detached, unless a runner already holds the lock."""
    try:
        lock = open(Path(os.environ.get("TMPDIR", "/tmp")) / f"vibevault-collector-{os.getuid()}.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # a sync is running; it will pick up our notification
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        cmd = [sys.executable, "-m", "vibevault.collectors.cli"] + (["--config", config_path] if config_path else []) + ["sync", "--quiet"]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)  # noqa: S603
    except Exception:  # noqa: BLE001 - a hook must never fail Claude Code
        pass
