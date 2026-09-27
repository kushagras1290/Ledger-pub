import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ledger import __version__
from ledger.api import accounts, system, transactions
from ledger.api.deps import require_api_key
from ledger.config import Settings
from ledger.db import build_engine, build_session_factory
from ledger.errors import LedgerError
from ledger.logging_setup import configure_logging, request_id_var

logger = logging.getLogger("ledger")

PROBLEM_JSON = "application/problem+json"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _problem(status: int, code: str, title: str, detail: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON,
        content={
            "type": f"https://errors.ledger.dev/{code}",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            **extra,
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        logger.info("ledger started", extra={"version": __version__})
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Ledger",
        version=__version__,
        summary="Double-entry ledger with idempotent, concurrency-safe writes.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(_: Request, exc: LedgerError) -> JSONResponse:
        return _problem(exc.status_code, exc.code, exc.title, exc.detail, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return _problem(
            422, "validation_error", "Request validation failed", "see 'errors'", errors=issues
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return _problem(500, "internal_error", "Internal server error", "unexpected failure")

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
    v1.include_router(accounts.router)
    v1.include_router(transactions.router)
    app.include_router(v1)
    app.include_router(system.router)
    return app
