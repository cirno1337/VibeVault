"""Durable local spool (SQLite, stdlib). Hooks only write here; delivery happens asynchronously.

Tables: outbox (events awaiting delivery, per destination), offsets (incremental transcript
position), notifications (hook-triggered scan requests), dest_state (backoff), excluded_sessions,
audit (local operational log: counts and ids only, never content or tokens).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, dest TEXT NOT NULL, event_key TEXT NOT NULL,
  payload TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(dest, event_key));
CREATE TABLE IF NOT EXISTS offsets(path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER NOT NULL DEFAULT 0,
  line_no INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, transcript_path TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dest_state(dest TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
  next_try REAL NOT NULL DEFAULT 0, last_error TEXT, batch_size INTEGER);
CREATE TABLE IF NOT EXISTS excluded_sessions(session_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rejected(id INTEGER PRIMARY KEY, dest TEXT, event_key TEXT, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
"""


class Spool:
    def __init__(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.dir = directory
        self.path = directory / "spool.db"
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.db.close()

    # --- generic helpers -----------------------------------------------------
    def audit(self, action: str, **detail) -> None:
        self.db.execute("INSERT INTO audit(ts, action, detail) VALUES (?,?,?)", (time.time(), action, json.dumps(detail)))

    def transaction(self):
        return _Tx(self.db)

    # --- controls ------------------------------------------------------------
    @property
    def disabled_marker(self) -> Path:
        return self.dir / "DISABLED"

    def is_disabled(self) -> bool:
        return self.disabled_marker.exists()

    def session_excluded(self, session_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM excluded_sessions WHERE session_id=?", (session_id,)).fetchone() is not None

    def exclude_session(self, session_id: str) -> int:
        """Exclude future events and drop anything already queued for the session."""
        self.db.execute("INSERT OR IGNORE INTO excluded_sessions VALUES (?,?)", (session_id, time.time()))
        n = self.db.execute("DELETE FROM outbox WHERE json_extract(payload,'$.external_session_id') = ?", (session_id,)).rowcount
        self.audit("exclude_session", session_id=session_id, dropped_from_outbox=n)
        return n

    # --- outbox ---------------------------------------------------------------
    def enqueue(self, dests: list[str], event: dict) -> int:
        key = f"{event['external_event_id']}#{event.get('revision', 1)}"
        payload = json.dumps(event, ensure_ascii=False)
        n = 0
        for d in dests:
            n += self.db.execute("INSERT OR IGNORE INTO outbox(dest,event_key,payload,created_at) VALUES (?,?,?,?)", (d, key, payload, time.time())).rowcount
        return n

    def outbox_count(self, dest: str | None = None) -> int:
        q, a = ("SELECT COUNT(*) FROM outbox WHERE dest=?", (dest,)) if dest else ("SELECT COUNT(*) FROM outbox", ())
        return self.db.execute(q, a).fetchone()[0]

    def next_batch(self, dest: str, limit: int) -> list[tuple[int, str, dict]]:
        rows = self.db.execute("SELECT id,event_key,payload FROM outbox WHERE dest=? ORDER BY id LIMIT ?", (dest, limit)).fetchall()
        return [(i, k, json.loads(p)) for i, k, p in rows]

    def delete_outbox(self, ids: list[int]) -> None:
        self.db.executemany("DELETE FROM outbox WHERE id=?", [(i,) for i in ids])

    # --- offsets / notifications -----------------------------------------------
    def get_offset(self, path: str) -> tuple[int | None, int, int]:
        r = self.db.execute("SELECT inode,offset,line_no FROM offsets WHERE path=?", (path,)).fetchone()
        return r if r else (None, 0, 0)

    def set_offset(self, path: str, inode: int, offset: int, line_no: int) -> None:
        self.db.execute("INSERT INTO offsets(path,inode,offset,line_no) VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET inode=?,offset=?,line_no=?", (path, inode, offset, line_no, inode, offset, line_no))

    def tracked_paths(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT path FROM offsets")]

    def notify(self, transcript_path: str | None) -> None:
        self.db.execute("INSERT INTO notifications(transcript_path, created_at) VALUES (?,?)", (transcript_path, time.time()))

    def take_notifications(self) -> list[str]:
        rows = self.db.execute("SELECT id, transcript_path FROM notifications ORDER BY id").fetchall()
        if rows:
            self.db.executemany("DELETE FROM notifications WHERE id=?", [(r[0],) for r in rows])
        return sorted({r[1] for r in rows if r[1]})

    def pending_notifications(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    # --- destination state -------------------------------------------------------
    def dest_state(self, dest: str) -> dict:
        r = self.db.execute("SELECT failures,next_try,last_error,batch_size FROM dest_state WHERE dest=?", (dest,)).fetchone()
        return dict(zip(("failures", "next_try", "last_error", "batch_size"), r, strict=True)) if r else {"failures": 0, "next_try": 0.0, "last_error": None, "batch_size": None}

    def set_dest_state(self, dest: str, failures: int, next_try: float, last_error: str | None, batch_size: int | None = None) -> None:
        self.db.execute("INSERT INTO dest_state(dest,failures,next_try,last_error,batch_size) VALUES (?,?,?,?,?) ON CONFLICT(dest) DO UPDATE SET failures=?,next_try=?,last_error=?,batch_size=?", (dest, failures, next_try, last_error, batch_size, failures, next_try, last_error, batch_size))


class _Tx:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, et, ev, tb):
        self.db.execute("ROLLBACK" if et else "COMMIT")
        return False
