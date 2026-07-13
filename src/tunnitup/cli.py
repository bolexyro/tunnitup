from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from aiohttp import web

from tunnitup.config import (
    DEFAULT_CONFIG_PATH,
    ConfigurationError,
    TunnitupConfig,
    load_config,
    render_starter_config,
)
from tunnitup.proxy import ProxySettings, create_proxy_app
from tunnitup.routing import (
    Route,
    RouteConfigurationError,
    RouteTable,
    normalize_path,
    normalize_upstream,
)

app = typer.Typer(
    name="tunnitup",
    help="Put many local services behind one tunnel-ready proxy.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Run and inspect Tunnitup's local development infrastructure."""


def build_route_table(
    default_upstream: str | None,
    route_specs: list[str],
    stripped_paths: list[str],
) -> RouteTable:
    specs = list(route_specs)
    explicit_paths = {normalize_path(spec.partition("=")[0]) for spec in specs if "=" in spec}
    if default_upstream:
        if "/" in explicit_paths:
            raise RouteConfigurationError(
                "the default upstream and an explicit '/' route cannot be used together"
            )
        specs.append(f"/={default_upstream}")
    if not specs:
        raise RouteConfigurationError(
            "provide a default port, such as 'tunnitup proxy 3000', or at least one --route"
        )

    normalized_stripped = {normalize_path(path) for path in stripped_paths}
    routes: list[Route] = []
    for spec in specs:
        path = normalize_path(spec.partition("=")[0])
        routes.append(Route.parse(spec, strip_prefix=path in normalized_stripped))

    unknown = normalized_stripped - {route.path for route in routes}
    if unknown:
        paths = ", ".join(sorted(unknown))
        raise RouteConfigurationError(f"--strip-prefix references undefined route(s): {paths}")
    return RouteTable(routes)


def _fail(message: str) -> None:
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(2)


def _load_config_or_exit(path: Path) -> TunnitupConfig:
    try:
        return load_config(path)
    except ConfigurationError as exc:
        _fail(str(exc))


def _print_configuration(config: TunnitupConfig) -> None:
    typer.echo(f"  Config     {config.source}")
    typer.echo(f"  Listening  http://{config.host}:{config.port}")
    typer.echo("  Routes")
    for route in sorted(config.routes.routes, key=lambda item: item.path):
        suffix = " (strip prefix)" if route.strip_prefix else ""
        typer.echo(f"    {route.path:<12} -> {route.upstream}{suffix}")


@app.command("init")
def init_config(
    frontend: Annotated[
        str,
        typer.Argument(help="Frontend port or URL to place at '/'."),
    ] = "3000",
    api_upstream: Annotated[
        str | None,
        typer.Option("--api", help="Optional API port or URL to place at '/api'."),
    ] = None,
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file to create."),
    ] = DEFAULT_CONFIG_PATH,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing configuration file."),
    ] = False,
) -> None:
    """Create a small, ready-to-edit tunnitup.toml file."""
    try:
        normalize_upstream(frontend)
        if api_upstream is not None:
            normalize_upstream(api_upstream)
    except RouteConfigurationError as exc:
        _fail(str(exc))

    mode = "w" if force else "x"
    try:
        with config.open(mode, encoding="utf-8", newline="\n") as file:
            file.write(render_starter_config(frontend, api_upstream))
    except FileExistsError:
        _fail(f"{config} already exists; use --force to replace it")
    except OSError as exc:
        _fail(f"could not write {config}: {exc}")

    loaded = _load_config_or_exit(config)
    typer.secho(f"Created {config}", fg=typer.colors.GREEN, bold=True)
    _print_configuration(loaded)
    typer.echo("\nNext: run 'tunnitup proxy'.")


@app.command("validate")
def validate_config(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file to validate."),
    ] = DEFAULT_CONFIG_PATH,
) -> None:
    """Validate configuration and show the effective routes."""
    loaded = _load_config_or_exit(config)
    typer.secho("Configuration is valid", fg=typer.colors.GREEN, bold=True)
    _print_configuration(loaded)


@app.command()
def proxy(
    default_upstream: Annotated[
        str | None,
        typer.Argument(
            help="Frontend port or upstream URL to expose at '/'. Example: 3000.",
            show_default=False,
        ),
    ] = None,
    route: Annotated[
        list[str] | None,
        typer.Option(
            "--route",
            "-r",
            help="Additional PATH=UPSTREAM mapping. Repeat for multiple services.",
            metavar="PATH=UPSTREAM",
        ),
    ] = None,
    strip_prefix: Annotated[
        list[str] | None,
        typer.Option(
            "--strip-prefix",
            help="Remove a route path before forwarding. Repeat as needed.",
            metavar="PATH",
        ),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Configuration file to use."),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option(help="Override the configured listening interface."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", "-p", min=1, max=65535, help="Override the proxy port."),
    ] = None,
    connect_timeout: Annotated[
        float | None,
        typer.Option(min=0.1, help="Override the upstream connection timeout."),
    ] = None,
    response_timeout: Annotated[
        float | None,
        typer.Option(min=0.1, help="Override the upstream response timeout."),
    ] = None,
) -> None:
    """Start the local path-based reverse proxy."""
    has_cli_routes = default_upstream is not None or bool(route)
    if config is not None and (has_cli_routes or strip_prefix):
        _fail("--config cannot be combined with positional or --route mappings")

    loaded: TunnitupConfig | None = None
    if config is not None:
        loaded = _load_config_or_exit(config)
    elif not has_cli_routes and not strip_prefix:
        loaded = _load_config_or_exit(DEFAULT_CONFIG_PATH)

    if loaded is not None:
        route_table = loaded.routes
    else:
        try:
            route_table = build_route_table(default_upstream, route or [], strip_prefix or [])
        except RouteConfigurationError as exc:
            _fail(str(exc))

    defaults = loaded.settings if loaded else ProxySettings()
    effective_host = host if host is not None else loaded.host if loaded else "127.0.0.1"
    effective_port = port if port is not None else loaded.port if loaded else 8080
    settings = ProxySettings(
        connect_timeout=(
            connect_timeout if connect_timeout is not None else defaults.connect_timeout
        ),
        response_timeout=(
            response_timeout if response_timeout is not None else defaults.response_timeout
        ),
        shutdown_timeout=defaults.shutdown_timeout,
    )

    typer.secho("Tunnitup proxy is ready", fg=typer.colors.GREEN, bold=True)
    if loaded:
        typer.echo(f"  Config     {loaded.source}")
    typer.echo(f"  Listening  http://{effective_host}:{effective_port}")
    typer.echo("  Routes")
    for configured_route in sorted(route_table.routes, key=lambda item: item.path):
        suffix = " (strip prefix)" if configured_route.strip_prefix else ""
        typer.echo(f"    {configured_route.path:<12} -> {configured_route.upstream}{suffix}")
    typer.echo("\nPress Ctrl+C to stop.\n")

    try:
        web.run_app(
            create_proxy_app(route_table, settings),
            host=effective_host,
            port=effective_port,
            print=None,
            shutdown_timeout=settings.shutdown_timeout,
            handler_cancellation=True,
        )
    except OSError as exc:
        typer.secho(f"Could not start the proxy: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
