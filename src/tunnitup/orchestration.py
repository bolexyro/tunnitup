from __future__ import annotations

from collections.abc import Callable

from aiohttp import web

from tunnitup.observability import HealthMonitor, ObservationStore
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
    observations: ObservationStore | None = None,
    health_interval: float = 5.0,
    health_timeout: float = 2.0,
) -> None:
    health_monitor = (
        HealthMonitor(
            routes,
            observations,
            interval=health_interval,
            timeout=health_timeout,
        )
        if observations is not None
        else None
    )
    runner = web.AppRunner(
        create_proxy_app(routes, settings, observations),
        shutdown_timeout=settings.shutdown_timeout,
        handler_cancellation=True,
    )
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        if health_monitor is not None:
            await health_monitor.start()
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
        if health_monitor is not None:
            await health_monitor.stop()
        await provider.stop()
        await runner.cleanup()
