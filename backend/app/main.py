"""App assembly & startup.

Startup sequence (lifespan), in order:
  1. settings           — config.get_settings() + runtime validation
  2. providers          — resolve models, set Settings.llm/embed_model explicitly
  3. stores             — load + reconcile (corruption => CRITICAL log + exit 1)
  4. reranker           — first-run download here, never mid-query
  5. embedding backfill — gemini mode only (§3.5)
  6. auto-seed          — AUTO_SEED=on and an empty manifest only (v1.2)

Request order through the ASGI stack (§1.10): CORS (outermost) -> body-size
precheck -> access-code gate -> per-IP throttle -> routing. The access gate sits
BEFORE the throttle so a 401 never consumes a throttle slot, and every refusal
still carries CORS headers because CORS wraps them all.

Every non-2xx response is the §1.1 error envelope; messages never contain
stack traces, filesystem paths, or key material. `ACCESS_CODE` and
`GOOGLE_API_KEY` are never logged (§5.2).
"""

from __future__ import annotations

import hmac
import logging
import math
import sys
import time
from collections import OrderedDict
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

# 405 maps to "not_found" — CONTRACTS.md §1.1 defines exactly six codes and
# method_not_allowed is not one of them; an undefined method on a route is
# "no such endpoint" from the client's point of view.
_STATUS_TO_CODE = {
    400: "bad_request",
    401: "unauthorized",
    404: "not_found",
    405: "not_found",
    429: "rate_limited",
    502: "provider_error",
}

# Routes the per-IP throttle applies to (§1.10). GET routes are exempt ON
# PURPOSE: useHealth polls 6x/min and every page reloads the document list
# after a mutation — throttling reads would lock an honest user out of their
# own UI (resolution 13).
THROTTLED_ROUTES = (
    ("POST", "/api/query"),
    ("POST", "/api/documents"),
    ("DELETE", "/api/documents/"),
)

HEALTH_PATH = "/api/health"


def _envelope(status: int, code: str, message: str, retry_after_s=None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if retry_after_s is not None:
        body["error"]["retry_after_s"] = int(retry_after_s)
    return JSONResponse(status_code=status, content=body)


class BodySizeLimitMiddleware:
    """Pure-ASGI request-body ceiling for EVERY `/api` route.

    Two layers, because either alone is bypassable:

    1. A `Content-Length` precheck — refuses before a byte is buffered.
    2. A streaming byte counter over `receive` — `Transfer-Encoding: chunked`
       carries no Content-Length, and a Content-Length can lie, so the header
       check alone lets an attacker stream unbounded bytes into Starlette's
       spool (RAM for JSON, ephemeral disk for multipart).

    Uploads get `MAX_REQUEST_BYTES` (§1.3) and answer `bad_file`; every other
    `/api` route gets the small `MAX_JSON_BODY_BYTES` and answers `bad_request`
    — a 2000-character question (§1.6) needs nothing more. Both run BEFORE
    FastAPI parses anything.
    """

    def __init__(self, app, max_bytes: int, json_max_bytes: int = 0):
        self.app = app
        self.max_bytes = max_bytes
        self.json_max_bytes = json_max_bytes or config.MAX_JSON_BODY_BYTES

    def _limit_for(self, scope) -> tuple[int, str, str]:
        """(limit, error code, message) for this request."""
        if scope.get("method") == "POST" and scope.get("path") == "/api/documents":
            return (
                self.max_bytes,
                "bad_file",
                f"request body too large (max {self.max_bytes // (1024 * 1024)} MB per request)",
            )
        return (
            self.json_max_bytes,
            "bad_request",
            f"request body too large (max {self.json_max_bytes // 1024} KB)",
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not (scope.get("path") or "").startswith("/api"):
            await self.app(scope, receive, send)
            return

        limit, code, message = self._limit_for(scope)

        for key, value in scope.get("headers") or []:
            if key == b"content-length" and value.isdigit() and int(value) > limit:
                await _envelope(400, code, message)(scope, receive, send)
                return

        state = {"seen": 0, "over": False, "sent": False}

        async def guarded_receive():
            event = await receive()
            if event.get("type") == "http.request":
                state["seen"] += len(event.get("body") or b"")
                if state["seen"] > limit:
                    # Stop feeding the app and let it unwind; the response is
                    # replaced below so the client still gets the envelope.
                    state["over"] = True
                    return {"type": "http.disconnect"}
            return event

        async def guarded_send(event):
            if state["over"]:
                # The body blew the cap: discard whatever the app produced and
                # answer with the §1.1 envelope exactly once.
                if event.get("type") == "http.response.start" and not state["sent"]:
                    state["sent"] = True
                    await _envelope(400, code, message)(scope, receive, send)
                return
            await send(event)

        await self.app(scope, guarded_receive, guarded_send)
        if state["over"] and not state["sent"]:
            state["sent"] = True
            await _envelope(400, code, message)(scope, receive, send)


class AccessCodeMiddleware:
    """The `ACCESS_CODE` quota gate (§1.10) — pure ASGI, body never touched.

    A quota gate, NOT a security boundary: the browser has to hold the code to
    use the app, so it is not secret from a determined visitor. It exists to
    stop a public demo URL from burning the free Gemini quota. It is unrelated
    to GOOGLE_API_KEY, which never leaves the server.

    `GET /api/health` is the only exempt route (the frontend poll and the
    container probe must work before a code is entered), OPTIONS preflight is
    exempt (CORS must complete before the browser sends custom headers), and
    the header value is NEVER logged — not at INFO, not at DEBUG, not in an
    error message, not in an exception.
    """

    def __init__(self, app, code: str = ""):
        self.app = app
        self.code = (code or "").strip()
        self._expected = self.code.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if not self.code or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method == "OPTIONS" or not path.startswith("/api"):
            await self.app(scope, receive, send)
            return
        if method == "GET" and path == HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        provided = None
        for key, value in scope.get("headers") or []:
            if key == b"x-access-code":
                provided = value
                break
        if provided is None:  # the header was not sent at all
            await _envelope(401, "unauthorized", "Access code required")(scope, receive, send)
            return
        # EVERY non-None value — the empty string included — reaches
        # compare_digest. An empty header is PRESENT and wrong, not absent, so
        # it is "Invalid access code". No truthiness test, no length check, no
        # early return, no logging of `provided` may sit in front of this line:
        # the two messages may reveal only whether the header was SENT, never
        # whether a code is configured, its length, or how close a guess was —
        # and any pre-compare branch on the value is what invites a timing
        # signal to be added next to it later (§1.10, ratified r3).
        if not hmac.compare_digest(provided, self._expected):
            await _envelope(401, "unauthorized", "Invalid access code")(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """Fixed-window per-IP throttle for the three mutating routes (§1.10).

    In-process memory only: not persisted, reset on restart, not shared across
    instances — the free tier runs one instance and that is accepted, not an
    oversight. The tracked-IP table is bounded so a spoofed X-Forwarded-For
    flood cannot exhaust memory.
    """

    def __init__(self, app, per_min: int = 10, window_s: int = 60, max_tracked: int = 4096,
                 trusted_proxy_hops: int = 0):
        self.app = app
        self.trusted_proxy_hops = max(0, int(trusted_proxy_hops))
        self.per_min = int(per_min)
        self.window_s = int(window_s) or 60
        self.max_tracked = int(max_tracked) or 1
        self._buckets: "OrderedDict[str, tuple[float, int]]" = OrderedDict()

    def _throttled(self, scope) -> bool:
        method, path = scope.get("method", ""), scope.get("path", "")
        for route_method, route_path in THROTTLED_ROUTES:
            if method != route_method:
                continue
            if route_path.endswith("/"):
                if path.startswith(route_path) and len(path) > len(route_path):
                    return True
            elif path == route_path:
                return True
        return False

    def _peer(self, scope) -> str:
        client = scope.get("client") or ()
        return (client[0] if client else None) or "unknown"

    def _client_key(self, scope) -> str:
        """Identity = socket peer, unless a trusted proxy count says otherwise.

        `X-Forwarded-For` is attacker-controlled end to end: a client can send
        any value, and a proxy that APPENDS leaves the client's own claim at
        the LEFT. Trusting the left hop was a total bypass (send any value, the
        throttle never applies) AND a targeted attack (`<victim>, <me>` burns
        the victim's bucket and locks them out). So:

          hops == 0  -> the header is ignored entirely (default, and correct
                        for local dev or any port exposed without a proxy);
          hops == N  -> take xff[-N], the Nth value from the RIGHT — only a
                        proxy we control can write those.

        Absent, malformed, or shorter-than-N XFF falls back to the socket
        peer, NEVER to a client-supplied value. That fallback is the fix: any
        path back to attacker-chosen text re-opens the bypass.

        (law — scope) This is a quota rail, not an authentication boundary. It
        carries no user identity, does not survive NAT or a proxy pool, and is
        not evidence of anything.
        """
        hops = self.trusted_proxy_hops
        if hops <= 0:
            return self._peer(scope)
        for key, value in scope.get("headers") or []:
            if key == b"x-forwarded-for":
                parts = [p.strip() for p in value.decode("latin-1").split(",")]
                parts = [p for p in parts if p]
                if len(parts) >= hops:
                    return parts[-hops]
                break  # too short to contain our proxies' hops: distrust it
        return self._peer(scope)

    def _check(self, key: str) -> int:
        """0 = allowed; otherwise whole seconds until the window frees a slot."""
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= self.window_s:
            start, count = now, 0
        if count >= self.per_min:
            # No move_to_end: ordering tracks WINDOW START, not last touch, so
            # a blocked key cannot keep itself alive (nor evict fresher ones).
            self._buckets[key] = (start, count)
            return max(1, int(math.ceil(start + self.window_s - now)))
        was_new_window = key not in self._buckets or self._buckets[key][0] != start
        self._buckets[key] = (start, count + 1)
        if was_new_window:
            self._buckets.move_to_end(key)  # newest window goes to the back
        while len(self._buckets) > self.max_tracked:
            oldest_key, (oldest_start, _) = next(iter(self._buckets.items()))
            if now - oldest_start < self.window_s and len(self._buckets) <= self.max_tracked:
                break
            self._buckets.pop(oldest_key)  # oldest window (expired first) evicted
        return 0

    async def __call__(self, scope, receive, send):
        if (
            self.per_min <= 0
            or scope.get("type") != "http"
            or scope.get("method") == "OPTIONS"
            or not self._throttled(scope)
        ):
            await self.app(scope, receive, send)
            return
        retry_after_s = self._check(self._client_key(scope))
        if retry_after_s:
            response = _envelope(
                429, "rate_limited", "Too many requests — slow down", retry_after_s
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

    # `auto` is best-effort: any provider failure degrades to retrieval-only
    # rather than killing the process. An explicit PROVIDER=gemini is a promise
    # the operator made, so it still fails loudly. (Ratified r3.)
    try:
        bundle = providers.init_providers(settings)
    except Exception as exc:  # noqa: BLE001 - auto must survive *any* failure
        if settings.provider != "auto":
            logger.critical("provider startup failed: %s", exc)
            raise SystemExit(1) from exc
        logger.warning(
            "provider auto-detection failed (%s); falling back to retrieval-only "
            "mode. Search works; generated answers are disabled until a valid "
            "GOOGLE_API_KEY is present in backend/.env.",
            exc,
        )
        bundle = providers.init_none_mode()

    try:
        stores.init_store()  # load + reconcile
    except stores.StoreCorruptionError as exc:
        logger.critical("STORE CORRUPTION: %s", exc)
        raise SystemExit(1) from exc

    rerank.init_reranker()

    if bundle.provider == "gemini":
        ingest.backfill_missing_embeddings()

    # Auto-seed LAST, so it runs against a fully reconciled store and can never
    # block boot: seed_sample_data swallows its own failures by contract.
    if settings.auto_seed == "on" and not stores.get_store().get_manifest():
        await ingest.seed_sample_data()

    logger.info(
        "startup complete: provider=%s rerank=%s docs=%d daily_llm_budget=%d "
        "access_code=%s",
        bundle.provider,
        rerank.effective_rerank(),
        stores.get_store().counts()[0],
        settings.daily_llm_budget,
        "on" if settings.access_code.strip() else "off",  # never the value
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

    settings = config.get_settings()
    origins = config.parse_cors_origins(settings.cors_origins)
    if "*" in origins:
        logger.warning(
            "CORS_ORIGINS is `*` — every origin may call this API. No cookies "
            "are ever sent, but prefer the exact frontend origin in production."
        )

    # Added innermost-first; the last one added is outermost. Request order is
    # therefore: CORS -> body-size -> access code -> throttle -> routing, so a
    # 401 never consumes a throttle slot and every refusal — including the two
    # new gates — still carries CORS headers for the allowed origin.
    app.add_middleware(
        RateLimitMiddleware,
        per_min=settings.rate_limit_per_min,
        window_s=config.RATE_LIMIT_WINDOW_S,
        max_tracked=config.RATE_LIMIT_MAX_TRACKED_IPS,
        trusted_proxy_hops=settings.trusted_proxy_hops,
    )
    app.add_middleware(AccessCodeMiddleware, code=settings.access_code)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=ingest.MAX_REQUEST_BYTES,
        json_max_bytes=config.MAX_JSON_BODY_BYTES,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],  # includes X-Access-Code; credentials stay off
    )

    app.include_router(api.router)

    # A chunks URL whose id contains path separators is normalized by the
    # client/proxy before it ever reaches routing (`/api/documents/../..
    # /etc/passwd/chunks` arrives as `/etc/passwd/chunks`). From the caller's
    # point of view that is still "the chunks endpoint with a malformed id",
    # so it gets the frozen §1.8 answer instead of a bare router miss. Real
    # chunk requests match the router route above first.
    @app.get("/{prefix:path}/chunks", include_in_schema=False)
    async def chunks_malformed_id(prefix: str) -> dict:
        raise api.NotFoundError("unknown document id")

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

    settings = config.get_settings()
    try:
        # 0.0.0.0 iff PORT is present in the environment (a PaaS is dictating
        # it); local dev with no PORT keeps binding 127.0.0.1 and never
        # exposes the LAN (resolution 15).
        uvicorn.run("app.main:app", host=config.bind_host(), port=settings.port)
    except SystemExit as exc:  # normalize any startup failure to exit code 1
        sys.exit(1 if exc.code else 0)
    except BaseException:  # noqa: BLE001 — fail loud, never a traceback to users
        logger.critical("server failed to start")
        sys.exit(1)
