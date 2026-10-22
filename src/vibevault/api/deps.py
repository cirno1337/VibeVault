from __future__ import annotations

import hmac
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DbSession


@dataclass(frozen=True)
class Principal:
    name: str
    scope: str  # "admin" | "ingest" | "read"


def get_db(request: Request) -> Iterator[DbSession]:
    with request.app.state.session_factory() as db:
        yield db


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h[:7].lower() == "bearer " and h[7:].strip():
        return h[7:].strip()
    return None


def authenticate(request: Request) -> Principal:
    token = _bearer(request)
    if token is None:
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Bearer token required"})
    s = request.app.state.settings
    found: Principal | None = None
    # Compare against every configured token (no early exit) with constant-time comparison.
    if s.admin_token is not None and hmac.compare_digest(token.encode(), s.admin_token.get_secret_value().encode()):
        found = Principal("admin", "admin")
    for name, tok in s.parsed_ingest_tokens().items():
        if hmac.compare_digest(token.encode(), tok.encode()) and found is None:
            found = Principal(f"ingest:{name}", "ingest")
    for name, tok in s.parsed_read_tokens().items():
        if hmac.compare_digest(token.encode(), tok.encode()) and found is None:
            found = Principal(f"read:{name}", "read")
    if found is None:
        raise HTTPException(401, detail={"code": "invalid_token", "message": "Invalid token"})
    return found


def require_admin(p: Principal = Depends(authenticate)) -> Principal:
    if p.scope != "admin":
        raise HTTPException(403, detail={"code": "forbidden", "message": "Admin token required"})
    return p


def require_ingest(p: Principal = Depends(authenticate)) -> Principal:
    if p.scope == "read":
        raise HTTPException(403, detail={"code": "forbidden", "message": "Read-only token cannot write"})
    return p  # admin or ingest scope


def require_reader(p: Principal = Depends(authenticate)) -> Principal:
    """Admin or read-only token; ingest tokens stay write-only."""
    if p.scope not in ("admin", "read"):
        raise HTTPException(403, detail={"code": "forbidden", "message": "Read or admin token required"})
    return p
