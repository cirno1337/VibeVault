"""`vibevault-collector`: hook | scan | flush | sync | backfill | status | enable | disable | exclude-session."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from ..redaction import Redactor
from .claude_code import (
    collection_allowed,
    discover_transcripts,
    handle_hook,
    scan_transcript,
    spawn_background_sync,
)
from .config import CollectorConfig
from .delivery import flush
from .spool import Spool


def _lock():
    f = open(Path(os.environ.get("TMPDIR", "/tmp")) / f"vibevault-collector-{os.getuid()}.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def run_sync(cfg: CollectorConfig, spool: Spool, redactor: Redactor, paths: list[str] | None = None) -> dict:
    """Drain notifications → scan transcripts → deliver. Loops until nothing new arrives."""
    out = {"events_queued": 0, "sent": 0}
    if not collection_allowed(cfg, spool):
        return {**out, "disabled": True}
    for _ in range(10):
        targets = set(paths or []) | set(spool.take_notifications()) | set(spool.tracked_paths())
        for p in sorted(targets):
            out["events_queued"] += scan_transcript(p, cfg, spool, redactor).queued
        paths = None
        out["sent"] += flush(cfg, spool).sent
        if not spool.pending_notifications():
            break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vibevault-collector")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="Claude Code hook entrypoint (reads JSON on stdin; prints nothing)")
    for n in ("scan", "flush", "status", "enable", "disable"):
        sub.add_parser(n)
    s = sub.add_parser("sync", help="scan tracked/notified transcripts and deliver")
    s.add_argument("--quiet", action="store_true")
    b = sub.add_parser("backfill", help="explicitly import existing transcripts")
    b.add_argument("--since-days", type=float, default=None)
    b.add_argument("--dry-run", action="store_true")
    x = sub.add_parser("exclude-session", help="stop collecting a session and drop queued events")
    x.add_argument("session_id")
    a = ap.parse_args(argv)

    if a.cmd == "hook":
        # Hooks must never block, fail, or write to stdout/stderr.
        try:
            cfg = CollectorConfig.load(a.config)
            spool = Spool(cfg.spool_dir)
            result = handle_hook(sys.stdin.read(), cfg, spool, Redactor.from_file(cfg.redaction_rules_file))
            spool.close()
            if result == "ok":
                spawn_background_sync(a.config)
        except Exception:  # noqa: BLE001
            pass
        return 0

    cfg = CollectorConfig.load(a.config)
    spool = Spool(cfg.spool_dir)
    redactor = Redactor.from_file(cfg.redaction_rules_file)

    if a.cmd == "disable":
        spool.disabled_marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        spool.audit("disable")
        print("collection disabled (hooks and sync do nothing; queued events are kept)")
    elif a.cmd == "enable":
        spool.disabled_marker.unlink(missing_ok=True)
        spool.audit("enable")
        print("collection enabled")
    elif a.cmd == "exclude-session":
        n = spool.exclude_session(a.session_id)
        print(f"session excluded; dropped {n} queued events. Events already delivered must be deleted on the server (DELETE /api/v1/sessions/<id>).")
    elif a.cmd in ("sync", "scan", "flush", "backfill"):
        lock = _lock()
        if lock is None:
            if not getattr(a, "quiet", False):
                print("another collector run is active")
            return 0
        if a.cmd == "scan":
            for p in sorted(set(spool.take_notifications()) | set(spool.tracked_paths())):
                scan_transcript(p, cfg, spool, redactor)
        elif a.cmd == "flush":
            print(json.dumps(flush(cfg, spool).__dict__))
        elif a.cmd == "backfill":
            paths = discover_transcripts(cfg, a.since_days)
            if a.dry_run:
                print(f"{len(paths)} transcript files would be imported")
            else:
                print(json.dumps(run_sync(cfg, spool, redactor, paths)))
        else:
            res = run_sync(cfg, spool, redactor)
            if not a.quiet:
                print(json.dumps(res))
    elif a.cmd == "status":
        rows = spool.db.execute("SELECT action, detail, ts FROM audit ORDER BY id DESC LIMIT 5").fetchall()
        print(json.dumps({
            "enabled": cfg.enabled and not spool.is_disabled(),
            "device_id": cfg.device_id,
            "destinations": [{"name": d.name, "url": d.url, "token_configured": bool(d.token()), **{k: v for k, v in spool.dest_state(d.name).items() if k in ("failures", "last_error")}, "queued": spool.outbox_count(d.name)} for d in cfg.destinations],
            "tracked_transcripts": len(spool.tracked_paths()),
            "rejected_total": spool.db.execute("SELECT COUNT(*) FROM rejected").fetchone()[0],
            "recent_audit": [{"action": r[0], "detail": json.loads(r[1]), "ts": r[2]} for r in rows],
        }, indent=2))
    spool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
