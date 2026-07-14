from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import cast

from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option
from textual.worker import Worker

from tunnitup.config import ConfigurationError, TunnelSettings, normalize_tunnel_url
from tunnitup.observability import ActivityEvent, ObservationStore, RequestEvent, RouteHealth
from tunnitup.orchestration import run_proxy_with_tunnel
from tunnitup.providers import ProviderError, Tunnel, create_provider
from tunnitup.proxy import ProxySettings
from tunnitup.routing import Route, RouteConfigurationError, RouteTable


@dataclass(frozen=True, slots=True)
class TuiRuntime:
    routes: RouteTable
    host: str = "127.0.0.1"
    port: int = 8080
    settings: ProxySettings = ProxySettings()
    tunnel: TunnelSettings = TunnelSettings()
    source: Path | None = None


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    provider: str
    public_url: str | None
    proxy_port: int


class TunnitupScreen(Screen[None]):
    @property
    def tunnitup(self) -> TunnitupApp:
        return cast("TunnitupApp", self.app)


class LaunchScreen(ModalScreen[LaunchOptions | None]):
    """Provider-aware launch settings shown immediately before startup."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, runtime: TuiRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        with Vertical(id="launch-editor"):
            yield Static("START TUNNITUP", classes="modal-title")
            yield Static("Choose how this local proxy becomes public.", classes="modal-copy")
            yield Label("Tunnel provider", classes="field-label")
            yield Select(
                [("ngrok", "ngrok")], value=self.runtime.tunnel.provider, id="launch-provider"
            )
            yield Label("Static domain or URL (optional)", classes="field-label")
            yield Input(
                value=self.runtime.tunnel.url or "",
                placeholder="my-domain.ngrok-free.app",
                id="launch-url",
            )
            yield Label("Local proxy port", classes="field-label")
            yield Input(value=str(self.runtime.port), id="launch-port", type="integer")
            yield Static("", id="launch-error", classes="error")
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Start", id="launch", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#launch-url", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "launch":
            self.action_launch()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_launch(self) -> None:
        error = self.query_one("#launch-error", Static)
        try:
            provider = str(self.query_one("#launch-provider", Select).value)
            raw_url = self.query_one("#launch-url", Input).value.strip()
            public_url = None
            if raw_url:
                candidate = raw_url if "://" in raw_url else f"https://{raw_url}"
                public_url = normalize_tunnel_url(candidate)
            raw_port = self.query_one("#launch-port", Input).value.strip()
            proxy_port = int(raw_port)
            if not 1 <= proxy_port <= 65535:
                raise ValueError("proxy port must be between 1 and 65535")
        except (ConfigurationError, ValueError) as exc:
            error.update(str(exc))
            return
        self.dismiss(LaunchOptions(provider, public_url, proxy_port))


class RouteEditorScreen(ModalScreen[Route | None]):
    """Small route editor shared by the add and edit commands."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, route: Route | None = None, *, default_path: str = "/api") -> None:
        super().__init__()
        self.route = route
        self.default_path = default_path

    def compose(self) -> ComposeResult:
        with Vertical(id="route-editor"):
            yield Static("EDIT ROUTE" if self.route else "ADD ROUTE", classes="modal-title")
            yield Label("Public path", classes="field-label")
            yield Input(value=self.route.path if self.route else self.default_path, id="route-path")
            yield Label("Local port or URL", classes="field-label")
            yield Input(
                value=str(self.route.upstream) if self.route else "8000",
                id="route-upstream",
            )
            yield Static("", id="route-error", classes="error")
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save route", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#route-path", Input).focus()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.action_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "save":
            self.action_save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        try:
            route = Route.parse(
                f"{self.query_one('#route-path', Input).value}="
                f"{self.query_one('#route-upstream', Input).value}",
                strip_prefix=self.route.strip_prefix if self.route else False,
            )
        except RouteConfigurationError as exc:
            self.query_one("#route-error", Static).update(str(exc))
            return
        self.dismiss(route)


class TrafficTable(DataTable):
    """A log table whose marker updates with the native cursor, without event lag."""

    BINDINGS = [
        Binding("home", "first_log", show=False),
        Binding("end", "last_log", show=False),
    ]

    def action_first_log(self) -> None:
        if self.row_count:
            self.move_cursor(row=0)

    def action_last_log(self) -> None:
        if self.row_count:
            self.move_cursor(row=self.row_count - 1)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.action_cursor_up()
        event.prevent_default()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.action_cursor_down()
        event.prevent_default()

    def watch_cursor_coordinate(self, old: Coordinate, new: Coordinate) -> None:
        super().watch_cursor_coordinate(old, new)
        if not self.columns:
            return
        if old.row < self.row_count:
            self.update_cell_at(Coordinate(old.row, 0), "")
        if new.row < self.row_count:
            self.update_cell_at(Coordinate(new.row, 0), "▶")

    def sync_cursor_marker(self) -> None:
        for row_index in range(self.row_count):
            self.update_cell_at(
                Coordinate(row_index, 0),
                "▶" if row_index == self.cursor_row else "",
            )


class CommandCenterScreen(TunnitupScreen):
    BINDINGS = [
        Binding("s,space", "toggle", show=False),
        Binding("right", "focus_traffic", show=False, priority=True),
        Binding("left", "focus_routes", show=False, priority=True),
        Binding("a", "add_route", show=False),
        Binding("e", "edit_route", show=False),
        Binding("c", "copy_url", show=False),
        Binding("x", "clear_requests", show=False),
        Binding("d", "error_details", show=False),
        Binding("r", "refresh", show=False),
        Binding("question_mark", "help", show=False),
        Binding("q", "app.quit", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="command-topbar"):
            yield Static(self._project_label(), id="project-label")
            yield Button("▶  start", id="toggle", variant="primary")
        with Horizontal(id="live-strip"):
            yield Static("", id="runtime-state")
            yield Static("", id="public-url")
            yield Static("", id="runtime-meta")
        yield Static("", id="runtime-error", classes="error-banner")
        with Horizontal(id="dashboard"):
            with Vertical(id="routes-panel", classes="panel"):
                with Horizontal(classes="pane-heading"):
                    yield Static("", id="route-heading")
                yield OptionList(id="routes-list", compact=True)
            with Vertical(id="requests-panel", classes="panel"):
                with Horizontal(classes="pane-heading"):
                    yield Static("RECENT TRAFFIC", id="traffic-heading")
                    yield Static("0 REQ/MIN", id="request-rate")
                yield TrafficTable(
                    id="requests-table",
                    cursor_type="row",
                    show_cursor=True,
                    header_height=1,
                )
        yield Static(
            "[bold #9fc8ef]↑↓[/] navigate    [bold #9fc8ef]←→[/] routes/traffic    "
            "[bold #9fc8ef]a[/] add    [bold #9fc8ef]e[/] edit    "
            "[bold #9fc8ef]c[/] copy URL    [bold #9fc8ef]x[/] clear    "
            "[bold #9fc8ef]s[/] start/stop    [bold #9fc8ef]?[/] help",
            id="keybar",
        )

    def on_mount(self) -> None:
        self._starting_frame = 0
        self._traffic_route_path: str | None = None
        self._traffic_events: tuple[RequestEvent, ...] = ()
        requests = self.query_one("#requests-table", DataTable)
        self._traffic_marker_column = requests.add_column("", width=2)
        requests.add_column("Time", width=9)
        requests.add_column("Method", width=8)
        self._traffic_path_column = requests.add_column("Path", width=32)
        requests.add_column("Code", width=8)
        requests.add_column("Latency", width=10)
        self.refresh_dashboard()
        self.call_after_refresh(self._resize_traffic_columns)
        self.set_interval(1, self.refresh_dashboard)
        self.set_interval(0.16, self._refresh_runtime_state)
        self.observe()

    def on_resize(self, _: events.Resize) -> None:
        self.call_after_refresh(self._resize_traffic_columns)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle":
            self.action_toggle()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "routes-list":
            self._refresh_traffic()

    def action_toggle(self) -> None:
        if self.tunnitup.runtime_state in {"starting", "online"}:
            self.tunnitup.stop_stack()
        else:
            runtime = self.tunnitup.runtime
            if runtime is None or not runtime.routes.routes:
                self.notify(
                    "Add at least one upstream route with A before starting.",
                    severity="warning",
                    timeout=2,
                )
                return
            self.app.push_screen(LaunchScreen(runtime), self._launch)

    def action_refresh(self) -> None:
        self.refresh_dashboard()

    def action_error_details(self) -> None:
        if self.tunnitup.runtime_error:
            self.notify(
                self.tunnitup.runtime_error,
                title="Tunnel error",
                severity="error",
                timeout=4,
            )

    def action_add_route(self) -> None:
        if not self._routes_are_editable():
            return
        runtime = self.tunnitup.runtime
        default_path = "/" if runtime is None or not runtime.routes.routes else "/api"
        self.app.push_screen(RouteEditorScreen(default_path=default_path), self._add_route)

    def _launch(self, options: LaunchOptions | None) -> None:
        runtime = self.tunnitup.runtime
        if options is None or runtime is None:
            return
        self.tunnitup.runtime = replace(
            runtime,
            port=options.proxy_port,
            tunnel=replace(
                runtime.tunnel,
                provider=options.provider,
                url=options.public_url,
            ),
        )
        self.tunnitup.start_stack()

    def action_edit_route(self) -> None:
        if not self._routes_are_editable():
            return
        selected = self._selected_route()
        if selected is not None:
            self.app.push_screen(RouteEditorScreen(selected), self._replace_route)

    def action_focus_traffic(self) -> None:
        table = self.query_one("#requests-table", DataTable)
        if table.row_count:
            table.focus()

    def action_focus_routes(self) -> None:
        self.query_one("#routes-list", OptionList).focus()

    def action_copy_url(self) -> None:
        if self.tunnitup.tunnel is None:
            self.notify(
                "Start Tunnitup before copying the public URL.",
                severity="warning",
                timeout=2,
            )
            return
        self.app.copy_to_clipboard(self.tunnitup.tunnel.public_url)
        self.notify("Public URL copied.", timeout=1.5)

    def action_clear_requests(self) -> None:
        count = len(self.tunnitup.observations.requests)
        self.tunnitup.observations.clear_requests()
        self.refresh_dashboard()
        noun = "request" if count == 1 else "requests"
        self.notify(f"Cleared {count} captured {noun}.", timeout=1.5)

    def action_help(self) -> None:
        self.notify(
            "↑↓ navigate · ←→ switch pane · A add · E edit · C copy URL · "
            "X clear traffic · S start/stop · Q quit",
            title="Keyboard controls",
            timeout=3,
        )

    def _routes_are_editable(self) -> bool:
        if self.tunnitup.runtime_state in {"starting", "online", "stopping"}:
            self.notify(
                "Stop Tunnitup before changing routes.",
                severity="warning",
                timeout=2,
            )
            return False
        return True

    def _selected_route(self) -> Route | None:
        runtime = self.tunnitup.runtime
        route_list = self.query_one("#routes-list", OptionList)
        if runtime is None or route_list.highlighted is None:
            return None
        path = route_list.get_option_at_index(route_list.highlighted).id
        return next((route for route in runtime.routes.routes if route.path == path), None)

    def _add_route(self, route: Route | None) -> None:
        if route is None:
            return
        self._update_routes(route)

    def _replace_route(self, route: Route | None) -> None:
        selected = self._selected_route()
        if route is None or selected is None:
            return
        self._update_routes(route, replacing=selected.path)

    def _update_routes(self, route: Route, *, replacing: str | None = None) -> None:
        runtime = self.tunnitup.runtime
        if runtime is None:
            return
        routes = [item for item in runtime.routes.routes if item.path != replacing]
        routes.append(route)
        try:
            self.tunnitup.runtime = replace(runtime, routes=RouteTable(routes))
        except RouteConfigurationError as exc:
            self.notify(str(exc), severity="error", timeout=3)
            return
        self.refresh_dashboard()

    @work(exclusive=True, exit_on_error=False)
    async def observe(self) -> None:
        async with self.tunnitup.observations.subscribe() as queue:
            while True:
                observation = await queue.get()
                if isinstance(observation, RequestEvent | RouteHealth | ActivityEvent):
                    self.refresh_dashboard()

    def _refresh_runtime_state(self) -> None:
        state = self.tunnitup.runtime_state.upper()
        if state == "STARTING":
            matrix = ("●·····", "·●····", "··●···", "···●··", "····●·", "·····●")
            phase = self._starting_frame % len(matrix)
            display = Text()
            for dot in matrix[phase]:
                color = "#5ac8fa" if dot == "●" else "#285b88"
                display.append(dot, style=f"bold {color}")
            display.append(" STARTING", style="bold #4a9be8")
            self._starting_frame += 1
        else:
            color = {
                "ONLINE": "#5ac8fa",
                "ERROR": "#ef8d84",
                "STOPPING": "#f2c66d",
            }.get(state, "#4a9be8")
            display = Text(f"■ {state}", style=f"bold {color}")
        self.query_one("#runtime-state", Static).update(display)

    def refresh_dashboard(self) -> None:
        runtime = self.tunnitup.runtime
        if runtime is None:
            return
        public = self.tunnitup.tunnel.public_url if self.tunnitup.tunnel else "not connected"
        self._refresh_runtime_state()
        self.query_one("#public-url", Static).update(public)
        self.query_one("#runtime-meta", Static).update(
            f"provider [bold]{runtime.tunnel.provider}[/]    uptime [bold]{self._uptime()}[/]"
        )
        button = self.query_one("#toggle", Button)
        button.label = (
            "■  stop" if self.tunnitup.runtime_state in {"starting", "online"} else "▶  start"
        )
        button.variant = (
            "error" if self.tunnitup.runtime_state in {"starting", "online"} else "primary"
        )
        error_banner = self.query_one("#runtime-error", Static)
        error_banner.update(self._friendly_error(self.tunnitup.runtime_error))
        error_banner.display = self.tunnitup.runtime_error is not None

        health = {item.route_path: item for item in self.tunnitup.observations.health}
        route_list = self.query_one("#routes-list", OptionList)
        selected = self._selected_route()
        route_list.clear_options()
        sorted_routes = sorted(runtime.routes.routes, key=lambda item: item.path)
        for route in sorted_routes:
            result = health.get(route.path)
            state_text = self._health_indicator(result)
            route_list.add_option(Option(self._route_row(route, state_text), id=route.path))
        route_paths = [route.path for route in sorted_routes]
        if route_paths:
            route_list.highlighted = (
                route_paths.index(selected.path)
                if selected is not None and selected.path in route_paths
                else 0
            )
        healthy_count = sum(item.healthy for item in health.values())
        self.query_one("#route-heading", Static).update(
            f"ROUTES  {healthy_count}/{len(runtime.routes.routes)} HEALTHY"
        )

        self._refresh_traffic()

    def _refresh_traffic(self) -> None:
        request_table = self.query_one("#requests-table", TrafficTable)
        selected = self._selected_route()
        selected_path = selected.path if selected is not None else None
        route_changed = selected_path != self._traffic_route_path
        self._traffic_route_path = selected_path
        events = tuple(
            sorted(
                (
                    event
                    for event in self.tunnitup.observations.requests
                    if event.route_path == selected_path
                ),
                key=lambda event: event.timestamp,
            )[-100:]
        )
        previous = self._traffic_events
        was_following = bool(previous) and request_table.cursor_row == len(previous) - 1

        if route_changed or not self._update_traffic_rows(request_table, previous, events):
            request_table.clear()
            for event in events:
                self._add_traffic_row(request_table, event)
            if events:
                request_table.move_cursor(row=len(events) - 1)
        elif was_following and events:
            request_table.move_cursor(row=len(events) - 1)

        self._traffic_events = events
        request_table.sync_cursor_marker()
        cutoff = datetime.now(UTC) - timedelta(minutes=1)
        rate = sum(event.timestamp >= cutoff for event in events)
        self.query_one("#request-rate", Static).update(f"{rate} REQ/MIN")

    def _update_traffic_rows(
        self,
        table: TrafficTable,
        previous: tuple[RequestEvent, ...],
        current: tuple[RequestEvent, ...],
    ) -> bool:
        if current == previous:
            return True
        shift = next(
            (
                offset
                for offset in range(len(previous) + 1)
                if previous[offset:] == current[: len(previous) - offset]
            ),
            None,
        )
        if shift is None:
            return False
        for _ in range(shift):
            row_key = table.coordinate_to_cell_key(Coordinate(0, 0)).row_key
            table.remove_row(row_key)
        for event in current[len(previous) - shift :]:
            self._add_traffic_row(table, event)
        return True

    def _add_traffic_row(self, table: TrafficTable, event: RequestEvent) -> None:
        table.add_row(
            "",
            event.timestamp.astimezone().strftime("%H:%M:%S"),
            self._method_cell(event.method),
            event.path,
            self._status_cell(event.status),
            f"{event.duration_ms:.0f}ms",
            key=str(id(event)),
            height=1,
        )

    def _resize_traffic_columns(self) -> None:
        table = self.query_one("#requests-table", DataTable)
        fixed_columns = 2 + 9 + 8 + 8 + 10
        cell_padding = table.cell_padding * 2 * 6
        path_width = max(24, table.size.width - fixed_columns - cell_padding - 1)
        column = table.columns[self._traffic_path_column]
        if column.width != path_width:
            column.width = path_width
            table.refresh(layout=True)

    @classmethod
    def _route_row(cls, route: Route, health: Text) -> Table:
        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(width=12, no_wrap=True)
        row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        row.add_column(width=2, justify="right")
        row.add_row(
            Text(route.path, style="bold"),
            Text(cls._display_upstream(route)),
            health,
        )
        return row

    @staticmethod
    def _method_cell(method: str) -> Text:
        normalized = method.upper()
        color = {
            "GET": "#5ac8fa",
            "POST": "#72d39a",
            "PUT": "#f2c66d",
            "PATCH": "#e6a85c",
            "DELETE": "#ef8d84",
            "OPTIONS": "#c79bf2",
            "HEAD": "#9fc8ef",
        }.get(normalized, "#91a4bb")
        return Text(normalized, style=f"bold {color}")

    @staticmethod
    def _status_cell(status: int) -> Text:
        if 200 <= status < 300:
            color = "#72d39a"
        elif 300 <= status < 400:
            color = "#77b7f2"
        elif 400 <= status < 500:
            color = "#f2c66d"
        elif status >= 500:
            color = "#ef8d84"
        else:
            color = "#91a4bb"
        return Text(str(status), style=f"bold {color}")

    @staticmethod
    def _health_indicator(result: RouteHealth | None) -> Text:
        if result is None:
            color = "#5d7390"
        elif result.healthy:
            color = "#5ac8fa"
        else:
            color = "#ef8d84"
        return Text("■", style=color)

    def _project_label(self) -> str:
        source = self.tunnitup.runtime.source if self.tunnitup.runtime else None
        project = source.parent if source and source.suffix else source or Path.cwd()
        try:
            display = f"~/{project.resolve().relative_to(Path.home().resolve()).as_posix()}"
        except ValueError:
            display = str(project)
        return f"tunnitup · {display}"

    def _uptime(self) -> str:
        if self.tunnitup.online_since is None:
            return "00:00"
        seconds = max(0, int(monotonic() - self.tunnitup.online_since))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02}:{minutes:02}:{seconds:02}"
        return f"{minutes:02}:{seconds:02}"

    @staticmethod
    def _display_upstream(route: Route) -> str:
        host = "localhost" if route.upstream.host in {"127.0.0.1", "::1"} else route.upstream.host
        port = f":{route.upstream.port}" if route.upstream.port else ""
        path = route.upstream.path.rstrip("/")
        return f"{host}{port}{path}"

    @staticmethod
    def _friendly_error(error: str | None) -> str:
        if error is None:
            return ""
        lowered = error.lower()
        if "already online" in lowered:
            return (
                "Domain already online. Stop the other ngrok session, then press Start.  "
                "[dim]D: details[/]"
            )
        if "authtoken" in lowered or "authentication" in lowered:
            return (
                "ngrok needs authentication. Run: ngrok config add-authtoken <token>  "
                "[dim]D: details[/]"
            )
        if "not found on path" in lowered:
            return "ngrok is not installed or is not on PATH.  [dim]D: details[/]"
        first_line = next((line.strip() for line in error.splitlines() if line.strip()), error)
        return f"{first_line[:140]}  [dim]D: details[/]"


class TunnitupApp(App[None]):
    TITLE = "Tunnitup"
    SUB_TITLE = "one domain, many local services"
    CSS = """
    Screen {
        background: #171f2b;
        color: #e6edf5;
    }

    .topbar, Footer {
        background: #101721;
        color: #a8b6c8;
    }

    .topbar {
        height: 3;
        padding: 1 2;
        text-style: bold;
    }

    #setup-shell {
        width: 76;
        max-width: 100%;
        height: auto;
        margin: 2 4;
        padding: 2 3;
        border: solid #34465c;
        background: #1d2836;
    }

    .step-label, .panel-title {
        color: #4a9be8;
        text-style: bold;
        margin-bottom: 1;
    }

    .screen-title {
        color: #e8f1fa;
        text-style: bold;
        margin-bottom: 1;
    }

    .screen-copy {
        color: #9aa8ba;
        margin-bottom: 2;
    }

    Input {
        background: #172332;
        color: #e8f1fa;
        border: solid #5d7390;
        margin-bottom: 1;
    }

    Input:focus { border: solid #4a9be8; }

    Button {
        border: solid #5d7390;
        background: #1d2b3b;
        color: #e6edf5;
        min-width: 16;
    }

    Button.-primary {
        background: #1863a9;
        color: #ffffff;
        border: solid #4a9be8;
    }

    Button.-error {
        background: #4c2c31;
        color: #f6d1ca;
        border: solid #a85d52;
    }

    .error {
        color: #ef8d84;
        min-height: 1;
        margin: 1 0;
    }

    .service-row {
        height: auto;
        min-height: 5;
        border-bottom: solid #34465c;
        padding: 1 0;
    }

    .service { width: 2fr; }
    .path-input { width: 1fr; }

    .actions {
        height: auto;
        margin-top: 2;
        align-horizontal: right;
    }

    .actions Button { margin-left: 1; }

    #preview-list {
        height: auto;
        border: solid #34465c;
    }

    .preview-row {
        height: auto;
        padding: 1 2;
        border-bottom: solid #27394e;
    }

    #command-topbar {
        height: 4;
        padding: 0 1;
        background: #1d2b3b;
        border-bottom: solid #34465c;
        align-vertical: middle;
    }

    #project-label {
        width: 1fr;
        color: #b7c9df;
        text-style: bold;
        content-align: left middle;
    }

    #command-topbar #toggle {
        width: 12;
        min-width: 12;
        height: 3;
        margin: 0;
    }

    #live-strip {
        height: 2;
        padding: 0 1;
        background: #101721;
        border-bottom: solid #34465c;
        align-vertical: middle;
    }

    #runtime-state {
        width: 18;
        text-overflow: ellipsis;
    }

    #public-url {
        width: 1fr;
        color: #77b7f2;
        text-overflow: ellipsis;
    }

    #runtime-meta {
        width: 38;
        color: #9fc0e5;
        text-align: left;
        text-overflow: ellipsis;
    }

    .error-banner {
        display: none;
        height: 3;
        max-height: 3;
        padding: 0 2;
        color: #ef8d84;
        background: #2b202a;
        border-bottom: solid #5f3b47;
        text-overflow: ellipsis;
        content-align: left middle;
    }

    #dashboard { height: 1fr; }

    .panel {
        padding: 0;
    }

    #routes-panel { width: 34%; border-right: solid #34465c; }
    #requests-panel { width: 66%; }

    .pane-heading {
        height: 2;
        padding: 0 1;
        background: #101721;
        color: #9fc0e5;
        border-bottom: solid #34465c;
        align-vertical: middle;
    }

    #route-heading, #traffic-heading {
        width: 1fr;
        content-align: left middle;
    }

    #request-rate {
        width: 16;
        text-align: right;
        content-align: right middle;
    }

    #routes-list {
        height: 1fr;
        padding: 0;
        background: #171f2b;
        color: #dce7f2;
        scrollbar-size: 1 1;
    }

    #routes-list > .option-list--option {
        padding: 0 1;
    }

    #routes-list > .option-list--option-highlighted {
        background: #1863a9;
        color: #ffffff;
    }

    #routes-list > .option-list--option-hover {
        background: #1d4f7c;
    }

    DataTable {
        height: 1fr;
        background: #171f2b;
        color: #dce7f2;
    }

    DataTable > .datatable--header {
        background: #1d2b3b;
        color: #91a4bb;
    }

    DataTable > .datatable--cursor {
        background: #1863a9;
        color: #ffffff;
    }

    #keybar {
        height: 2;
        padding: 0 1;
        background: #1d2b3b;
        color: #9fc0e5;
        border-top: solid #34465c;
        content-align: left middle;
    }

    RouteEditorScreen, LaunchScreen {
        align: center middle;
        background: #000000 50%;
    }

    #route-editor, #launch-editor {
        width: 58;
        height: auto;
        padding: 1 2;
        background: #1d2836;
        border: solid #5d7390;
    }

    .modal-title {
        color: #9fc8ef;
        text-style: bold;
        margin-bottom: 1;
    }

    .field-label {
        color: #9aa8ba;
    }

    .modal-copy {
        color: #9aa8ba;
        margin-bottom: 1;
    }

    Select {
        margin-bottom: 1;
        border: solid #5d7390;
        background: #172332;
    }
    """

    def __init__(
        self,
        runtime: TuiRuntime | None = None,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.observations = ObservationStore()
        self.runtime_state = "stopped"
        self.runtime_error: str | None = None
        self.tunnel: Tunnel | None = None
        self.online_since: float | None = None
        self.runtime_worker: Worker[None] | None = None

    def on_mount(self) -> None:
        if self.runtime is None:
            self.runtime = TuiRuntime(routes=RouteTable([]))
        self.push_screen(CommandCenterScreen())

    def start_stack(self) -> None:
        if self.runtime is None or self.runtime_state in {"starting", "online"}:
            return
        self.runtime_error = None
        self.runtime_state = "starting"
        self.refresh_command_center()
        self.runtime_worker = self.run_stack()

    def stop_stack(self) -> None:
        if self.runtime_worker is None:
            return
        self.runtime_state = "stopping"
        self.runtime_worker.cancel()
        self.refresh_command_center()

    @work(group="runtime", exclusive=True, exit_on_error=False)
    async def run_stack(self) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        try:
            provider = create_provider(runtime.tunnel.provider)

            def on_ready(tunnel: Tunnel) -> None:
                self.tunnel = tunnel
                self.online_since = monotonic()
                self.runtime_state = "online"
                self.refresh_command_center()

            await run_proxy_with_tunnel(
                runtime.routes,
                runtime.host,
                runtime.port,
                runtime.settings,
                provider,
                public_url=runtime.tunnel.url,
                startup_timeout=runtime.tunnel.startup_timeout,
                on_ready=on_ready,
                observations=self.observations,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, ProviderError, RouteConfigurationError) as exc:
            self.runtime_error = str(exc)
            self.runtime_state = "error"
        finally:
            if self.runtime_state not in {"error"}:
                self.runtime_state = "stopped"
            self.tunnel = None
            self.online_since = None
            self.refresh_command_center()

    def refresh_command_center(self) -> None:
        if isinstance(self.screen, CommandCenterScreen):
            self.screen.refresh_dashboard()
