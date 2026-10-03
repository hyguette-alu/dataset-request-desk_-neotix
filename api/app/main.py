import logging
import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.errors import DomainError
from app.logging_setup import configure_logging, safe_extra

configure_logging(settings.log_level)

from app.routers import analytics, auth, episodes, requests, users  # noqa: E402

logger = logging.getLogger("app")
access_logger = logging.getLogger("app.access")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Dataset Request Desk",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next) -> Response:
        """One structured log line per request.

        `user_id` is read off request.state after the route has run, because
        the auth dependency is what puts it there.
        """
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Log the failed request before the exception propagates, or this
            # request would never appear in the access log at all.
            access_logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "user_id": getattr(request.state, "user_id", None),
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        access_logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "user_id": getattr(request.state, "user_id", None),
            },
        )
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Domain rule violations become 4xx with a message a user can act on.

        Services raise these instead of HTTPException so the rules stay
        testable without going through the API.
        """
        logger.info(
            "domain_error",
            extra=safe_extra(
                {
                    "request_id": getattr(request.state, "request_id", None),
                    "error": type(exc).__name__,
                    "status": exc.status_code,
                    "detail": exc.message,
                    **exc.context,
                }
            ),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "error": type(exc).__name__, **exc.context},
        )

    # Served on both paths: /api/health is what the SPA's nginx proxy reaches,
    # /health is the conventional place a platform health check looks.
    @app.get("/api/health", tags=["ops"])
    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        """Liveness plus a cheap dependency check.

        Returns 200 with database="down" rather than failing outright, so the
        response distinguishes "process is wedged" from "database is
        unreachable" when something is on fire.
        """
        database = "up"
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            logger.exception("health_check_database_unreachable")
            database = "down"

        return {
            "status": "ok" if database == "up" else "degraded",
            "environment": settings.environment,
            "database": database,
        }

    app.include_router(auth.router)
    app.include_router(requests.router)
    app.include_router(episodes.router)
    app.include_router(analytics.router)
    app.include_router(users.router)

    return app


app = create_app()
