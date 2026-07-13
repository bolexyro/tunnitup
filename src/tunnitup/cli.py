from __future__ import annotations

from typing import Annotated

import typer
from aiohttp import web

from tunnitup.proxy import ProxySettings, create_proxy_app
from tunnitup.routing import Route, RouteConfigurationError, RouteTable, normalize_path

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
    host: Annotated[
        str,
        typer.Option(help="Local interface for the proxy to listen on."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", min=1, max=65535, help="Local proxy port."),
    ] = 8080,
    connect_timeout: Annotated[
        float,
        typer.Option(min=0.1, help="Seconds allowed to connect to an upstream."),
    ] = 10.0,
    response_timeout: Annotated[
        float,
        typer.Option(min=0.1, help="Seconds allowed between upstream response chunks."),
    ] = 60.0,
) -> None:
    """Start the local path-based reverse proxy."""
    try:
        route_table = build_route_table(default_upstream, route or [], strip_prefix or [])
    except RouteConfigurationError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    typer.secho("Tunnitup proxy is ready", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Listening  http://{host}:{port}")
    typer.echo("  Routes")
    for configured_route in sorted(route_table.routes, key=lambda item: item.path):
        suffix = " (strip prefix)" if configured_route.strip_prefix else ""
        typer.echo(f"    {configured_route.path:<12} -> {configured_route.upstream}{suffix}")
    typer.echo("\nPress Ctrl+C to stop.\n")

    settings = ProxySettings(
        connect_timeout=connect_timeout,
        response_timeout=response_timeout,
    )
    try:
        web.run_app(
            create_proxy_app(route_table, settings),
            host=host,
            port=port,
            print=None,
            shutdown_timeout=settings.shutdown_timeout,
            handler_cancellation=True,
        )
    except OSError as exc:
        typer.secho(f"Could not start the proxy: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
