from __future__ import annotations

from collections.abc import Callable

from aiohttp import web

from tunnitup.providers import ProviderError, Tunnel, TunnelProvider
from tunnitup.proxy import ProxySettings, create_proxy_app
from tunnitup.routing import RouteTable


def local_tunnel_url(host: str, port: int) -> str:
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "[::1]"
    return f"http://{host}:{port}"


async def run_proxy_with_tunnel(
    routes: RouteTable,
    host: str,
    port: int,
    settings: ProxySettings,
    provider: TunnelProvider,
    *,
    public_url: str | None = None,
    startup_timeout: float = 15.0,
    on_ready: Callable[[Tunnel], None] | None = None,
) -> None:
    runner = web.AppRunner(
        create_proxy_app(routes, settings),
        shutdown_timeout=settings.shutdown_timeout,
        handler_cancellation=True,
    )
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        tunnel = await provider.start(
            local_tunnel_url(host, port),
            public_url=public_url,
            startup_timeout=startup_timeout,
        )
        if on_ready is not None:
            on_ready(tunnel)
        await provider.wait()
        raise ProviderError(f"{provider.name} stopped unexpectedly")
    finally:
        await provider.stop()
        await runner.cleanup()
