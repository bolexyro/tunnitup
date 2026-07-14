import asyncio
from typing import Any

from textual.widgets import DataTable, Input

from tunnitup.discovery import ServiceProbe
from tunnitup.providers.base import Tunnel
from tunnitup.routing import Route, RouteTable
from tunnitup.tui import (
    CommandCenterScreen,
    PathsScreen,
    PortsScreen,
    PreviewScreen,
    TuiRuntime,
    TunnitupApp,
)


async def fake_probe(ports: tuple[int, ...]) -> tuple[ServiceProbe, ...]:
    return tuple(
        ServiceProbe(
            port=port,
            reachable=True,
            kind="frontend" if index == 0 else "api",
            detail="test service",
            suggested_path="/" if index == 0 else "/api",
        )
        for index, port in enumerate(ports)
    )


async def test_tui_guides_ports_to_editable_paths_and_preview() -> None:
    app = TunnitupApp(probe_function=fake_probe)

    async with app.run_test(size=(100, 36)) as pilot:
        assert isinstance(app.screen, PortsScreen)
        ports = app.screen.query_one("#ports", Input)
        ports.value = "3000, 8000"
        await pilot.click("#probe")
        await pilot.pause()

        assert isinstance(app.screen, PathsScreen)
        app.screen.query_one("#path-8000", Input).value = "/backend"
        await pilot.click("#preview")
        await pilot.pause()

        assert isinstance(app.screen, PreviewScreen)
        assert [route.path for route in app.screen.routes.routes] == ["/backend", "/"]


async def test_tui_opens_command_center_for_existing_runtime() -> None:
    runtime = TuiRuntime(routes=RouteTable([Route.parse("/=3000")]))
    app = TunnitupApp(runtime)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, CommandCenterScreen)
        assert app.screen.query_one("#routes-table", DataTable).row_count == 1
        assert app.screen.query_one("#requests-table", DataTable).row_count == 0


async def test_command_center_starts_and_stops_the_runtime(monkeypatch: Any) -> None:
    async def fake_run(*args: Any, **kwargs: Any) -> None:
        kwargs["on_ready"](Tunnel("fake", "https://public.test", "http://127.0.0.1:8080"))
        await asyncio.Event().wait()

    monkeypatch.setattr("tunnitup.tui.create_provider", lambda _: object())
    monkeypatch.setattr("tunnitup.tui.run_proxy_with_tunnel", fake_run)
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#toggle")
        await pilot.pause(0.05)

        assert app.runtime_state == "online"
        assert app.tunnel is not None
        assert app.tunnel.public_url == "https://public.test"

        app.screen.action_toggle()
        await pilot.pause(0.05)

        assert app.runtime_state == "stopped"
        assert app.tunnel is None
