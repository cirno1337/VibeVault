"""HTTP API, version 1 (prefix /api/v1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from .. import __version__, jobs
from .. import models as m
from ..ingest import ingest_batch, normalize_alias
from ..schemas import ACTORS, EVENT_TYPES, BatchIn, BatchResult, sanitize_validation_errors
from ..search import EmptyQuery, search_events
from .deps import Principal, get_db, require_admin, require_ingest
from .serializers import derived_out, event_out, iso, job_out, project_out, session_out

router = APIRouter(prefix="/api/v1")
Db = Annotated[DbSession, Depends(get_db)]
Admin = Annotated[Principal, Depends(require_admin)]
Ingest = Annotated[Principal, Depends(require_ingest)]


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _page(items: list, total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _audit(db: DbSession, p: Principal, action: str, target: str | None = None, **detail) -> None:
    db.add(m.AuditLog(ts=_now(), actor=p.name, action=action, target=target, detail=detail))


def _404(what: str):
    return HTTPException(404, detail={"code": "not_found", "message": f"{what} not found"})


Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


# ------------------------------------------------------------------ identity
@router.get("/me", tags=["auth"])
def me(p: Annotated[Principal, Depends(require_ingest)]):
    return {"name": p.name, "scope": p.scope, "version": __version__}


# ------------------------------------------------------------------ ingestion
@router.post("/events", response_model=BatchResult, tags=["ingest"], summary="Ingest one event (idempotent)")
def post_event(request: Request, db: Db, p: Ingest, event: dict[str, Any] = Body(...)):
    s = request.app.state
    return ingest_batch(db, s.settings, s.redactor, [event], p.name, _now())


@router.post("/events/batch", response_model=BatchResult, tags=["ingest"], summary="Ingest a batch (idempotent, per-item results)")
def post_batch(request: Request, db: Db, p: Ingest, body: dict[str, Any] = Body(...)):
    s = request.app.state
    try:
        batch = BatchIn.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, detail={"code": "validation_error", "message": "Invalid batch envelope", "details": sanitize_validation_errors(e.errors())}) from None
    if len(batch.events) > s.settings.max_batch_events:
        raise HTTPException(413, detail={"code": "batch_too_large", "message": f"At most {s.settings.max_batch_events} events per batch"})
    return ingest_batch(db, s.settings, s.redactor, batch.events, p.name, _now())


# ------------------------------------------------------------------ projects
class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list, max_length=50)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    collection_enabled: bool | None = None
    ai_processing_enabled: bool | None = None


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=1000)


def _project_stats(db: DbSession, ids: list[int]) -> dict[int, dict[str, Any]]:
    out = {i: {"session_count": 0, "event_count": 0, "last_event_at": None} for i in ids}
    for pid, n, ev, last in db.execute(
        select(m.Session.project_id, func.count(), func.coalesce(func.sum(m.Session.event_count), 0), func.max(m.Session.last_event_at))
        .where(m.Session.project_id.in_(ids)).group_by(m.Session.project_id)
    ):
        out[pid] = {"session_count": n, "event_count": int(ev), "last_event_at": iso(last)}
    return out


def _assign_alias(db: DbSession, project: m.Project, alias: str) -> int:
    """Attach alias; retro-assign unresolved sessions/events whose hint matches. Returns sessions updated."""
    norm = normalize_alias(alias)
    existing = db.scalar(select(m.ProjectAlias).where(m.ProjectAlias.alias == norm))
    if existing and existing.project_id != project.id:
        raise HTTPException(409, detail={"code": "alias_in_use", "message": "Alias belongs to another project"})
    if not existing:
        project.aliases.append(m.ProjectAlias(alias=norm))
        db.flush()
    n = 0
    for sess in db.scalars(select(m.Session).where(m.Session.project_id.is_(None), m.Session.project_hint.is_not(None))):
        if normalize_alias(sess.project_hint or "") == norm:
            sess.project_id = project.id
            db.execute(update(m.Event).where(m.Event.session_id == sess.id).values(project_id=project.id))
            n += 1
    for ev_id in db.scalars(select(m.Event.id).where(m.Event.project_id.is_(None), m.Event.session_id.is_(None), m.Event.project_hint.is_not(None))):
        e = db.get(m.Event, ev_id)
        if normalize_alias(e.project_hint or "") == norm:
            e.project_id = project.id
    return n


@router.get("/projects", tags=["projects"])
def list_projects(db: Db, _: Admin, limit: Limit = 50, offset: Offset = 0):
    total = db.scalar(select(func.count()).select_from(m.Project)) or 0
    rows = db.scalars(select(m.Project).options(joinedload(m.Project.aliases)).order_by(m.Project.name).limit(limit).offset(offset)).unique().all()
    stats = _project_stats(db, [r.id for r in rows])
    unresolved = db.scalar(select(func.count()).select_from(m.Session).where(m.Session.project_id.is_(None))) or 0
    page = _page([project_out(r, **stats[r.id]) for r in rows], total, limit, offset)
    page["unresolved_sessions"] = unresolved
    return page


@router.post("/projects", status_code=201, tags=["projects"])
def create_project(db: Db, p: Admin, body: ProjectCreate):
    from ..ingest import _slugify

    slug, n = _slugify(body.name), 1
    base = slug
    while db.scalar(select(m.Project.id).where(m.Project.slug == slug)) is not None:
        n += 1
        slug = f"{base}-{n}"
    proj = m.Project(slug=slug, name=body.name, description=body.description)
    db.add(proj)
    db.flush()
    for a in body.aliases:
        _assign_alias(db, proj, a)
    _audit(db, p, "project_create", str(proj.id))
    db.commit()
    return project_out(proj, **_project_stats(db, [proj.id])[proj.id])


@router.get("/projects/{project_id}", tags=["projects"])
def get_project(project_id: int, db: Db, _: Admin):
    proj = db.get(m.Project, project_id)
    if proj is None:
        raise _404("Project")
    derived = db.scalars(
        select(m.DerivedItem).where(m.DerivedItem.project_id == project_id, m.DerivedItem.state == "active", m.DerivedItem.kind != "digest")
        .order_by(m.DerivedItem.created_at.desc()).limit(50)
    ).all()
    return {**project_out(proj, **_project_stats(db, [project_id])[project_id]), "derived": [derived_out(d) for d in derived]}


@router.patch("/projects/{project_id}", tags=["projects"])
def patch_project(project_id: int, body: ProjectPatch, db: Db, p: Admin):
    proj = db.get(m.Project, project_id)
    if proj is None:
        raise _404("Project")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(proj, k, v)
    _audit(db, p, "project_update", str(project_id), fields=sorted(changes))
    db.commit()
    return project_out(proj)


@router.post("/projects/{project_id}/aliases", tags=["projects"])
def add_alias(project_id: int, body: AliasIn, db: Db, p: Admin):
    proj = db.get(m.Project, project_id)
    if proj is None:
        raise _404("Project")
    n = _assign_alias(db, proj, body.alias)
    _audit(db, p, "project_alias_add", str(project_id), sessions_assigned=n)
    db.commit()
    return {"project": project_out(proj), "sessions_assigned": n}


class ProjectMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into: int


def _project_refs() -> list[Any]:
    """Every column that points at projects.id: aliases, sessions, events, derived items, ..."""
    return [
        c for t in m.Base.metadata.sorted_tables for c in t.columns
        if any(fk.column.table.name == m.Project.__tablename__ for fk in c.foreign_keys)
    ]


@router.post("/projects/{project_id}/merge", tags=["projects"], summary="Move everything of a project (aliases included) into another, then delete it")
def merge_project(project_id: int, body: ProjectMerge, db: Db, p: Admin):
    src, dst = db.get(m.Project, project_id), db.get(m.Project, body.into)
    if src is None or dst is None:
        raise _404("Project")
    if src.id == dst.id:
        raise HTTPException(422, detail={"code": "same_project", "message": "Cannot merge a project into itself"})
    moved: dict[str, int] = {}
    for col in _project_refs():
        n = db.execute(update(col.table).where(col == src.id).values({col.name: dst.id})).rowcount
        if n:
            moved[col.table.name] = n
    # Core statements only: the ORM must not cascade-delete the aliases it still thinks belong to src.
    db.expunge(src)
    db.execute(delete(m.Project).where(m.Project.id == project_id))
    _audit(db, p, "project_merge", str(project_id), into=dst.id, moved=moved)
    db.commit()
    db.refresh(dst)
    return {"project": project_out(dst, **_project_stats(db, [dst.id])[dst.id]), "moved": moved}


@router.delete("/projects/{project_id}", tags=["projects"], summary="Delete a project that nothing refers to (merge it otherwise)")
def delete_project(project_id: int, db: Db, p: Admin):
    proj = db.get(m.Project, project_id)
    if proj is None:
        raise _404("Project")
    used = {
        col.table.name: n for col in _project_refs() if col.table.name != m.ProjectAlias.__tablename__
        if (n := db.scalar(select(func.count()).select_from(col.table).where(col == project_id)) or 0)
    }
    if used:
        raise HTTPException(409, detail={"code": "project_not_empty", "message": "Project still has data; merge it into another project instead", "details": used})
    aliases = [a.alias for a in proj.aliases]
    db.delete(proj)
    _audit(db, p, "project_delete", str(project_id), aliases=aliases)
    db.commit()
    return {"deleted": project_id, "aliases_removed": aliases}


# ------------------------------------------------------------------ devices / sources
class DevicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_enabled: bool | None = None
    label: str | None = Field(default=None, max_length=200)


@router.get("/devices", tags=["sources"])
def list_devices(db: Db, _: Admin):
    rows = db.execute(
        select(m.SourceDevice, m.Source.kind, func.count(m.Event.seq))
        .join(m.Source, m.Source.id == m.SourceDevice.source_id)
        .outerjoin(m.Event, m.Event.device_id == m.SourceDevice.id)
        .group_by(m.SourceDevice.id).order_by(m.Source.kind, m.SourceDevice.device_key)
    ).all()
    return {"items": [
        {"id": d.id, "source": kind, "device_key": d.device_key, "label": d.label,
         "collection_enabled": d.collection_enabled, "first_seen_at": iso(d.first_seen_at),
         "last_seen_at": iso(d.last_seen_at), "event_count": n}
        for d, kind, n in rows
    ]}


@router.patch("/devices/{device_id}", tags=["sources"])
def patch_device(device_id: int, body: DevicePatch, db: Db, p: Admin):
    d = db.get(m.SourceDevice, device_id)
    if d is None:
        raise _404("Device")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(d, k, v)
    _audit(db, p, "device_update", str(device_id), **body.model_dump(exclude_unset=True))
    db.commit()
    return {"id": d.id, "collection_enabled": d.collection_enabled, "label": d.label}


# ------------------------------------------------------------------ sessions
def _session_query(project_id, source, device, since, until, unresolved, q_title):
    stmt = select(m.Session).join(m.Source, m.Source.id == m.Session.source_id).join(m.SourceDevice, m.SourceDevice.id == m.Session.device_id)
    conds = [m.Session.excluded.is_(False)]
    if project_id is not None:
        conds.append(m.Session.project_id == project_id)
    if source:
        conds.append(m.Source.kind == source)
    if device:
        conds.append(m.SourceDevice.device_key == device)
    if since:
        conds.append(m.Session.last_event_at >= since)
    if until:
        conds.append(m.Session.started_at <= until)
    if unresolved:
        conds.append(m.Session.project_id.is_(None))
    if q_title:
        conds.append(m.Session.title.ilike(f"%{q_title}%"))
    return stmt.where(and_(*conds))


@router.get("/sessions", tags=["sessions"])
def list_sessions(
    db: Db, _: Admin, project_id: int | None = None, source: str | None = None, device: str | None = None,
    since: AwareDatetime | None = None, until: AwareDatetime | None = None, unresolved: bool = False,
    title: str | None = None, limit: Limit = 50, offset: Offset = 0,
):
    stmt = _session_query(project_id, source, device, since, until, unresolved, title)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.options(joinedload(m.Session.source), joinedload(m.Session.device), joinedload(m.Session.project))
        .order_by(m.Session.last_event_at.desc().nulls_last(), m.Session.id.desc()).limit(limit).offset(offset)
    ).unique().all()
    previews = {}
    for s in rows:
        first = db.scalar(
            select(m.Event.content).where(m.Event.session_id == s.id, m.Event.is_current, m.Event.actor == "user", m.Event.event_type == "message")
            .order_by(m.Event.occurred_at, m.Event.seq).limit(1)
        )
        previews[s.id] = (first or "")[:200]
    return _page([session_out(s, preview=previews[s.id]) for s in rows], total, limit, offset)


@router.get("/sessions/{session_id}", tags=["sessions"])
def get_session(session_id: int, db: Db, _: Admin):
    s = db.scalar(select(m.Session).options(joinedload(m.Session.source), joinedload(m.Session.device), joinedload(m.Session.project)).where(m.Session.id == session_id))
    if s is None:
        raise _404("Session")
    derived = db.scalars(select(m.DerivedItem).where(m.DerivedItem.session_id == session_id, m.DerivedItem.state.in_(("active", "draft"))).order_by(m.DerivedItem.created_at)).all()
    return {**session_out(s), "derived": [derived_out(d) for d in derived]}


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    excluded: bool | None = None
    project_id: int | None = None


@router.patch("/sessions/{session_id}", tags=["sessions"])
def patch_session(session_id: int, body: SessionPatch, db: Db, p: Admin):
    s = db.get(m.Session, session_id)
    if s is None:
        raise _404("Session")
    data = body.model_dump(exclude_unset=True)
    if "project_id" in data:
        if data["project_id"] is not None and db.get(m.Project, data["project_id"]) is None:
            raise _404("Project")
        s.project_id = data["project_id"]
        db.execute(update(m.Event).where(m.Event.session_id == s.id).values(project_id=data["project_id"]))
    if "excluded" in data:
        s.excluded = bool(data["excluded"])
    _audit(db, p, "session_update", str(session_id), fields=sorted(data))
    db.commit()
    return {"id": s.id, "excluded": s.excluded, "project_id": s.project_id}


@router.delete("/sessions/{session_id}", tags=["sessions"], summary="Delete a session's evidence; keeps an exclusion tombstone")
def delete_session(session_id: int, db: Db, p: Admin):
    s = db.get(m.Session, session_id)
    if s is None:
        raise _404("Session")
    n = db.execute(delete(m.Event).where(m.Event.session_id == session_id)).rowcount
    db.execute(delete(m.DerivedItem).where(m.DerivedItem.session_id == session_id))
    db.execute(delete(m.ToolExecution).where(m.ToolExecution.session_id == session_id))
    s.excluded, s.event_count, s.title = True, 0, None
    _audit(db, p, "session_delete", str(session_id), events_deleted=n)
    db.commit()
    return {"deleted_events": n, "tombstone": True}


@router.get("/sessions/{session_id}/events", tags=["sessions"])
def session_events(
    session_id: int, db: Db, _: Admin, event_type: str | None = None, include_superseded: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200, offset: Offset = 0,
):
    if db.get(m.Session, session_id) is None:
        raise _404("Session")
    conds = [m.Event.session_id == session_id]
    if not include_superseded:
        conds.append(m.Event.is_current)
    if event_type:
        conds.append(m.Event.event_type == event_type)
    total = db.scalar(select(func.count()).select_from(m.Event).where(*conds)) or 0
    rows = db.scalars(
        select(m.Event).options(joinedload(m.Event.source), joinedload(m.Event.device)).where(*conds)
        .order_by(m.Event.occurred_at, func.coalesce(m.Event.sequence, 0), m.Event.seq).limit(limit).offset(offset)
    ).unique().all()
    return _page(_events_with_related(db, rows), total, limit, offset)


def _events_with_related(db: DbSession, rows: list[m.Event]) -> list[dict[str, Any]]:
    call_ids = {e.tool_call_id for e in rows if e.tool_call_id}
    tools: dict[tuple[int, int, str], m.ToolExecution] = {}
    if call_ids:
        for t in db.scalars(select(m.ToolExecution).where(m.ToolExecution.tool_call_id.in_(call_ids))):
            tools[(t.source_id, t.device_id, t.tool_call_id)] = t
    fcs: dict[str, list[m.FileChange]] = {}
    fe = [e.id for e in rows if e.event_type == "file_change"]
    if fe:
        for f in db.scalars(select(m.FileChange).where(m.FileChange.event_id.in_(fe))):
            fcs.setdefault(f.event_id, []).append(f)
    return [
        event_out(e, tool=tools.get((e.source_id, e.device_id, e.tool_call_id or "")), file_changes=fcs.get(e.id))
        for e in rows
    ]


# ------------------------------------------------------------------ events
@router.get("/events", tags=["events"])
def list_events(
    db: Db, _: Admin, project_id: int | None = None, source: str | None = None,
    event_type: Annotated[str | None, Query(pattern=f"^({'|'.join(EVENT_TYPES)})$")] = None,
    actor: Annotated[str | None, Query(pattern=f"^({'|'.join(ACTORS)})$")] = None,
    since: AwareDatetime | None = None, until: AwareDatetime | None = None,
    unsessioned: bool = False, limit: Limit = 50, offset: Offset = 0,
):
    stmt = select(m.Event).join(m.Source, m.Source.id == m.Event.source_id).where(m.Event.is_current)
    if project_id is not None:
        stmt = stmt.where(m.Event.project_id == project_id)
    if source:
        stmt = stmt.where(m.Source.kind == source)
    if event_type:
        stmt = stmt.where(m.Event.event_type == event_type)
    if actor:
        stmt = stmt.where(m.Event.actor == actor)
    if since:
        stmt = stmt.where(m.Event.occurred_at >= since)
    if until:
        stmt = stmt.where(m.Event.occurred_at <= until)
    if unsessioned:
        stmt = stmt.where(m.Event.session_id.is_(None))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.options(joinedload(m.Event.source), joinedload(m.Event.device)).order_by(m.Event.occurred_at.desc(), m.Event.seq.desc()).limit(limit).offset(offset)).unique().all()
    return _page(_events_with_related(db, rows), total, limit, offset)


@router.get("/events/{event_id}", tags=["events"])
def get_event(event_id: str, db: Db, _: Admin):
    e = db.scalar(select(m.Event).options(joinedload(m.Event.source), joinedload(m.Event.device)).where(m.Event.id == event_id))
    if e is None:
        raise _404("Event")
    tool = db.scalar(select(m.ToolExecution).where(m.ToolExecution.source_id == e.source_id, m.ToolExecution.device_id == e.device_id, m.ToolExecution.tool_call_id == e.tool_call_id)) if e.tool_call_id else None
    fcs = db.scalars(select(m.FileChange).where(m.FileChange.event_id == e.id)).all()
    same = and_(m.Event.source_id == e.source_id, m.Event.device_id == e.device_id)
    parent = db.scalar(select(m.Event).options(joinedload(m.Event.source), joinedload(m.Event.device)).where(same, m.Event.external_event_id == e.parent_external_event_id, m.Event.is_current)) if e.parent_external_event_id else None
    children = db.scalars(select(m.Event).options(joinedload(m.Event.source), joinedload(m.Event.device)).where(same, m.Event.parent_external_event_id == e.external_event_id, m.Event.is_current).order_by(m.Event.occurred_at).limit(50)).unique().all()
    revisions = db.execute(select(m.Event.id, m.Event.revision, m.Event.is_current, m.Event.ingested_at).where(same, m.Event.external_event_id == e.external_event_id).order_by(m.Event.revision)).all()
    derived = db.scalars(
        select(m.DerivedItem).join(m.DerivedEvidence, m.DerivedEvidence.derived_id == m.DerivedItem.id)
        .where(m.DerivedEvidence.event_id == e.id, m.DerivedItem.state == "active")
    ).unique().all()
    return {
        "event": event_out(e, raw=True, tool=tool, file_changes=fcs),
        "parent": event_out(parent) if parent else None,
        "children": [event_out(c) for c in children],
        "revisions": [{"id": i, "revision": r, "is_current": c, "ingested_at": iso(t)} for i, r, c, t in revisions],
        "derived": [derived_out(d) for d in derived],
    }


# ------------------------------------------------------------------ search
@router.get("/search", tags=["search"])
def search(
    db: Db, _: Admin, q: Annotated[str, Query(min_length=1, max_length=500)], project_id: int | None = None,
    source: str | None = None, event_type: str | None = None, actor: str | None = None,
    since: AwareDatetime | None = None, until: AwareDatetime | None = None,
    include_superseded: bool = False, limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Offset = 0,
):
    try:
        res = search_events(db, q, project_id=project_id, source=source, event_type=event_type, actor=actor,
                            since=_aware(since), until=_aware(until), include_superseded=include_superseded,
                            limit=limit, offset=offset)
    except EmptyQuery as e:
        raise HTTPException(400, detail={"code": "empty_query", "message": str(e)}) from None
    ids = [r["id"] for r in res["rows"]]
    sess = {}
    if ids:
        for sid, title, pname in db.execute(
            select(m.Session.id, m.Session.title, m.Project.name).outerjoin(m.Project, m.Project.id == m.Session.project_id)
            .where(m.Session.id.in_({r["session_id"] for r in res["rows"] if r["session_id"]}))
        ):
            sess[sid] = {"title": title, "project": pname}
    items = []
    for r in res["rows"]:
        occurred = r["occurred_at"]
        if isinstance(occurred, str):
            occurred = datetime.fromisoformat(occurred).replace(tzinfo=UTC)
        items.append({
            "event_id": r["id"], "session_id": r["session_id"], "project_id": r["project_id"], "source": r["source"],
            "event_type": r["event_type"], "actor": r["actor"], "occurred_at": iso(occurred), "rank": r["rank"],
            "snippet": r["snippet"], "session_title": sess.get(r["session_id"], {}).get("title"),
            "project_name": sess.get(r["session_id"], {}).get("project"),
        })
    return {**_page(items, res["total"], limit, offset), "query": q, "fts_match": res["match"]}


# ------------------------------------------------------------------ jobs
class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(digest_session|summarize_session|classify_sessions)$")
    session_id: int | None = None  # required except for classify_sessions (runs over all unassigned sessions)


@router.get("/jobs", tags=["jobs"])
def list_jobs(db: Db, _: Admin, status: Annotated[str | None, Query(pattern="^(pending|running|completed|failed)$")] = None,
              kind: str | None = None, limit: Limit = 50, offset: Offset = 0):
    stmt = select(m.Job)
    if status:
        stmt = stmt.where(m.Job.status == status)
    if kind:
        stmt = stmt.where(m.Job.kind == kind)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(m.Job.id.desc()).limit(limit).offset(offset)).all()
    return {**_page([job_out(j) for j in rows], total, limit, offset), "counts": jobs.counts_by_status(db)}


@router.post("/jobs", status_code=201, tags=["jobs"])
def create_job(request: Request, body: JobCreate, db: Db, p: Admin):
    s = request.app.state
    if body.kind in ("summarize_session", "classify_sessions") and not s.registry.any_configured():
        raise HTTPException(409, detail={"code": "ai_not_configured", "message": "No AI provider is configured (VIBEVAULT_PROVIDERS is empty)"})
    if body.kind == "classify_sessions":
        job, created = jobs.enqueue(db, s.settings, body.kind, {"manual": True}, "classify_sessions")
    else:
        if body.session_id is None or db.get(m.Session, body.session_id) is None:
            raise _404("Session")
        job, created = jobs.enqueue(db, s.settings, body.kind, {"session_id": body.session_id}, f"{body.kind}:{body.session_id}")
    _audit(db, p, "job_enqueue", str(job.id), kind=body.kind, created=created)
    db.commit()
    return {**job_out(job), "created": created}


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: int, db: Db, _: Admin):
    j = db.get(m.Job, job_id)
    if j is None:
        raise _404("Job")
    return job_out(j)


@router.post("/jobs/{job_id}/retry", tags=["jobs"])
def retry_job(job_id: int, db: Db, p: Admin):
    if db.get(m.Job, job_id) is None:
        raise _404("Job")
    job = jobs.retry_failed(db, job_id)
    if job is None:
        raise HTTPException(409, detail={"code": "not_retryable", "message": "Only failed jobs without an active duplicate can be retried"})
    _audit(db, p, "job_retry", str(job_id))
    db.commit()
    return job_out(job)


# ------------------------------------------------------------------ derived knowledge
@router.get("/derived", tags=["derived"])
def list_derived(db: Db, _: Admin, session_id: int | None = None, project_id: int | None = None,
                 kind: str | None = None, epistemic_status: str | None = None,
                 state: Annotated[str, Query(pattern="^(active|draft|superseded)$")] = "active",
                 limit: Limit = 50, offset: Offset = 0):
    stmt = select(m.DerivedItem).where(m.DerivedItem.state == state)
    for col, val in ((m.DerivedItem.session_id, session_id), (m.DerivedItem.project_id, project_id), (m.DerivedItem.kind, kind), (m.DerivedItem.epistemic_status, epistemic_status)):
        if val is not None:
            stmt = stmt.where(col == val)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(m.DerivedItem.created_at.desc()).limit(limit).offset(offset)).all()
    sids = {d.session_id for d in rows if d.session_id}
    titles = dict(db.execute(select(m.Session.id, m.Session.title).where(m.Session.id.in_(sids))).all()) if sids else {}
    return _page([{**derived_out(d), "session_title": titles.get(d.session_id)} for d in rows], total, limit, offset)


@router.post("/derived/{item_id}/confirm", tags=["derived"], summary="Human confirmation of a model-derived item")
def confirm_derived(item_id: int, db: Db, p: Admin):
    d = db.get(m.DerivedItem, item_id)
    if d is None:
        raise _404("Derived item")
    if not d.evidence:
        raise HTTPException(409, detail={"code": "no_evidence", "message": "Items without evidence cannot be confirmed"})
    d.epistemic_status, d.confirmed_by, d.confirmed_at = "confirmed", p.name, _now()
    _audit(db, p, "derived_confirm", str(item_id))
    db.commit()
    return derived_out(d)


def _project_suggestion(db: DbSession, item_id: int) -> m.DerivedItem:
    from ..classify import KIND

    d = db.get(m.DerivedItem, item_id)
    if d is None or d.kind != KIND:
        raise _404("Project suggestion")
    return d


@router.post("/derived/{item_id}/apply", tags=["derived"], summary="Accept a project suggestion: assign the session (creating the suggested project)")
def apply_project_suggestion(item_id: int, db: Db, p: Admin):
    from ..classify import apply_suggestion

    d = _project_suggestion(db, item_id)
    if (d.data or {}).get("applied"):
        raise HTTPException(409, detail={"code": "already_applied", "message": "This suggestion was already applied"})
    try:
        proj = apply_suggestion(db, d, p.name, _now())
    except LookupError as e:
        raise _404(str(e).capitalize()) from None
    except ValueError:
        raise HTTPException(409, detail={"code": "nothing_to_apply", "message": "The model found no project for this session"}) from None
    _audit(db, p, "project_suggestion_apply", str(item_id), project_id=proj.id, session_id=d.session_id)
    db.commit()
    return {"item": derived_out(d), "project": project_out(proj)}


@router.post("/derived/{item_id}/dismiss", tags=["derived"], summary="Reject a project suggestion (the session is not asked about again until it grows)")
def dismiss_project_suggestion(item_id: int, db: Db, p: Admin):
    d = _project_suggestion(db, item_id)
    d.state, d.superseded_at = "superseded", _now()
    d.data = {**(d.data or {}), "dismissed": True}
    _audit(db, p, "project_suggestion_dismiss", str(item_id), session_id=d.session_id)
    db.commit()
    return derived_out(d)


# ------------------------------------------------------------------ providers / stats / audit
@router.get("/providers", tags=["ai"])
def providers(request: Request, db: Db, _: Admin):
    s = request.app.state
    return {
        "ai_enabled": s.registry.any_configured(),
        "allow_external_ai": s.settings.allow_external_ai,
        "providers": [st.as_dict() for st in s.registry.all_status(db, _now())],
    }


@router.get("/stats", tags=["stats"])
def stats(request: Request, db: Db, _: Admin):
    from datetime import timedelta

    now = _now()
    c = lambda stmt: db.scalar(stmt) or 0  # noqa: E731
    ev = m.Event
    by_type = dict(db.execute(select(ev.event_type, func.count()).where(ev.is_current).group_by(ev.event_type)).all())
    by_source = dict(db.execute(select(m.Source.kind, func.count()).join(ev, ev.source_id == m.Source.id).where(ev.is_current).group_by(m.Source.kind)).all())
    s = request.app.state
    return {
        "totals": {
            "events": c(select(func.count()).select_from(ev).where(ev.is_current)),
            "sessions": c(select(func.count()).select_from(m.Session).where(m.Session.excluded.is_(False))),
            "projects": c(select(func.count()).select_from(m.Project)),
            "devices": c(select(func.count()).select_from(m.SourceDevice)),
            "unresolved_sessions": c(select(func.count()).select_from(m.Session).where(m.Session.project_id.is_(None), m.Session.excluded.is_(False))),
            "derived_items": c(select(func.count()).select_from(m.DerivedItem).where(m.DerivedItem.state == "active")),
        },
        "events_by_type": by_type,
        "events_by_source": by_source,
        "events_last_24h": c(select(func.count()).select_from(ev).where(ev.ingested_at >= now - timedelta(hours=24))),
        "events_last_7d": c(select(func.count()).select_from(ev).where(ev.ingested_at >= now - timedelta(days=7))),
        "last_ingested_at": iso(db.scalar(select(func.max(ev.ingested_at)))),
        "processing": {
            "events_pending": c(select(func.count()).select_from(ev).where(ev.processing_status == "pending")),
            "events_processed": c(select(func.count()).select_from(ev).where(ev.processing_status == "processed")),
            "jobs": jobs.counts_by_status(db),
        },
        "redaction": {
            "events_redacted": c(select(func.count()).select_from(ev).where(ev.redaction_status == "redacted")),
            "events_unscanned": c(select(func.count()).select_from(ev).where(ev.redaction_status == "unscanned")),
        },
        "ai": {"enabled": s.registry.any_configured()},
        "version": __version__,
    }


@router.get("/audit", tags=["stats"])
def audit(db: Db, _: Admin, limit: Limit = 50, offset: Offset = 0):
    total = db.scalar(select(func.count()).select_from(m.AuditLog)) or 0
    rows = db.scalars(select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(limit).offset(offset)).all()
    return _page([{"id": r.id, "ts": iso(r.ts), "actor": r.actor, "action": r.action, "target": r.target, "detail": r.detail} for r in rows], total, limit, offset)
