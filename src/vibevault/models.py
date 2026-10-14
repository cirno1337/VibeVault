"""SQLAlchemy models.

Layers (see docs/architecture.md):
  RAW EVIDENCE      sources, source_devices, projects, sessions, events, tool_executions, file_changes
  OPERATIONS        jobs, audit_log, provider_usage
  DERIVED KNOWLEDGE derived_items, derived_evidence (always reference raw events; never edit them)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo natively)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: UTCDateTime}


# ---------------------------------------------------------------- raw evidence


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    collection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_processing_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    aliases: Mapped[list[ProjectAlias]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectAlias(Base):
    """Maps a source-provided project identity (cwd, git remote, URL) to a project."""

    __tablename__ = "project_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(1000), unique=True)

    project: Mapped[Project] = relationship(back_populates="aliases")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), unique=True)  # claude_code, chatgpt_web, ...
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SourceDevice(Base):
    __tablename__ = "source_devices"
    __table_args__ = (UniqueConstraint("source_id", "device_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    device_key: Mapped[str] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(200))
    collection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)

    source: Mapped[Source] = relationship()


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("source_id", "device_id", "external_session_id"),
        Index("ix_sessions_last_event", "last_event_at"),
        Index("ix_sessions_project", "project_id", "last_event_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    device_id: Mapped[int] = mapped_column(ForeignKey("source_devices.id"))
    external_session_id: Mapped[str] = mapped_column(String(256))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    project_hint: Mapped[str | None] = mapped_column(String(1000))  # unresolved identity
    title: Mapped[str | None] = mapped_column(String(500))  # source-provided only
    started_at: Mapped[datetime | None] = mapped_column()
    last_event_at: Mapped[datetime | None] = mapped_column()
    ended_at: Mapped[datetime | None] = mapped_column()
    # open: no end seen; ended: session_end seen. "interrupted" is a UI judgement, not stored.
    status: Mapped[str] = mapped_column(String(16), default="open")
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)  # tombstone / do-not-collect
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    source: Mapped[Source] = relationship()
    device: Mapped[SourceDevice] = relationship()
    project: Mapped[Project | None] = relationship()


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source_id", "device_id", "external_event_id", "revision", name="uq_event_identity"),
        Index("ix_events_session_order", "session_id", "occurred_at", "sequence"),
        Index("ix_events_project_time", "project_id", "occurred_at"),
        Index("ix_events_time", "occurred_at"),
        Index("ix_events_type", "event_type"),
        Index("ix_events_processing", "processing_status"),
        Index("ix_events_tool_call", "source_id", "device_id", "tool_call_id"),
        Index("ix_events_parent", "source_id", "device_id", "parent_external_event_id"),
        {"sqlite_autoincrement": True},
    )
    # `seq` is the rowid alias used by FTS5 (stable across VACUUM) and records ingest order.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True)  # deterministic UUIDv5 of identity
    schema_version: Mapped[str] = mapped_column(String(8))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    device_id: Mapped[int] = mapped_column(ForeignKey("source_devices.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    external_session_id: Mapped[str | None] = mapped_column(String(256))
    external_event_id: Mapped[str] = mapped_column(String(256))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    project_hint: Mapped[str | None] = mapped_column(String(1000))
    event_type: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column()
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)
    ingested_by: Mapped[str | None] = mapped_column(String(100))
    sequence: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    parent_external_event_id: Mapped[str | None] = mapped_column(String(256))
    tool_call_id: Mapped[str | None] = mapped_column(String(256))
    redaction_status: Mapped[str] = mapped_column(String(16), default="unscanned")
    redaction_count: Mapped[int] = mapped_column(Integer, default=0)
    redaction_rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {rule: count}
    processing_status: Mapped[str] = mapped_column(String(16), default="pending")
    raw_payload: Mapped[Any | None] = mapped_column(JSON)
    raw_payload_ref: Mapped[str | None] = mapped_column(String(1000))

    source: Mapped[Source] = relationship()
    device: Mapped[SourceDevice] = relationship()


class ToolExecution(Base):
    """One tool call and (when it arrives) its result. Rows are upserted by tool_call_id."""

    __tablename__ = "tool_executions"
    __table_args__ = (UniqueConstraint("source_id", "device_id", "tool_call_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    device_id: Mapped[int] = mapped_column(ForeignKey("source_devices.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    tool_call_id: Mapped[str] = mapped_column(String(256))
    tool_name: Mapped[str | None] = mapped_column(String(200))
    command: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    is_error: Mapped[bool | None] = mapped_column(Boolean)
    call_event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    result_event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()


class FileChange(Base):
    __tablename__ = "file_changes"
    __table_args__ = (Index("ix_file_changes_path", "path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(2000))
    operation: Mapped[str] = mapped_column(String(16))
    old_path: Mapped[str | None] = mapped_column(String(2000))
    diff: Mapped[str | None] = mapped_column(Text)
    before_hash: Mapped[str | None] = mapped_column(String(128))
    after_hash: Mapped[str | None] = mapped_column(String(128))


# ----------------------------------------------------------------- operations


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after", "priority"),
        Index(
            "uq_jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('pending','running') AND dedupe_key IS NOT NULL"),
            postgresql_where=text("status IN ('pending','running') AND dedupe_key IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|completed|failed
    priority: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column()
    status_note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()


class AuditLog(Base):
    """Operational audit trail. `detail` holds counts and identifiers, never payload content."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(300))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    __table_args__ = (UniqueConstraint("provider", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD, UTC
    requests: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------- derived knowledge


class DerivedItem(Base):
    """Interpretation layer. Never replaces raw events; always carries evidence + epistemic status.

    origin:  deterministic | model | human
    epistemic_status: confirmed | inferred | hypothesis | unresolved
      - Model output can only be inferred/hypothesis/unresolved. "confirmed" requires a human
        (POST /derived/{id}/confirm) or deterministic computation from raw events.
    state: draft (generation still running) | active | superseded
    """

    __tablename__ = "derived_items"
    __table_args__ = (
        Index("ix_derived_session", "session_id", "state"),
        Index("ix_derived_project", "project_id", "kind", "state"),
        Index("ix_derived_generation", "generation_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(16))
    epistemic_status: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="active")
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    generation_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column()
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column()

    evidence: Mapped[list[DerivedEvidence]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class DerivedEvidence(Base):
    __tablename__ = "derived_evidence"
    __table_args__ = (Index("ix_derived_evidence_event", "event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    derived_id: Mapped[int] = mapped_column(ForeignKey("derived_items.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    note: Mapped[str | None] = mapped_column(String(500))

    item: Mapped[DerivedItem] = relationship(back_populates="evidence")
