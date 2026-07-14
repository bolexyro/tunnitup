import asyncio

import pytest

from tunnitup.orchestration import (
    ProxyStartupError,
    local_tunnel_url,
    run_proxy_with_tunnel,
)
from tunnitup.providers.base import ProviderError, Tunnel
from tunnitup.proxy import ProxySettings
from tunnitup.routing import Route, RouteTable


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(
        self,
        local_url: str,
        *,
        public_url: str | None = None,
        startup_timeout: float = 15.0,
    ) -> Tunnel:
        self.started = True
        return Tunnel("fake", public_url or "https://public.test", local_url)

    async def wait(self) -> None:
        raise ProviderError("provider exited")

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8080"),
        ("0.0.0.0", "http://127.0.0.1:8080"),
        ("::", "http://[::1]:8080"),
    ],
)
def test_local_tunnel_url_uses_a_connectable_loopback(host: str, expected: str) -> None:
    assert local_tunnel_url(host, 8080) == expected


async def test_orchestration_stops_provider_when_it_exits() -> None:
    provider = FakeProvider()
    ready: list[Tunnel] = []
    routes = RouteTable([Route.parse("/=3000")])

    with pytest.raises(ProviderError, match="provider exited"):
        await run_proxy_with_tunnel(
            routes,
            "127.0.0.1",
            0,
            ProxySettings(),
            provider,
            on_ready=ready.append,
        )

    assert provider.started is True
    assert provider.stopped is True
    assert ready[0].public_url == "https://public.test"


async def test_orchestration_surfaces_an_occupied_proxy_port() -> None:
    listener = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
    socket = listener.sockets[0]
    port = socket.getsockname()[1]
    provider = FakeProvider()

    try:
        with pytest.raises(ProxyStartupError, match=f"Port {port} is already in use"):
            await run_proxy_with_tunnel(
                RouteTable([Route.parse("/=3000")]),
                "127.0.0.1",
                port,
                ProxySettings(),
                provider,
            )
    finally:
        listener.close()
        await listener.wait_closed()

    assert provider.started is False
    assert provider.stopped is True
