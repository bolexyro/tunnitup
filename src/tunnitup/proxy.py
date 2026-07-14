from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from time import perf_counter

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tunnitup.observability import ObservationStore, RequestEvent, display_upstream
from tunnitup.routing import Route, RouteTable

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
WEBSOCKET_HANDSHAKE_HEADERS = frozenset(
    {
        "sec-websocket-accept",
        "sec-websocket-extensions",
        "sec-websocket-key",
        "sec-websocket-protocol",
        "sec-websocket-version",
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


def _request_headers(request: web.Request, route: Route) -> CIMultiDict[str]:
    blocked = (
        HOP_BY_HOP_HEADERS
        | WEBSOCKET_HANDSHAKE_HEADERS
        | _connection_tokens(request.headers)
        | {"expect", "host"}
    )
    headers = CIMultiDict(
        (name, value) for name, value in request.headers.items() if name.lower() not in blocked
    )

    peername = request.transport.get_extra_info("peername") if request.transport else None
    client_ip = peername[0] if peername else None
    existing = request.headers.get("X-Forwarded-For")
    if client_ip:
        headers["X-Forwarded-For"] = f"{existing}, {client_ip}" if existing else client_ip
    headers["X-Forwarded-Host"] = _forwarded_host(request)
    headers["X-Forwarded-Proto"] = _forwarded_proto(request)
    if route.path != "/":
        headers["X-Forwarded-Prefix"] = route.path
    else:
        headers.popall("X-Forwarded-Prefix", None)
    return headers


def _forwarded_host(request: web.Request) -> str:
    candidate = request.headers.get("X-Forwarded-Host", request.host).split(",", 1)[0].strip()
    try:
        parsed = URL(f"//{candidate}")
    except ValueError:
        return request.host
    valid = parsed.host and parsed.user is None and parsed.password is None
    return candidate if valid else request.host


def _forwarded_proto(request: web.Request) -> str:
    candidate = request.headers.get("X-Forwarded-Proto", request.scheme).split(",", 1)[0]
    normalized = candidate.strip().casefold()
    return normalized if normalized in {"http", "https"} else request.scheme


def _response_headers(
    headers: CIMultiDictProxy[str], request: web.Request, route: Route
) -> CIMultiDict[str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(headers)
    rewritten = CIMultiDict[str]()
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in blocked:
            continue
        if lowered == "location":
            value = _rewrite_location(value, request, route)
        elif lowered == "set-cookie":
            for cookie in _rewrite_set_cookie(value, route):
                rewritten.add(name, cookie)
            continue
        rewritten.add(name, value)
    return rewritten


def _prefixed_path(path: str, route: Route) -> str:
    if route.path == "/" or route.matches(path):
        return path
    return f"{route.path}{path if path.startswith('/') else f'/{path}'}"


def _rewrite_location(location: str, request: web.Request, route: Route) -> str:
    if route.path == "/":
        return location
    try:
        target = URL(location, encoded=True)
    except ValueError:
        return location

    if not target.is_absolute():
        if not location.startswith("/"):
            return location
        return str(
            target.with_path(
                _prefixed_path(target.raw_path, route),
                encoded=True,
                keep_query=True,
                keep_fragment=True,
            )
        )

    if target.host != route.upstream.host or target.port != route.upstream.port:
        return location

    public_scheme = _forwarded_proto(request)
    public_host = _forwarded_host(request)
    public = URL.build(scheme=public_scheme, authority=public_host)
    rewritten = str(
        public.with_path(_prefixed_path(target.raw_path, route), encoded=True)
    )
    if target.raw_query_string:
        rewritten = f"{rewritten}?{target.raw_query_string}"
    if target.raw_fragment:
        rewritten = f"{rewritten}#{target.raw_fragment}"
    return rewritten


def _rewrite_set_cookie(header: str, route: Route) -> tuple[str, ...]:
    if route.path == "/":
        return (header,)
    cookies: SimpleCookie[str] = SimpleCookie()
    try:
        cookies.load(header)
    except ValueError:
        return (header,)
    if not cookies:
        return (header,)

    rewritten: list[str] = []
    for morsel in cookies.values():
        cookie_path = morsel["path"]
        if not cookie_path:
            morsel["path"] = route.path
        elif cookie_path == "/":
            morsel["path"] = route.path
        elif cookie_path.startswith("/"):
            morsel["path"] = _prefixed_path(cookie_path, route)
        if morsel["domain"] and morsel["domain"].lstrip(".").casefold() == (
            route.upstream.host or ""
        ).casefold():
            morsel["domain"] = ""
        rewritten.append(morsel.OutputString())
    return tuple(rewritten)


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
    try:
        yield
    finally:
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
    route = _select_route(request)
    if route is None:
        response = web.json_response(
            {"error": "no route configured for this path", "path": request.path},
            status=404,
        )
        _record_request(request, started, route=route, status=404)
        return response

    target = route.target_url(request.path, request.query_string)
    if request.headers.get("Upgrade", "").casefold() == "websocket":
        return await _proxy_websocket(request, route, target, started)
    try:
        upstream = await request.app[SESSION_KEY].request(
            request.method,
            target,
            headers=_request_headers(request, route),
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
        _record_request(
            request, started, route=route, status=504, error="upstream timed out"
        )
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
        _record_request(
            request, started, route=route, status=502, error="upstream unavailable"
        )
        return response

    try:
        response = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=_response_headers(upstream.headers, request, route),
        )
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        _record_request(request, started, route=route, status=upstream.status)
        return response
    finally:
        upstream.release()


async def _proxy_websocket(
    request: web.Request, route: Route, target: URL, started: float
) -> web.StreamResponse:
    protocols = tuple(
        protocol.strip()
        for protocol in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if protocol.strip()
    )
    try:
        upstream = await request.app[SESSION_KEY].ws_connect(
            target,
            headers=_request_headers(request, route),
            protocols=protocols,
            autoping=False,
            autoclose=False,
            compress=0,
            max_msg_size=0,
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        _record_request(
            request, started, route=route, status=502, error="websocket unavailable"
        )
        return web.json_response(
            {
                "error": "upstream websocket is unavailable",
                "route": route.path,
                "upstream": str(route.upstream),
                "detail": str(exc),
            },
            status=502,
        )

    selected_protocols = (upstream.protocol,) if upstream.protocol else ()
    downstream = web.WebSocketResponse(
        protocols=selected_protocols,
        autoping=False,
        autoclose=False,
        compress=False,
        max_msg_size=0,
    )
    await downstream.prepare(request)

    downstream_to_upstream = asyncio.create_task(
        _relay_websocket(downstream, upstream)
    )
    upstream_to_downstream = asyncio.create_task(
        _relay_websocket(upstream, downstream)
    )
    tasks = {downstream_to_upstream, upstream_to_downstream}
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await upstream.close()
        await downstream.close()

    _record_request(request, started, route=route, status=101)
    return downstream


async def _relay_websocket(
    source: aiohttp.ClientWebSocketResponse | web.WebSocketResponse,
    destination: aiohttp.ClientWebSocketResponse | web.WebSocketResponse,
) -> None:
    async for message in source:
        if message.type == aiohttp.WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type == aiohttp.WSMsgType.PING:
            await destination.ping(message.data)
        elif message.type == aiohttp.WSMsgType.PONG:
            await destination.pong(message.data)
        elif message.type == aiohttp.WSMsgType.CLOSE:
            await destination.close(code=message.data, message=message.extra.encode())
            break
        elif message.type in {
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.ERROR,
        }:
            break


def _record_request(
    request: web.Request,
    started: float,
    *,
    route: Route | None,
    status: int,
    error: str | None = None,
) -> None:
    observations = request.app[OBSERVATIONS_KEY]
    if observations is None:
        return
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


def _select_route(request: web.Request) -> Route | None:
    """Prefer a more specific route from a same-origin referring page.

    Some applications mounted below a prefix emit root-relative requests. A Swagger UI
    served from /api, for example, may fetch /v3/api-docs. Direct path matching would
    send that request to the catch-all / route, so retain affinity with /api when the
    browser tells us that is where the request originated.
    """
    routes = request.app[ROUTES_KEY]
    direct = routes.match(request.path)
    if direct is None or direct.path != "/":
        return direct

    referer = request.headers.get("Referer")
    if not referer:
        return direct
    try:
        referring_url = URL(referer)
    except ValueError:
        return direct
    if not _matches_request_authority(referring_url, request):
        return direct

    referred = routes.match(referring_url.path)
    return referred if referred is not None and referred.path != "/" else direct


def _matches_request_authority(referring_url: URL, request: web.Request) -> bool:
    authorities = [request.host]
    if forwarded_host := request.headers.get("X-Forwarded-Host"):
        authorities.append(forwarded_host.split(",", 1)[0].strip())

    for authority in authorities:
        try:
            request_url = URL(f"//{authority}")
        except ValueError:
            continue
        if (
            referring_url.host is not None
            and request_url.host is not None
            and referring_url.host.casefold() == request_url.host.casefold()
            and referring_url.explicit_port == request_url.explicit_port
        ):
            return True
    return False


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
