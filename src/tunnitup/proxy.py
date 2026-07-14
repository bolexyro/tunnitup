from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web
from multidict import CIMultiDict, CIMultiDictProxy

from tunnitup.observability import ObservationStore, RequestEvent, display_upstream
from tunnitup.routing import RouteTable

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

SESSION_KEY: web.AppKey[ClientSession] = web.AppKey("upstream_session", ClientSession)
ROUTES_KEY: web.AppKey[RouteTable] = web.AppKey("route_table", RouteTable)
SETTINGS_KEY: web.AppKey[ProxySettings] = web.AppKey("proxy_settings")
OBSERVATIONS_KEY: web.AppKey[ObservationStore | None] = web.AppKey("observations")


@dataclass(frozen=True, slots=True)
class ProxySettings:
    connect_timeout: float = 10.0
    response_timeout: float = 60.0
    shutdown_timeout: float = 10.0

    def __post_init__(self) -> None:
        for name, value in (
            ("connect timeout", self.connect_timeout),
            ("response timeout", self.response_timeout),
            ("shutdown timeout", self.shutdown_timeout),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")


def _connection_tokens(headers: Mapping[str, str]) -> set[str]:
    return {
        token.strip().lower() for token in headers.get("Connection", "").split(",") if token.strip()
    }


def _request_headers(request: web.Request) -> CIMultiDict[str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(request.headers) | {"expect", "host"}
    headers = CIMultiDict(
        (name, value) for name, value in request.headers.items() if name.lower() not in blocked
    )

    peername = request.transport.get_extra_info("peername") if request.transport else None
    client_ip = peername[0] if peername else None
    existing = request.headers.get("X-Forwarded-For")
    if client_ip:
        headers["X-Forwarded-For"] = f"{existing}, {client_ip}" if existing else client_ip
    headers["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host", request.host)
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", request.scheme)
    return headers


def _response_headers(headers: CIMultiDictProxy[str]) -> CIMultiDict[str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(headers)
    return CIMultiDict(
        (name, value) for name, value in headers.items() if name.lower() not in blocked
    )


async def _session_context(app: web.Application) -> AsyncIterator[None]:
    settings = app[SETTINGS_KEY]
    timeout = ClientTimeout(
        total=None,
        connect=settings.connect_timeout,
        sock_connect=settings.connect_timeout,
        sock_read=settings.response_timeout,
    )
    app[SESSION_KEY] = ClientSession(
        timeout=timeout,
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    )
    yield
    await app[SESSION_KEY].close()


async def handle_request(request: web.Request) -> web.StreamResponse:
    started = perf_counter()
    observations = request.app[OBSERVATIONS_KEY]
    if observations is not None:
        observations.request_started()
    try:
        return await _handle_request(request, started)
    finally:
        if observations is not None:
            observations.request_finished()


async def _handle_request(request: web.Request, started: float) -> web.StreamResponse:
    route = request.app[ROUTES_KEY].match(request.path)
    if route is None:
        response = web.json_response(
            {"error": "no route configured for this path", "path": request.path},
            status=404,
        )
        _record_request(request, started, status=404)
        return response

    target = route.target_url(request.path, request.query_string)
    try:
        upstream = await request.app[SESSION_KEY].request(
            request.method,
            target,
            headers=_request_headers(request),
            data=request.content.iter_chunked(64 * 1024),
            allow_redirects=False,
        )
    except TimeoutError as exc:
        response = web.json_response(
            {
                "error": "upstream service timed out",
                "route": route.path,
                "upstream": str(route.upstream),
                "detail": str(exc),
            },
            status=504,
        )
        _record_request(request, started, status=504, error="upstream timed out")
        return response
    except aiohttp.ClientError as exc:
        response = web.json_response(
            {
                "error": "upstream service is unavailable",
                "route": route.path,
                "upstream": str(route.upstream),
                "detail": str(exc),
            },
            status=502,
        )
        _record_request(request, started, status=502, error="upstream unavailable")
        return response

    try:
        response = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=_response_headers(upstream.headers),
        )
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        _record_request(request, started, status=upstream.status)
        return response
    finally:
        upstream.release()


def _record_request(
    request: web.Request,
    started: float,
    *,
    status: int,
    error: str | None = None,
) -> None:
    observations = request.app[OBSERVATIONS_KEY]
    if observations is None:
        return
    route = request.app[ROUTES_KEY].match(request.path)
    observations.record(
        RequestEvent(
            timestamp=datetime.now(UTC),
            method=request.method,
            path=request.path,
            route_path=route.path if route else None,
            upstream=display_upstream(route.upstream) if route else None,
            status=status,
            duration_ms=(perf_counter() - started) * 1000,
            error=error,
        )
    )


def create_proxy_app(
    route_table: RouteTable,
    settings: ProxySettings | None = None,
    observations: ObservationStore | None = None,
) -> web.Application:
    app = web.Application()
    app[ROUTES_KEY] = route_table
    app[SETTINGS_KEY] = settings or ProxySettings()
    app[OBSERVATIONS_KEY] = observations
    app.cleanup_ctx.append(_session_context)
    app.router.add_route("*", "/{path_info:.*}", handle_request)
    return app
