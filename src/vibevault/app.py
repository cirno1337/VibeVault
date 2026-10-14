"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from . import db as dbmod
from .api.middleware import RequestContextMiddleware
from .api.routes import router
from .config import Settings, get_settings
from .logging_setup import configure_logging
from .providers import ProviderRegistry
from .redaction import Redactor
from .schemas import sanitize_validation_errors
from .worker import Worker

log = logging.getLogger("vibevault")


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


def create_app(settings: Settings | None = None, registry: ProviderRegistry | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.admin_token is None:
        raise RuntimeError("VIBEVAULT_ADMIN_TOKEN is not set; refusing to start without authentication")

    engine = dbmod.make_engine(settings.database_url)
    session_factory = dbmod.make_session_factory(engine)
    registry = registry or ProviderRegistry(settings)
    worker = Worker(settings, session_factory, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_migrate:
            dbmod.run_migrations(settings.database_url)
        if settings.worker_enabled:
            worker.start()
        log.info("started", extra={"version": __version__, "worker": settings.worker_enabled, "ai_providers": settings.provider_names()})
        yield
        worker.stop()
        engine.dispose()

    app = FastAPI(
        title="VibeVault API", version=__version__, lifespan=lifespan,
        description="Private archive of AI-assisted development. API contract is versioned under /api/v1; event schema_version is '1'.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.registry = registry
    app.state.worker = worker
    app.state.redactor = Redactor.from_file(settings.redaction_rules_file)

    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_body_bytes)
    if settings.cors_origin_list():
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list(), allow_methods=["*"], allow_headers=["authorization", "content-type"])

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return _error(422, "validation_error", "Request validation failed", sanitize_validation_errors(exc.errors()))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        d = exc.detail
        if isinstance(d, dict) and "code" in d:
            resp = _error(exc.status_code, d["code"], d.get("message", ""), d.get("details"))
        else:
            resp = _error(exc.status_code, "http_error", str(d))
        if exc.status_code == 401:
            resp.headers["WWW-Authenticate"] = "Bearer"
        return resp

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.error("unhandled error", extra={"exc_type": type(exc).__name__})  # no message: may echo payload
        return _error(500, "internal_error", "Internal server error")

    @app.get("/healthz", tags=["ops"])
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    def readyz():
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            current, head = dbmod.current_revision(engine), dbmod.head_revision()
        except Exception as e:  # noqa: BLE001
            log.error("readiness failed", extra={"exc_type": type(e).__name__})
            return JSONResponse({"status": "unavailable", "reason": "database"}, status_code=503)
        if current != head:
            return JSONResponse({"status": "unavailable", "reason": "migrations_pending", "current": current, "head": head}, status_code=503)
        return {"status": "ready", "schema_revision": current}

    app.include_router(router)

    static = Path(settings.static_dir) if settings.static_dir else None
    if static and (static / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "docs", "openapi")):
                raise HTTPException(404)
            candidate = (static / path).resolve()
            if path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static / "index.html")

    return app


def create_app_from_env() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    return create_app(s)
