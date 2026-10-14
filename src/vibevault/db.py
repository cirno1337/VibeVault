"""Engine/session setup and migration helpers. SQLite-first; keep SQL portable where cheap."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite and url.database and url.database != ":memory:":
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}} if is_sqlite else {}
    engine = create_engine(database_url, future=True, **kwargs)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
            # Take over transaction control from pysqlite (SQLAlchemy's documented recipe).
            dbapi_conn.isolation_level = None

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):  # pragma: no cover - trivial
            # A deferred transaction that reads, then writes after another connection committed,
            # fails at once with "database is locked" (busy_timeout does not apply to that lock
            # upgrade in WAL mode). Taking the write lock up front makes writers queue instead.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def _alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def run_migrations(database_url: str, revision: str = "head") -> None:
    command.upgrade(_alembic_config(database_url), revision)


def head_revision() -> str:
    return ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head() or ""


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
