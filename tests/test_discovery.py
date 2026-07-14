import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tunnitup.discovery import (
    PortInputError,
    ServiceProbe,
    assign_suggested_paths,
    parse_ports,
    probe_ports,
)


def test_parse_ports_preserves_order_and_removes_duplicates() -> None:
    assert parse_ports("3000, 8000, 3000, 4000") == (3000, 8000, 4000)


@pytest.mark.parametrize("value", ["", "3000,", "api", "0", "65536"])
def test_parse_ports_rejects_invalid_input(value: str) -> None:
    with pytest.raises(PortInputError):
        parse_ports(value)


def test_path_suggestions_are_deterministic_and_collision_free() -> None:
    probes = (
        ServiceProbe(3000, True, "frontend", "HTML"),
        ServiceProbe(3001, True, "frontend", "HTML"),
        ServiceProbe(8000, True, "api", "OpenAPI"),
        ServiceProbe(8001, True, "api", "JSON"),
        ServiceProbe(4000, True, "service", "HTTP"),
    )

    assigned = assign_suggested_paths(probes)

    assert [probe.suggested_path for probe in assigned] == [
        "/",
        "/app-3001",
        "/api",
        "/api-8001",
        "/service-4000",
    ]


async def test_probe_ports_classifies_only_the_requested_http_service() -> None:
    async def frontend(_: web.Request) -> web.Response:
        return web.Response(text="<html><body>hello</body></html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", frontend)
    async with TestServer(app) as server:
        port = server.make_url("").port
        assert port is not None
        probes = await probe_ports((port,))

    assert len(probes) == 1
    assert probes[0].port == port
    assert probes[0].reachable is True
    assert probes[0].kind == "frontend"
    assert probes[0].detail.startswith("HTML ·")
    assert probes[0].suggested_path == "/"
