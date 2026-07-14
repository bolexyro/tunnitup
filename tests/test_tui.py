import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from textual.containers import Horizontal, Vertical
from textual.widgets import Checkbox, DataTable, Input, OptionList, Static

from tunnitup.observability import RequestEvent, RouteHealth
from tunnitup.providers.base import Tunnel
from tunnitup.routing import Route, RouteTable
from tunnitup.tui import (
    CommandCenterScreen,
    LaunchScreen,
    RouteEditorScreen,
    TuiRuntime,
    TunnitupApp,
)


async def test_tui_opens_empty_command_center_and_adds_first_root_route() -> None:
    app = TunnitupApp()

    async with app.run_test(size=(100, 36)) as pilot:
        assert isinstance(app.screen, CommandCenterScreen)
        assert app.runtime is not None
        assert app.runtime.routes.routes == ()

        await pilot.press("a")
        await pilot.pause()

        assert isinstance(app.screen, RouteEditorScreen)
        assert app.screen.query_one("#route-path", Input).value == "/"
        app.screen.query_one("#route-upstream", Input).value = "3000"
        await pilot.click("#save")
        await pilot.pause()

        assert isinstance(app.screen, CommandCenterScreen)
        assert app.runtime.routes.match("/").upstream.port == 3000

        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, LaunchScreen)


async def test_tui_opens_command_center_for_existing_runtime() -> None:
    runtime = TuiRuntime(routes=RouteTable([Route.parse("/=3000")]))
    app = TunnitupApp(runtime)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, CommandCenterScreen)
        route_list = app.screen.query_one("#routes-list", OptionList)
        assert route_list.option_count == 1
        assert app.screen.query_one("#requests-table", DataTable).row_count == 0
        assert "STOPPED" in str(app.screen.query_one("#runtime-state", Static).render())
        topbar = app.screen.query_one("#command-topbar", Horizontal)
        assert topbar.region.height == 4
        assert topbar.region.width == 120
        assert app.screen.query_one("#live-strip", Horizontal).region.height == 3
        assert not app.screen.query("#add-route")
        assert "start/stop" in str(app.screen.query_one("#keybar", Static).render())
        routes_panel = app.screen.query_one("#routes-panel", Vertical)
        routes_width = routes_panel.region.width
        assert route_list.region.width >= routes_width - 1
        requests_width = app.screen.query_one("#requests-panel", Vertical).region.width
        assert routes_width < requests_width
        assert 0.32 <= routes_width / (routes_width + requests_width) <= 0.40


async def test_command_center_clears_captured_traffic() -> None:
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))
    app.observations.record(
        RequestEvent(
            timestamp=datetime.now(UTC),
            method="GET",
            path="/",
            route_path="/",
            upstream="http://127.0.0.1:3000",
            status=200,
            duration_ms=1,
        )
    )

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#requests-table", DataTable).row_count == 1

        await pilot.press("x")
        await pilot.pause()

        assert app.observations.requests == ()
        assert app.screen.query_one("#requests-table", DataTable).row_count == 0


async def test_command_center_filters_traffic_as_route_highlight_moves() -> None:
    routes = RouteTable([Route.parse("/=3000"), Route.parse("/api=8000")])
    app = TunnitupApp(TuiRuntime(routes=routes))
    now = datetime.now(UTC)
    for path, route_path in (("/home", "/"), ("/api/users", "/api")):
        app.observations.record(
            RequestEvent(
                timestamp=now,
                method="GET",
                path=path,
                route_path=route_path,
                upstream="http://127.0.0.1:3000",
                status=200,
                duration_ms=1,
            )
        )

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#requests-table", DataTable)
        assert table.get_row_at(0)[3] == "/home"

        app.screen.query_one("#routes-list", OptionList).highlighted = 1
        await pilot.pause()

        assert table.row_count == 1
        assert table.get_row_at(0)[3] == "/api/users"


async def test_command_center_moves_active_traffic_marker() -> None:
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))
    started = datetime.now(UTC)
    for index in range(40):
        app.observations.record(
            RequestEvent(
                timestamp=started + timedelta(milliseconds=index),
                method="GET",
                path=f"/{index}",
                route_path="/",
                upstream="http://127.0.0.1:3000",
                status=200,
                duration_ms=1,
            )
        )

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#requests-table", DataTable)
        assert table.get_row_at(0)[3] == "/0"
        assert table.get_row_at(39)[3] == "/39"
        assert table.get_row_at(39)[0] == "▶"

        await pilot.press("right", "home")
        await pilot.pause()

        assert table.has_focus
        assert table.get_row_at(0)[0] == "▶"
        assert table.get_row_at(39)[0] == ""

        await pilot.press("end")
        await pilot.pause()

        assert table.get_row_at(0)[0] == ""
        assert table.get_row_at(39)[0] == "▶"
        assert table.scroll_y > 0


async def test_command_center_animates_the_starting_state() -> None:
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))
    app.runtime_state = "starting"

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen._starting_frame = 0
        screen._refresh_runtime_state()
        first_render = screen.query_one("#runtime-state", Static).render()
        first = str(first_render)
        first_styles = tuple(str(span.style) for span in first_render.spans)
        screen._refresh_runtime_state()
        second_render = screen.query_one("#runtime-state", Static).render()
        second_styles = tuple(str(span.style) for span in second_render.spans)

        assert "STARTING" in first
        assert first.count("•") == 14
        assert "\n" in first
        assert screen.query_one("#runtime-state", Static).region.height == 2
        assert first_styles != second_styles


def test_command_center_colors_http_methods_and_status_classes() -> None:
    assert CommandCenterScreen._method_cell("get").style == "bold #5ac8fa"
    assert CommandCenterScreen._method_cell("POST").style == "bold #72d39a"
    assert CommandCenterScreen._method_cell("DELETE").style == "bold #ef8d84"

    assert CommandCenterScreen._status_cell(204).style == "bold #72d39a"
    assert CommandCenterScreen._status_cell(302).style == "bold #77b7f2"
    assert CommandCenterScreen._status_cell(404).style == "bold #f2c66d"
    assert CommandCenterScreen._status_cell(503).style == "bold #ef8d84"


def test_command_center_uses_semantic_square_health_indicators() -> None:
    now = datetime.now(UTC)
    healthy = RouteHealth(now, "/", "localhost:3000", True, 200, 1.0)
    unhealthy = RouteHealth(now, "/", "localhost:3000", False, 500, 1.0)

    assert CommandCenterScreen._health_indicator(None).plain == "■"
    assert CommandCenterScreen._health_indicator(None).style == "#5d7390"
    assert CommandCenterScreen._health_indicator(healthy).style == "#5ac8fa"
    assert CommandCenterScreen._health_indicator(unhealthy).style == "#ef8d84"


def test_command_center_summarizes_known_ngrok_errors() -> None:
    raw = (
        "ngrok exited unexpectedly with code 1\n"
        "ERROR: endpoint https://example.ngrok.app is already online"
    )

    assert CommandCenterScreen._friendly_error(raw) == (
        "Domain already online. Stop the other ngrok session, then press Start.  [dim]D: details[/]"
    )


async def test_command_center_starts_and_stops_the_runtime(monkeypatch: Any) -> None:
    async def fake_run(*args: Any, **kwargs: Any) -> None:
        kwargs["on_ready"](Tunnel("fake", "https://public.test", "http://127.0.0.1:8080"))
        await asyncio.Event().wait()

    monkeypatch.setattr("tunnitup.tui.create_provider", lambda _: object())
    monkeypatch.setattr("tunnitup.tui.run_proxy_with_tunnel", fake_run)
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#toggle")
        await pilot.pause()
        assert isinstance(app.screen, LaunchScreen)
        app.screen.query_one("#launch-url", Input).value = "public.test"
        await pilot.click("#launch")
        await pilot.pause(0.05)

        assert app.runtime_state == "online"
        assert app.runtime is not None
        assert app.runtime.tunnel.url == "https://public.test"
        assert app.tunnel is not None
        assert app.tunnel.public_url == "https://public.test"

        await pilot.press("s")
        await pilot.pause(0.05)

        assert app.runtime_state == "stopped"
        assert app.tunnel is None


async def test_command_center_adds_a_route_while_stopped() -> None:
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("a")
        await pilot.pause()

        assert isinstance(app.screen, RouteEditorScreen)
        app.screen.query_one("#route-path", Input).value = "/api"
        app.screen.query_one("#route-upstream", Input).value = "8000"
        await pilot.click("#save")
        await pilot.pause()

        assert isinstance(app.screen, CommandCenterScreen)
        assert app.runtime is not None
        assert {route.path for route in app.runtime.routes.routes} == {"/", "/api"}


async def test_new_non_root_route_strips_its_public_prefix_by_default() -> None:
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=5173")])))

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("a")
        await pilot.pause()

        editor = app.screen
        assert isinstance(editor, RouteEditorScreen)
        assert editor.query_one("#route-strip-prefix", Checkbox).value is True
        editor.query_one("#route-path", Input).value = "/astropay-api"
        editor.query_one("#route-upstream", Input).value = "3009"
        await pilot.click("#save")
        await pilot.pause()

        assert app.runtime is not None
        route = app.runtime.routes.match("/astropay-api/docs")
        assert route is not None
        assert route.strip_prefix is True
        assert str(route.target_url("/astropay-api/docs")) == "http://localhost:3009/docs"


async def test_ctrl_c_stops_the_runtime_before_exiting(monkeypatch: Any) -> None:
    stopped = asyncio.Event()

    async def fake_run(*args: Any, **kwargs: Any) -> None:
        kwargs["on_ready"](Tunnel("fake", "https://public.test", "http://localhost:8080"))
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("tunnitup.tui.create_provider", lambda _: object())
    monkeypatch.setattr("tunnitup.tui.run_proxy_with_tunnel", fake_run)
    app = TunnitupApp(TuiRuntime(routes=RouteTable([Route.parse("/=3000")])))

    async with app.run_test(size=(120, 36)) as pilot:
        app.start_stack()
        await pilot.pause(0.05)
        assert app.runtime_state == "online"

        await pilot.press("ctrl+c")
        await asyncio.wait_for(stopped.wait(), timeout=1)

    assert app.runtime_state == "stopped"
