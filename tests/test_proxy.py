import asyncio
from collections.abc import AsyncIterator

import pytest
from aiohttp import ClientSession, DummyCookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from tunnitup.observability import ObservationStore
from tunnitup.proxy import ProxySettings, create_proxy_app
from tunnitup.routing import Route, RouteTable

SERVICE_KEY = web.AppKey("service", str)


@pytest.fixture
async def upstream_server() -> AsyncIterator[TestServer]:
    async def echo(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "method": request.method,
                "path": request.path,
                "query": request.query_string,
                "body": (await request.read()).decode(),
                "forwarded_host": request.headers.get("X-Forwarded-Host"),
            },
            headers={"X-Upstream": "echo"},
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", echo)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture
async def proxy_client(upstream_server: TestServer) -> AsyncIterator[TestClient]:
    upstream = str(upstream_server.make_url(""))
    routes = RouteTable(
        [
            Route.parse(f"/={upstream}"),
            Route.parse(f"/api={upstream}", strip_prefix=True),
        ]
    )
    client = TestClient(TestServer(create_proxy_app(routes)))
    await client.start_server()
    yield client
    await client.close()


async def test_forwards_method_path_query_body_and_response_headers(
    proxy_client: TestClient,
) -> None:
    response = await proxy_client.post("/api/users?active=true", data="hello")

    assert response.status == 200
    assert response.headers["X-Upstream"] == "echo"
    payload = await response.json()
    assert payload == {
        "method": "POST",
        "path": "/users",
        "query": "active=true",
        "body": "hello",
        "forwarded_host": response.request_info.url.host + f":{response.request_info.url.port}",
    }


async def test_unmatched_path_returns_a_useful_404() -> None:
    routes = RouteTable([Route.parse("/api=8000")])
    async with TestClient(TestServer(create_proxy_app(routes))) as client:
        response = await client.get("/missing")

        assert response.status == 404
        assert (await response.json())["error"] == "no route configured for this path"


async def test_unavailable_upstream_returns_a_useful_502() -> None:
    routes = RouteTable([Route.parse("/=http://127.0.0.1:1")])
    async with TestClient(TestServer(create_proxy_app(routes))) as client:
        response = await client.get("/")

        assert response.status == 502
        payload = await response.json()
        assert payload["error"] == "upstream service is unavailable"
        assert payload["route"] == "/"


async def test_records_completed_requests_for_observers(upstream_server: TestServer) -> None:
    observations = ObservationStore()
    routes = RouteTable([Route.parse(f"/api={upstream_server.make_url('')}")])

    async with TestClient(
        TestServer(create_proxy_app(routes, observations=observations))
    ) as client:
        response = await client.get("/api/users?token=secret")
        await response.read()

    event = observations.requests[-1]
    assert event.method == "GET"
    assert event.path == "/api/users"
    assert event.route_path == "/api"
    assert event.status == 200
    assert event.duration_ms >= 0
    assert event.error is None
    assert observations.active_requests == 0


async def test_records_proxy_failures_for_observers() -> None:
    observations = ObservationStore()
    routes = RouteTable([Route.parse("/=http://127.0.0.1:1")])

    async with TestClient(
        TestServer(create_proxy_app(routes, observations=observations))
    ) as client:
        response = await client.get("/")
        await response.read()

    event = observations.requests[-1]
    assert event.status == 502
    assert event.error == "upstream unavailable"


async def test_streams_a_response_before_the_upstream_finishes() -> None:
    release_second_chunk = asyncio.Event()

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"first")
        await release_second_chunk.wait()
        await response.write(b"second")
        return response

    upstream_app = web.Application()
    upstream_app.router.add_get("/", stream)
    async with TestServer(upstream_app) as upstream:
        routes = RouteTable([Route.parse(f"/={upstream.make_url('')}")])
        async with TestClient(TestServer(create_proxy_app(routes))) as client:
            response = await client.get("/")
            assert await asyncio.wait_for(response.content.readexactly(5), timeout=1) == b"first"
            release_second_chunk.set()
            assert await response.read() == b"second"


async def test_streams_a_large_request_body_to_the_upstream() -> None:
    async def receive(request: web.Request) -> web.Response:
        total = 0
        chunks = 0
        async for chunk in request.content.iter_chunked(16 * 1024):
            total += len(chunk)
            chunks += 1
        return web.json_response({"bytes": total, "chunks": chunks})

    async def body() -> AsyncIterator[bytes]:
        for _ in range(128):
            yield b"x" * (16 * 1024)
            await asyncio.sleep(0)

    upstream_app = web.Application()
    upstream_app.router.add_post("/upload", receive)
    async with TestServer(upstream_app) as upstream:
        routes = RouteTable([Route.parse(f"/={upstream.make_url('')}")])
        async with TestClient(TestServer(create_proxy_app(routes))) as client:
            response = await client.post("/upload", data=body())
            payload = await response.json()

    assert response.status == 200
    assert payload["bytes"] == 2 * 1024 * 1024
    assert payload["chunks"] > 1


async def test_preserves_forwarded_context_and_removes_connection_headers() -> None:
    async def inspect(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "forwarded_for": request.headers.get("X-Forwarded-For"),
                "forwarded_host": request.headers.get("X-Forwarded-Host"),
                "forwarded_proto": request.headers.get("X-Forwarded-Proto"),
                "removed_request_header": request.headers.get("X-Client-Secret"),
            },
            headers={
                "Connection": "X-Upstream-Secret",
                "X-Upstream-Secret": "remove-me",
            },
        )

    upstream_app = web.Application()
    upstream_app.router.add_get("/", inspect)
    async with TestServer(upstream_app) as upstream:
        routes = RouteTable([Route.parse(f"/={upstream.make_url('')}")])
        async with TestClient(TestServer(create_proxy_app(routes))) as client:
            response = await client.get(
                "/",
                headers={
                    "Connection": "X-Client-Secret",
                    "X-Client-Secret": "remove-me",
                    "X-Forwarded-For": "203.0.113.10",
                    "X-Forwarded-Host": "public.example.test",
                    "X-Forwarded-Proto": "https",
                },
            )
            payload = await response.json()

    assert payload["forwarded_for"].startswith("203.0.113.10, ")
    assert payload["forwarded_host"] == "public.example.test"
    assert payload["forwarded_proto"] == "https"
    assert payload["removed_request_header"] is None
    assert "X-Upstream-Secret" not in response.headers


async def test_upstream_timeout_returns_a_useful_504() -> None:
    async def slow(_request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)
        return web.Response(text="late")

    upstream_app = web.Application()
    upstream_app.router.add_get("/", slow)
    async with TestServer(upstream_app) as upstream:
        routes = RouteTable([Route.parse(f"/={upstream.make_url('')}")])
        settings = ProxySettings(response_timeout=0.05)
        async with TestClient(TestServer(create_proxy_app(routes, settings))) as client:
            response = await client.get("/")
            payload = await response.json()

    assert response.status == 504
    assert payload["error"] == "upstream service timed out"


async def test_proxy_does_not_share_upstream_cookies_between_requests() -> None:
    async def cookies(request: web.Request) -> web.Response:
        if request.path == "/set":
            return web.Response(headers={"Set-Cookie": "session=secret; Path=/"})
        return web.json_response({"cookie": request.headers.get("Cookie")})

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{path:.*}", cookies)
    async with TestServer(upstream_app) as upstream:
        routes = RouteTable([Route.parse(f"/={upstream.make_url('')}")])
        async with TestServer(create_proxy_app(routes)) as proxy:
            async with ClientSession(cookie_jar=DummyCookieJar()) as client:
                await client.get(proxy.make_url("/set"))
                response = await client.get(proxy.make_url("/read"))
                payload = await response.json()

    assert payload["cookie"] is None


async def test_root_relative_request_stays_with_same_origin_referring_route() -> None:
    async def identify(request: web.Request) -> web.Response:
        return web.json_response({"service": request.app[SERVICE_KEY], "path": request.path})

    frontend_app = web.Application()
    frontend_app[SERVICE_KEY] = "frontend"
    frontend_app.router.add_route("*", "/{path:.*}", identify)
    backend_app = web.Application()
    backend_app[SERVICE_KEY] = "backend"
    backend_app.router.add_route("*", "/{path:.*}", identify)

    async with TestServer(frontend_app) as frontend, TestServer(backend_app) as backend:
        routes = RouteTable(
            [
                Route.parse(f"/={frontend.make_url('')}"),
                Route.parse(f"/api={backend.make_url('')}", strip_prefix=True),
            ]
        )
        observations = ObservationStore()
        async with TestClient(
            TestServer(create_proxy_app(routes, observations=observations))
        ) as client:
            response = await client.get(
                "/v3/api-docs/swagger-config",
                headers={
                    "Host": "public.example.test",
                    "X-Forwarded-Host": "public.example.test",
                    "Referer": "https://public.example.test/api/swagger-ui/index.html",
                },
            )
            payload = await response.json()

    assert payload == {"service": "backend", "path": "/v3/api-docs/swagger-config"}
    assert observations.requests[0].route_path == "/api"


async def test_referrer_affinity_ignores_cross_origin_and_specific_direct_routes() -> None:
    async def identify(request: web.Request) -> web.Response:
        return web.Response(text=request.app[SERVICE_KEY])

    frontend_app = web.Application()
    frontend_app[SERVICE_KEY] = "frontend"
    frontend_app.router.add_route("*", "/{path:.*}", identify)
    backend_app = web.Application()
    backend_app[SERVICE_KEY] = "backend"
    backend_app.router.add_route("*", "/{path:.*}", identify)

    async with TestServer(frontend_app) as frontend, TestServer(backend_app) as backend:
        routes = RouteTable(
            [Route.parse(f"/={frontend.make_url('')}"), Route.parse(f"/api={backend.make_url('')}")]
        )
        async with TestClient(TestServer(create_proxy_app(routes))) as client:
            cross_origin = await client.get(
                "/v3/api-docs", headers={"Referer": "https://untrusted.test/api/docs"}
            )
            specific = await client.get(
                "/api/users", headers={"Referer": str(client.make_url("/"))}
            )
            cross_origin_text = await cross_origin.text()
            specific_text = await specific.text()

    assert cross_origin_text == "frontend"
    assert specific_text == "backend"
