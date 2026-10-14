"""`vibevault` command line: serve, migrate, worker, backup, restore, prune, seed, purge-synthetic."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.engine import make_url


def _sqlite_path(url: str) -> Path:
    u = make_url(url)
    if u.get_backend_name() != "sqlite" or not u.database:
        raise SystemExit("this command supports SQLite databases only")
    return Path(u.database).expanduser()


def cmd_serve(a):
    import uvicorn

    uvicorn.run("vibevault.app:create_app_from_env", factory=True, host=a.host, port=a.port, log_config=None)


def cmd_migrate(a):
    from .config import get_settings
    from .db import run_migrations

    run_migrations(get_settings().database_url)
    print("migrations applied")


def cmd_worker(a):
    """Run only the queue worker (use with VIBEVAULT_WORKER_ENABLED=false on the API process)."""
    import time

    from .config import get_settings
    from .db import make_engine, make_session_factory
    from .logging_setup import configure_logging
    from .providers import ProviderRegistry
    from .worker import Worker

    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    w = Worker(s, make_session_factory(make_engine(s.database_url)), ProviderRegistry(s))
    w.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        w.stop()


def cmd_backup(a):
    """Online, consistent backup using SQLite's backup API (safe while the server runs)."""
    from .config import get_settings

    src = _sqlite_path(get_settings().database_url)
    dest = Path(a.dest)
    if dest.is_dir() or a.dest.endswith(("/", os.sep)):
        dest = dest / f"vibevault-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s_conn, sqlite3.connect(dest) as d_conn:
        s_conn.backup(d_conn)
    with sqlite3.connect(dest) as c:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    os.chmod(dest, 0o600)
    print(f"backup written: {dest} (integrity_check={ok})")
    if ok != "ok":
        raise SystemExit(1)


def cmd_restore(a):
    """Replace the database with a backup. Stop the server first."""
    from .config import get_settings

    dest = _sqlite_path(get_settings().database_url)
    src = Path(a.src)
    with sqlite3.connect(src) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("backup failed integrity_check; refusing to restore")
    if dest.exists():
        keep = dest.with_suffix(dest.suffix + f".pre-restore-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        shutil.copy2(dest, keep)
        print(f"existing database saved to {keep}")
    for ext in ("-wal", "-shm"):
        Path(str(dest) + ext).unlink(missing_ok=True)
    shutil.copy2(src, dest)
    os.chmod(dest, 0o600)
    print(f"restored {src} -> {dest}; start the server to apply any newer migrations")


def cmd_prune(a):
    """Delete events older than N days (retention). Dry-run unless --yes. Never touches newer data."""
    from . import models as m
    from .config import get_settings
    from .db import make_engine, make_session_factory

    cutoff = datetime.now(UTC) - timedelta(days=a.older_than_days)
    s = get_settings()
    sf = make_session_factory(make_engine(s.database_url))
    with sf() as db:
        n = len(db.scalars(select(m.Event.seq).where(m.Event.occurred_at < cutoff)).all())
        print(f"{n} events older than {cutoff:%Y-%m-%d}")
        if not a.yes:
            print("dry run; pass --yes to delete")
            return
        db.execute(delete(m.Event).where(m.Event.occurred_at < cutoff))
        orphan = db.execute(delete(m.DerivedItem).where(m.DerivedItem.origin == "model", ~m.DerivedItem.id.in_(select(m.DerivedEvidence.derived_id)))).rowcount
        db.add(m.AuditLog(actor="cli", action="prune", detail={"events": n, "older_than_days": a.older_than_days, "orphan_derived_removed": orphan}))
        db.commit()
        print(f"deleted {n} events and {orphan} derived items left without evidence")


def cmd_seed(a):
    from .collectors.client import post_batch
    from .synthetic import build

    token = os.environ.get("VIBEVAULT_TOKEN") or os.environ.get("VIBEVAULT_ADMIN_TOKEN")
    if not token:
        raise SystemExit("set VIBEVAULT_TOKEN (an ingest or admin token)")
    events = build()
    res = post_batch(a.url, token, events)
    if not res.ok:
        raise SystemExit(f"seeding failed: {res.error}")
    b = res.body or {}
    print(json.dumps({k: b.get(k) for k in ("received", "created", "duplicate", "conflict", "rejected", "skipped")}))


def cmd_purge_synthetic(a):
    from . import models as m
    from .config import get_settings
    from .db import make_engine, make_session_factory

    sf = make_session_factory(make_engine(get_settings().database_url))
    with sf() as db:
        src = db.scalar(select(m.Source).where(m.Source.kind == "synthetic"))
        if src is None:
            print("no synthetic data")
            return
        n = db.execute(delete(m.Event).where(m.Event.source_id == src.id)).rowcount
        db.execute(delete(m.Session).where(m.Session.source_id == src.id))
        db.execute(delete(m.ToolExecution).where(m.ToolExecution.source_id == src.id))
        db.commit()
        print(f"removed {n} synthetic events")


def main(argv=None):
    p = argparse.ArgumentParser(prog="vibevault")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the API + UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    sub.add_parser("worker", help="run only the background worker").set_defaults(fn=cmd_worker)
    b = sub.add_parser("backup", help="consistent online SQLite backup")
    b.add_argument("dest")
    b.set_defaults(fn=cmd_backup)
    r = sub.add_parser("restore", help="restore a backup (server must be stopped)")
    r.add_argument("src")
    r.set_defaults(fn=cmd_restore)
    pr = sub.add_parser("prune", help="delete events older than N days (retention)")
    pr.add_argument("--older-than-days", type=int, required=True)
    pr.add_argument("--yes", action="store_true")
    pr.set_defaults(fn=cmd_prune)
    sd = sub.add_parser("seed", help="POST the synthetic dataset to a running server")
    sd.add_argument("--url", default="http://127.0.0.1:8787")
    sd.set_defaults(fn=cmd_seed)
    sub.add_parser("purge-synthetic", help="remove synthetic dataset").set_defaults(fn=cmd_purge_synthetic)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main(sys.argv[1:])
