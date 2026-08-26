"""App assembly & startup.

Startup sequence (lifespan), in order:
  1. settings           — config.get_settings() + runtime validation
  2. providers          — resolve models, set Settings.llm/embed_model explicitly
  3. stores             — load + reconcile (corruption => CRITICAL log + exit 1)
  4. reranker           — first-run download here, never mid-query
  5. embedding backfill — gemini mode only (§3.5)

Every non-2xx response is the §1.1 error envelope; messages never contain
stack traces, filesystem paths, or key material.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api, config, ingest, providers, rerank, stores

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("alpha.main")

CORS_ALLOWED_ORIGINS = ["http://localhost:3000"]

# 405 maps to "not_found" — CONTRACTS.md §1.1 defines exactly six codes and
# method_not_allowed is not one of them; an undefined method on a route is
# "no such endpoint" from the client's point of view.
_STATUS_TO_CODE = {
    400: "bad_request",
    404: "not_found",
    405: "not_found",
    429: "rate_limited",
    502: "provider_error",
}


def _envelope(status: int, code: str, message: str, retry_after_s=None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if retry_after_s is not None:
        body["error"]["retry_after_s"] = int(retry_after_s)
    return JSONResponse(status_code=status, content=body)


class BodySizeLimitMiddleware:
    """Pure-ASGI Content-Length precheck for POST /api/documents.

    Runs BEFORE FastAPI's multipart parsing (which spools the whole body), so
    an oversized request is refused with the §1.1 `bad_file` envelope without
    buffering a byte. Chunked requests without Content-Length fall through to
    the capped per-file reads in the upload handler.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") == "POST" and scope.get("path") == "/api/documents":
            for key, value in scope.get("headers") or []:
                if key == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                    response = _envelope(
                        400,
                        "bad_file",
                        f"request body too large (max {self.max_bytes // (1024 * 1024)} MB per request)",
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = config.get_settings()
    try:
        settings.validate_runtime()
    except ValueError as exc:
        logger.critical("configuration error: %s", exc)
        raise SystemExit(1) from exc

    try:
        providers.init_providers(settings)
    except (providers.ProviderError, providers.RateLimitedError) as exc:
        logger.critical("provider startup failed: %s", exc)
        raise SystemExit(1) from exc

    try:
        stores.init_store()  # load + reconcile
    except stores.StoreCorruptionError as exc:
        logger.critical("STORE CORRUPTION: %s", exc)
        raise SystemExit(1) from exc

    rerank.init_reranker()

    if settings.effective_provider == "gemini":
        ingest.backfill_missing_embeddings()

    logger.info(
        "startup complete: provider=%s rerank=%s docs=%d",
        settings.effective_provider,
        rerank.effective_rerank(),
        stores.get_store().counts()[0],
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Alpha Detective API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # lock the surface: no /openapi.json schema dump
    )

    # Added before CORSMiddleware so CORS wraps it (last added = outermost):
    # even a size-refused response carries CORS headers for the allowed origin.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=ingest.MAX_REQUEST_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(api.router)

    @app.exception_handler(api.ApiError)
    async def api_error_handler(request: Request, exc: api.ApiError):
        return _envelope(exc.status, exc.code, exc.message)

    @app.exception_handler(providers.RateLimitedError)
    async def rate_limited_handler(request: Request, exc: providers.RateLimitedError):
        return _envelope(429, "rate_limited", "Free-tier rate limit hit", exc.retry_after_s)

    @app.exception_handler(providers.ProviderError)
    async def provider_error_handler(request: Request, exc: providers.ProviderError):
        return _envelope(502, "provider_error", str(exc) or "provider request failed")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 is remapped to the bad_request envelope.
        message = "invalid request payload"
        errors = exc.errors()
        if errors:
            loc = ".".join(str(p) for p in errors[0].get("loc", []) if p != "body")
            msg = errors[0].get("msg", "")
            if loc or msg:
                message = f"invalid request payload: {loc + ': ' if loc else ''}{msg}"
        return _envelope(400, "bad_request", message)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = _STATUS_TO_CODE.get(exc.status_code, "internal" if exc.status_code >= 500 else "bad_request")
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return _envelope(exc.status_code, code, detail)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=True)
        return _envelope(500, "internal", "internal error")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
    except SystemExit as exc:  # normalize any startup failure to exit code 1
        sys.exit(1 if exc.code else 0)
    except BaseException:  # noqa: BLE001 — fail loud, never a traceback to users
        logger.critical("server failed to start")
        sys.exit(1)
