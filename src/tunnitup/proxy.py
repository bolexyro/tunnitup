from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web
from multidict import CIMultiDict, CIMultiDictProxy

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


def _connection_tokens(headers: Mapping[str, str]) -> set[str]:
    return {
        token.strip().lower()
        for token in headers.get("Connection", "").split(",")
        if token.strip()
    }


def _request_headers(request: web.Request) -> CIMultiDict[str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(request.headers) | {"host"}
    headers = CIMultiDict(
        (name, value) for name, value in request.headers.items() if name.lower() not in blocked
    )

    peername = request.transport.get_extra_info("peername") if request.transport else None
    client_ip = peername[0] if peername else None
    existing = request.headers.get("X-Forwarded-For")
    if client_ip:
        headers["X-Forwarded-For"] = f"{existing}, {client_ip}" if existing else client_ip
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    return headers


def _response_headers(headers: CIMultiDictProxy[str]) -> CIMultiDict[str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(headers)
    return CIMultiDict(
        (name, value) for name, value in headers.items() if name.lower() not in blocked
    )


async def _session_context(app: web.Application) -> AsyncIterator[None]:
    timeout = ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=None)
    app[SESSION_KEY] = ClientSession(timeout=timeout, auto_decompress=False)
    yield
    await app[SESSION_KEY].close()


async def handle_request(request: web.Request) -> web.StreamResponse:
    route = request.app[ROUTES_KEY].match(request.path)
    if route is None:
        return web.json_response(
            {"error": "no route configured for this path", "path": request.path},
            status=404,
        )

    target = route.target_url(request.path, request.query_string)
    try:
        upstream = await request.app[SESSION_KEY].request(
            request.method,
            target,
            headers=_request_headers(request),
            data=request.content.iter_chunked(64 * 1024),
            allow_redirects=False,
        )
    except (TimeoutError, aiohttp.ClientError) as exc:
        return web.json_response(
            {
                "error": "upstream service is unavailable",
                "route": route.path,
                "upstream": str(route.upstream),
                "detail": str(exc),
            },
            status=502,
        )

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
        return response
    finally:
        upstream.release()


def create_proxy_app(route_table: RouteTable) -> web.Application:
    app = web.Application()
    app[ROUTES_KEY] = route_table
    app.cleanup_ctx.append(_session_context)
    app.router.add_route("*", "/{path_info:.*}", handle_request)
    return app
