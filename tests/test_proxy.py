from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tunnitup.proxy import create_proxy_app
from tunnitup.routing import Route, RouteTable


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
