import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from textual.containers import Horizontal, Vertical
from textual.widgets import Checkbox, DataTable, Input, OptionList, Select, Static

from tunnitup.mappings import MappingStore, SavedMapping
from tunnitup.observability import RequestEvent, RouteHealth
from tunnitup.providers.base import Tunnel
from tunnitup.routing import Route, RouteTable
from tunnitup.tui import (
    CommandCenterScreen,
    ConfirmDeleteScreen,
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
        assert app.screen.query_one("#live-strip", Horizontal).region.height == 2
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
        first = str(screen.query_one("#runtime-state", Static).render())
        screen._refresh_runtime_state()
        second = str(screen.query_one("#runtime-state", Static).render())

        assert "STARTING" in first
        assert first != second


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
        "Domain already online. Stop the other tunnel session, then press Start.  "
        "[dim]D: details[/]"
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
        app.screen.query_one("#launch-provider", Select).value = "outray"
        app.screen.query_one("#launch-url", Input).value = "public.test"
        await pilot.click("#launch")
        await pilot.pause(0.05)

        assert app.runtime_state == "online"
        assert app.runtime is not None
        assert app.runtime.tunnel.url == "https://public.test"
        assert app.runtime.tunnel.provider == "outray"
        assert app.tunnel is not None
        assert app.tunnel.public_url == "https://public.test"

        await pilot.press("s")
        await pilot.pause(0.05)

        assert app.runtime_state == "stopped"
        assert app.tunnel is None
        assert app.runtime_worker is None

    app.stop_stack()
    assert app.runtime_state == "stopped"


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


async def test_tui_imports_project_routes_into_global_mapping_store(tmp_path: Path) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    runtime = TuiRuntime(
        routes=RouteTable(
            [
                Route.parse("/=3000"),
                Route.parse("/api=8000", strip_prefix=True),
            ]
        )
    )

    app = TunnitupApp(runtime, mapping_store=store)

    assert len(app.mappings) == 2
    assert set(app.selected_mapping_names) == {mapping.name for mapping in app.mappings}
    assert store.load() == app.mappings

    reopened = TunnitupApp(mapping_store=store)
    assert reopened.mappings == app.mappings
    assert reopened.selected_mapping_names == ()


async def test_launch_can_explicitly_select_no_saved_mappings(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    store.save((SavedMapping("frontend", Route.parse("/=3000")),))
    observed_routes: list[RouteTable] = []

    async def fake_run(routes: RouteTable, *args: Any, **kwargs: Any) -> None:
        observed_routes.append(routes)
        kwargs["on_ready"](Tunnel("fake", "https://public.test", "http://127.0.0.1:8080"))
        await asyncio.Event().wait()

    monkeypatch.setattr("tunnitup.tui.create_provider", lambda _: object())
    monkeypatch.setattr("tunnitup.tui.run_proxy_with_tunnel", fake_run)
    app = TunnitupApp(mapping_store=store)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, LaunchScreen)
        assert app.screen.query_one("#launch-mapping-0", Checkbox).value is False

        await pilot.click("#launch")
        await pilot.pause(0.05)

        assert app.runtime_state == "online"
        assert observed_routes[0].routes == ()
        app.stop_stack()
        await pilot.pause(0.05)


def test_app_deletes_mapping_from_persistent_catalog(tmp_path: Path) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    mappings = (
        SavedMapping("frontend", Route.parse("/=3000")),
        SavedMapping("api", Route.parse("/api=8000", strip_prefix=True)),
    )
    store.save(mappings)
    app = TunnitupApp(mapping_store=store)

    app.delete_mapping("frontend")

    assert tuple(mapping.name for mapping in app.mappings) == ("api",)
    assert store.load() == app.mappings


def test_command_center_summarizes_outray_setup_errors() -> None:
    assert CommandCenterScreen._friendly_error("Outray was not found on PATH").startswith(
        "Outray is not installed"
    )
    assert CommandCenterScreen._friendly_error("Outray is not authenticated").startswith(
        "Outray needs authentication"
    )


async def test_tui_add_edit_delete_round_trip_is_persistent(tmp_path: Path) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    app = TunnitupApp(mapping_store=store)

    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, RouteEditorScreen)
        editor.query_one("#route-name", Input).value = "billing-api"
        editor.query_one("#route-path", Input).value = "/billing"
        editor.query_one("#route-upstream", Input).value = "9000"
        await pilot.click("#save")
        await pilot.pause()

        assert tuple(mapping.name for mapping in store.load()) == ("billing-api",)
        assert store.load()[0].route.strip_prefix is True

        await pilot.press("e")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, RouteEditorScreen)
        editor.query_one("#route-upstream", Input).value = "9001"
        await pilot.click("#save")
        await pilot.pause()

        assert store.load()[0].route.upstream.port == 9001

        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)
        await pilot.click("#cancel")
        await pilot.pause()
        assert tuple(mapping.name for mapping in store.load()) == ("billing-api",)

        await pilot.press("delete")
        await pilot.pause()
        await pilot.click("#confirm-remove")
        await pilot.pause()

        assert store.load() == ()
        assert app.mappings == ()


async def test_tui_launches_selected_saved_mappings_with_outray(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    store.save(
        (
            SavedMapping("frontend", Route.parse("/=5173")),
            SavedMapping("api", Route.parse("/api=8000", strip_prefix=True)),
        )
    )
    captured: dict[str, Any] = {}

    async def fake_run(routes: RouteTable, *args: Any, **kwargs: Any) -> None:
        captured["routes"] = routes
        kwargs["on_ready"](Tunnel("outray", "https://public.outray.app", args[0]))
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "tunnitup.tui.create_provider",
        lambda name: captured.setdefault("provider", name),
    )
    monkeypatch.setattr("tunnitup.tui.run_proxy_with_tunnel", fake_run)
    app = TunnitupApp(mapping_store=store)

    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        launch = app.screen
        assert isinstance(launch, LaunchScreen)
        launch.query_one("#launch-provider", Select).value = "outray"
        await pilot.click("#launch-mapping-0")
        await pilot.click("#launch-mapping-1")
        await pilot.click("#launch")
        await pilot.pause(0.05)

        assert app.runtime_state == "online"
        assert captured["provider"] == "outray"
        assert {route.path for route in captured["routes"].routes} == {"/", "/api"}
        assert app.runtime is not None
        assert app.runtime.tunnel.provider == "outray"

        app.stop_stack()
        await pilot.pause(0.05)


async def test_launch_rejects_two_active_mappings_for_the_same_public_path(
    tmp_path: Path,
) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    store.save(
        (
            SavedMapping("shop-ui", Route.parse("/=3000")),
            SavedMapping("admin-ui", Route.parse("/=5173")),
        )
    )
    app = TunnitupApp(mapping_store=store)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        launch = app.screen
        assert isinstance(launch, LaunchScreen)
        launch.query_one("#launch-mapping-0", Checkbox).value = True
        launch.query_one("#launch-mapping-1", Checkbox).value = True
        await pilot.click("#launch")
        await pilot.pause()

        assert isinstance(app.screen, CommandCenterScreen)
        assert app.runtime_state == "stopped"
        assert app.runtime is not None
        assert app.runtime.routes.routes == ()


async def test_launch_modal_fits_small_terminal_and_scrolls_many_mappings(
    tmp_path: Path,
) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    store.save(
        tuple(
            SavedMapping(f"service-{index}", Route.parse(f"/service-{index}={8000 + index}"))
            for index in range(15)
        )
    )
    app = TunnitupApp(mapping_store=store)

    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.press("s")
        await pilot.pause()

        launch = app.screen
        assert isinstance(launch, LaunchScreen)
        editor = launch.query_one("#launch-editor")
        mapping_list = launch.query_one("#launch-mappings")
        assert editor.region.x >= 0
        assert editor.region.y >= 0
        assert editor.region.right <= launch.size.width
        assert editor.region.bottom <= launch.size.height
        assert mapping_list.region.height <= 16
        assert mapping_list.virtual_size.height > mapping_list.region.height


async def test_tui_surfaces_occupied_proxy_port(
    monkeypatch: Any,
) -> None:
    listener = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]

    class FakeProvider:
        name = "fake"

        async def start(self, *_args: Any, **_kwargs: Any) -> Tunnel:
            raise AssertionError("provider must not start when proxy port is occupied")

        async def wait(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr("tunnitup.tui.create_provider", lambda _name: FakeProvider())
    app = TunnitupApp(
        TuiRuntime(
            routes=RouteTable([Route.parse("/=3000")]),
            port=port,
        )
    )

    try:
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.click("#launch")
            await pilot.pause(0.1)

            assert app.runtime_state == "error"
            assert app.runtime_error is not None
            assert f"Port {port} is already in use" in app.runtime_error
    finally:
        listener.close()
        await listener.wait_closed()
