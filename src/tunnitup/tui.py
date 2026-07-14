from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import cast

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static
from textual.worker import Worker

from tunnitup.config import TunnelSettings
from tunnitup.discovery import PortInputError, ServiceProbe, parse_ports, probe_ports
from tunnitup.observability import ActivityEvent, ObservationStore, RequestEvent, RouteHealth
from tunnitup.orchestration import run_proxy_with_tunnel
from tunnitup.providers import ProviderError, Tunnel, create_provider
from tunnitup.proxy import ProxySettings
from tunnitup.routing import Route, RouteConfigurationError, RouteTable, normalize_path

ProbeFunction = Callable[[tuple[int, ...]], Awaitable[tuple[ServiceProbe, ...]]]


@dataclass(frozen=True, slots=True)
class TuiRuntime:
    routes: RouteTable
    host: str = "127.0.0.1"
    port: int = 8080
    settings: ProxySettings = ProxySettings()
    tunnel: TunnelSettings = TunnelSettings()
    source: Path | None = None


class TunnitupScreen(Screen[None]):
    @property
    def tunnitup(self) -> TunnitupApp:
        return cast("TunnitupApp", self.app)


class PortsScreen(TunnitupScreen):
    BINDINGS = [("escape", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Static("TUNNITUP   one domain, many local services", classes="topbar")
        with Vertical(id="setup-shell"):
            yield Label("01 / PORTS", classes="step-label")
            yield Static("Which ports are your services using?", classes="screen-title")
            yield Static(
                "Tunnitup will probe only these localhost ports. Nothing becomes public yet.",
                classes="screen-copy",
            )
            yield Input(
                value="3000, 8000",
                placeholder="3000, 8000, 4000",
                id="ports",
            )
            yield Static("", id="ports-error", classes="error")
            yield Button("Probe ports  →", id="probe", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ports", Input).focus()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.action_probe()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "probe":
            self.action_probe()

    def action_probe(self) -> None:
        error = self.query_one("#ports-error", Static)
        try:
            ports = parse_ports(self.query_one("#ports", Input).value)
        except PortInputError as exc:
            error.update(str(exc))
            return
        error.update("")
        self.query_one("#probe", Button).disabled = True
        self.query_one("#probe", Button).label = "Probing…"
        self.probe_services(ports)

    @work(exclusive=True, exit_on_error=False)
    async def probe_services(self, ports: tuple[int, ...]) -> None:
        try:
            probes = await self.tunnitup.probe_function(ports)
        except Exception as exc:
            self.query_one("#ports-error", Static).update(f"Could not probe services: {exc}")
            self.query_one("#probe", Button).disabled = False
            self.query_one("#probe", Button).label = "Probe ports  →"
            return
        await self.app.push_screen(PathsScreen(probes))


class PathsScreen(TunnitupScreen):
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, probes: tuple[ServiceProbe, ...]) -> None:
        super().__init__()
        self.probes = probes

    def compose(self) -> ComposeResult:
        yield Static("TUNNITUP   one domain, many local services", classes="topbar")
        with VerticalScroll(id="setup-shell"):
            yield Label("02 / PATHS", classes="step-label")
            yield Static("Edit the suggested public paths", classes="screen-title")
            yield Static(
                "Suggestions come from ordinary HTTP responses. You have the final say.",
                classes="screen-copy",
            )
            for probe in self.probes:
                state = probe.detail if probe.reachable else f"{probe.detail} · can still configure"
                with Horizontal(classes="service-row"):
                    yield Static(f"localhost:{probe.port}\n[dim]{state}[/dim]", classes="service")
                    yield Input(
                        value=probe.suggested_path,
                        id=f"path-{probe.port}",
                        classes="path-input",
                    )
            yield Static("", id="paths-error", classes="error")
            with Horizontal(classes="actions"):
                yield Button("←  Back", id="back")
                yield Button("Preview routes  →", id="preview", variant="primary")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "preview":
            self.action_preview()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_preview(self) -> None:
        try:
            routes = RouteTable(
                [
                    Route.parse(
                        f"{normalize_path(self.query_one(f'#path-{probe.port}', Input).value)}="
                        f"{probe.port}"
                    )
                    for probe in self.probes
                ]
            )
        except RouteConfigurationError as exc:
            self.query_one("#paths-error", Static).update(str(exc))
            return
        self.app.push_screen(PreviewScreen(routes))


class PreviewScreen(TunnitupScreen):
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, routes: RouteTable) -> None:
        super().__init__()
        self.routes = routes

    def compose(self) -> ComposeResult:
        yield Static("TUNNITUP   one domain, many local services", classes="topbar")
        with Vertical(id="setup-shell"):
            yield Label("03 / PREVIEW", classes="step-label")
            yield Static("Review what will become public", classes="screen-title")
            yield Static(
                "The tunnel is still stopped. Launching opens the command center.",
                classes="screen-copy",
            )
            with Vertical(id="preview-list"):
                for route in sorted(self.routes.routes, key=lambda item: item.path):
                    yield Static(
                        f"[bold #4a9be8]PUBLIC DOMAIN{route.path}[/]\n  →  {route.upstream}",
                        classes="preview-row",
                    )
            with Horizontal(classes="actions"):
                yield Button("←  Back", id="back")
                yield Button("Launch Tunnitup  →", id="launch", variant="primary")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "launch":
            self.launch()

    def action_back(self) -> None:
        self.app.pop_screen()

    @work(exclusive=True)
    async def launch(self) -> None:
        self.tunnitup.runtime = TuiRuntime(routes=self.routes)
        await self.app.push_screen(CommandCenterScreen())
        self.tunnitup.start_stack()


class RouteEditorScreen(ModalScreen[Route | None]):
    """Small route editor shared by the add and edit commands."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, route: Route | None = None) -> None:
        super().__init__()
        self.route = route

    def compose(self) -> ComposeResult:
        with Vertical(id="route-editor"):
            yield Static("EDIT ROUTE" if self.route else "ADD ROUTE", classes="modal-title")
            yield Label("Public path", classes="field-label")
            yield Input(value=self.route.path if self.route else "/api", id="route-path")
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


class CommandCenterScreen(TunnitupScreen):
    BINDINGS = [
        Binding("space", "toggle", show=False),
        Binding("enter", "inspect_route", show=False),
        Binding("a", "add_route", show=False),
        Binding("e", "edit_route", show=False),
        Binding("c", "copy_url", show=False),
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
                    yield Button("a  add", id="add-route")
                yield DataTable(
                    id="routes-table", cursor_type="row", show_header=False, cell_padding=0
                )
            with Vertical(id="requests-panel", classes="panel"):
                with Horizontal(classes="pane-heading"):
                    yield Static("RECENT TRAFFIC", id="traffic-heading")
                    yield Static("0 REQ/MIN", id="request-rate")
                yield DataTable(
                    id="requests-table",
                    cursor_type="row",
                    show_cursor=False,
                    header_height=2,
                )
        yield Static(
            "[bold #9fc8ef]↑↓[/] route    [bold #9fc8ef]enter[/] inspect    "
            "[bold #9fc8ef]a[/] add    [bold #9fc8ef]e[/] edit    "
            "[bold #9fc8ef]c[/] copy URL    [bold #9fc8ef]?[/] help",
            id="keybar",
        )

    def on_mount(self) -> None:
        routes = self.query_one("#routes-table", DataTable)
        routes.add_column("Path", width=12)
        routes.add_column("Upstream", width=30)
        routes.add_column("Health", width=2)
        requests = self.query_one("#requests-table", DataTable)
        requests.add_column("Time", width=9)
        requests.add_column("Method", width=8)
        requests.add_column("Path", width=32)
        requests.add_column("Code", width=8)
        requests.add_column("Latency", width=10)
        self.refresh_dashboard()
        self.set_interval(1, self.refresh_dashboard)
        self.observe()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle":
            self.action_toggle()
        elif event.button.id == "add-route":
            self.action_add_route()

    def action_toggle(self) -> None:
        if self.tunnitup.runtime_state in {"starting", "online"}:
            self.tunnitup.stop_stack()
        else:
            self.tunnitup.start_stack()

    def action_refresh(self) -> None:
        self.refresh_dashboard()

    def action_error_details(self) -> None:
        if self.tunnitup.runtime_error:
            self.notify(
                self.tunnitup.runtime_error,
                title="Tunnel error",
                severity="error",
                timeout=10,
            )

    def action_add_route(self) -> None:
        if not self._routes_are_editable():
            return
        self.app.push_screen(RouteEditorScreen(), self._add_route)

    def action_edit_route(self) -> None:
        if not self._routes_are_editable():
            return
        selected = self._selected_route()
        if selected is not None:
            self.app.push_screen(RouteEditorScreen(selected), self._replace_route)

    def action_inspect_route(self) -> None:
        selected = self._selected_route()
        if selected is None:
            return
        public = self.tunnitup.tunnel.public_url if self.tunnitup.tunnel else "PUBLIC DOMAIN"
        self.notify(
            f"{public.rstrip('/')}{selected.path}  →  {self._display_upstream(selected)}",
            title=f"Route {selected.path}",
            timeout=6,
        )

    def action_copy_url(self) -> None:
        if self.tunnitup.tunnel is None:
            self.notify("Start Tunnitup before copying the public URL.", severity="warning")
            return
        self.app.copy_to_clipboard(self.tunnitup.tunnel.public_url)
        self.notify("Public URL copied.")

    def action_help(self) -> None:
        self.notify(
            "↑↓ select · Enter inspect · A add · E edit · C copy URL · Space start/stop · Q quit",
            title="Keyboard controls",
            timeout=8,
        )

    def _routes_are_editable(self) -> bool:
        if self.tunnitup.runtime_state in {"starting", "online", "stopping"}:
            self.notify("Stop Tunnitup before changing routes.", severity="warning")
            return False
        return True

    def _selected_route(self) -> Route | None:
        runtime = self.tunnitup.runtime
        table = self.query_one("#routes-table", DataTable)
        if runtime is None or table.row_count == 0:
            return None
        path = str(table.get_row_at(table.cursor_row)[0])
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
            self.notify(str(exc), severity="error")
            return
        self.refresh_dashboard()

    @work(exclusive=True, exit_on_error=False)
    async def observe(self) -> None:
        async with self.tunnitup.observations.subscribe() as queue:
            while True:
                observation = await queue.get()
                if isinstance(observation, RequestEvent | RouteHealth | ActivityEvent):
                    self.refresh_dashboard()

    def refresh_dashboard(self) -> None:
        runtime = self.tunnitup.runtime
        if runtime is None:
            return
        public = self.tunnitup.tunnel.public_url if self.tunnitup.tunnel else "not connected"
        state = self.tunnitup.runtime_state.upper()
        state_color = "#5ac8fa" if state == "ONLINE" else "#4a9be8"
        if state == "ERROR":
            state_color = "#ef8d84"
        self.query_one("#runtime-state", Static).update(
            f"[bold {state_color}]■[/] [bold]{state}[/]"
        )
        self.query_one("#public-url", Static).update(public)
        self.query_one("#runtime-meta", Static).update(
            f"provider [bold]{runtime.tunnel.provider}[/]    uptime [bold]{self._uptime()}[/]"
        )
        button = self.query_one("#toggle", Button)
        button.label = (
            "■  stop"
            if self.tunnitup.runtime_state in {"starting", "online"}
            else "▶  start"
        )
        button.variant = (
            "error" if self.tunnitup.runtime_state in {"starting", "online"} else "primary"
        )
        error_banner = self.query_one("#runtime-error", Static)
        error_banner.update(self._friendly_error(self.tunnitup.runtime_error))
        error_banner.display = self.tunnitup.runtime_error is not None

        health = {item.route_path: item for item in self.tunnitup.observations.health}
        route_table = self.query_one("#routes-table", DataTable)
        route_table.clear()
        for route in sorted(runtime.routes.routes, key=lambda item: item.path):
            result = health.get(route.path)
            if result is None:
                state_text = Text("●", style="#5d7390")
            elif result.healthy:
                state_text = Text("●", style="#5ac8fa")
            else:
                state_text = Text("●", style="#ef8d84")
            route_table.add_row(
                route.path,
                self._display_upstream(route),
                state_text,
                height=3,
            )
        healthy_count = sum(item.healthy for item in health.values())
        self.query_one("#route-heading", Static).update(
            f"ROUTES  {healthy_count}/{len(runtime.routes.routes)} HEALTHY"
        )

        request_table = self.query_one("#requests-table", DataTable)
        request_table.clear()
        for event in reversed(self.tunnitup.observations.requests[-100:]):
            request_table.add_row(
                event.timestamp.astimezone().strftime("%H:%M:%S"),
                event.method,
                event.path,
                str(event.status),
                f"{event.duration_ms:.0f}ms",
                height=2,
            )
        cutoff = datetime.now(UTC) - timedelta(minutes=1)
        rate = sum(event.timestamp >= cutoff for event in self.tunnitup.observations.requests)
        self.query_one("#request-rate", Static).update(f"{rate} REQ/MIN")

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
        host = (
            "localhost"
            if route.upstream.host in {"127.0.0.1", "::1"}
            else route.upstream.host
        )
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

    CommandCenterScreen {
        border: solid #34465c;
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
        height: 3;
        padding: 0 1;
        background: #101721;
        border-bottom: solid #34465c;
        align-vertical: middle;
    }

    #runtime-state {
        width: 12;
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
        border-right: solid #34465c;
    }

    #routes-panel { width: 36%; }
    #requests-panel { width: 64%; }

    .pane-heading {
        height: 3;
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

    #add-route {
        width: 10;
        min-width: 10;
        height: 3;
        margin: 0;
        background: #171f2b;
        border: solid #5d7390;
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
        height: 3;
        padding: 0 1;
        background: #1d2b3b;
        color: #9fc0e5;
        border-top: solid #34465c;
        content-align: left middle;
    }

    RouteEditorScreen {
        align: center middle;
        background: #000000 50%;
    }

    #route-editor {
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
    """

    def __init__(
        self,
        runtime: TuiRuntime | None = None,
        *,
        probe_function: ProbeFunction = probe_ports,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.probe_function = probe_function
        self.observations = ObservationStore()
        self.runtime_state = "stopped"
        self.runtime_error: str | None = None
        self.tunnel: Tunnel | None = None
        self.online_since: float | None = None
        self.runtime_worker: Worker[None] | None = None

    def on_mount(self) -> None:
        if self.runtime is None:
            self.push_screen(PortsScreen())
        else:
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
        except (OSError, ProviderError) as exc:
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
