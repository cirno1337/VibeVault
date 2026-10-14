"""Versioned, source-independent event contract (schema_version "1").

Collectors normalise whatever they capture into `EventIn`. Nothing here knows about the DOM of
chatgpt.com / claude.ai or the layout of any particular transcript file; such details live in
collectors and, if worth keeping, in `raw_payload` / `metadata`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

EventType = Literal[
    "session_start",
    "session_end",
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "file_change",
    "error",
    "system",
    "other",
]
Actor = Literal["user", "assistant", "tool", "system"]
EVENT_TYPES: tuple[str, ...] = EventType.__args__  # type: ignore[attr-defined]
ACTORS: tuple[str, ...] = Actor.__args__  # type: ignore[attr-defined]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class ProjectRef(Strict):
    key: str = Field(min_length=1, max_length=1000, description="Source-side identity, e.g. cwd or git remote")
    name: str | None = Field(default=None, max_length=300)


class ToolBlock(Strict):
    tool_call_id: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=200)
    command: str | None = None
    input: dict[str, Any] | None = None
    output: str | None = None
    exit_code: int | None = None
    is_error: bool | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class FileChangeIn(Strict):
    path: str = Field(min_length=1, max_length=2000)
    operation: Literal["create", "edit", "delete", "read", "rename"]
    old_path: str | None = Field(default=None, max_length=2000)
    diff: str | None = None
    before_hash: str | None = Field(default=None, max_length=128)
    after_hash: str | None = Field(default=None, max_length=128)


class RedactionInfo(Strict):
    """What the collector did before sending. Rule names + counts only, never matched text."""

    status: Literal["none", "redacted", "unscanned"] = "unscanned"
    count: int = Field(default=0, ge=0)
    rules: dict[str, int] = Field(default_factory=dict)


_ID = r"^[A-Za-z0-9_.\-]+$"


class EventIn(Strict):
    schema_version: Literal["1"] = "1"
    source: str = Field(pattern=r"^[a-z0-9_.\-]{1,64}$", description="e.g. claude_code, chatgpt_web")
    device_id: str = Field(min_length=1, max_length=128, pattern=_ID)
    device_label: str | None = Field(default=None, max_length=200)
    external_event_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(default=1, ge=1, le=1_000_000)
    external_session_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_title: str | None = Field(default=None, max_length=500)
    project: ProjectRef | None = None
    event_type: EventType
    actor: Actor
    occurred_at: AwareDatetime
    sequence: int | None = Field(default=None, ge=0)
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_external_event_id: str | None = Field(default=None, max_length=256)
    tool: ToolBlock | None = None
    file_changes: list[FileChangeIn] = Field(default_factory=list, max_length=200)
    redaction: RedactionInfo = Field(default_factory=RedactionInfo)
    raw_payload: Any | None = None
    raw_payload_ref: str | None = Field(default=None, max_length=1000)

    @field_validator("content")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        return v.replace("\x00", "")


class BatchIn(Strict):
    events: list[dict[str, Any]]


class ItemResult(BaseModel):
    index: int
    external_event_id: str | None = None
    status: Literal["created", "duplicate", "conflict", "rejected", "skipped"]
    event_id: str | None = None
    reason: str | None = None
    errors: list[dict[str, str]] | None = None


class BatchResult(BaseModel):
    schema_version: str = "1"
    received: int
    created: int
    duplicate: int
    conflict: int
    rejected: int
    skipped: int
    results: list[ItemResult]


def sanitize_validation_errors(errors: list[Any]) -> list[dict[str, str]]:
    """Pydantic errors minus `input`/`ctx` (which echo the offending payload, possibly a secret)."""
    return [
        {"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
        for e in errors
    ]
