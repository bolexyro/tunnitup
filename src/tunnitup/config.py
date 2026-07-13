from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tunnitup.proxy import ProxySettings
from tunnitup.routing import Route, RouteConfigurationError, RouteTable

DEFAULT_CONFIG_PATH = Path("tunnitup.toml")


class ConfigurationError(ValueError):
    """Raised when a Tunnitup configuration file is missing or invalid."""


@dataclass(frozen=True, slots=True)
class TunnitupConfig:
    source: Path
    host: str
    port: int
    settings: ProxySettings
    routes: RouteTable


def _reject_unknown_keys(values: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigurationError(f"{location} contains unknown field(s): {names}")


def _positive_number(value: Any, location: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigurationError(f"{location} must be a number greater than zero")
    return float(value)


def _parse_proxy(raw: Any) -> tuple[str, int, ProxySettings]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError("[proxy] must be a table")
    _reject_unknown_keys(
        raw,
        {"host", "port", "connect_timeout", "response_timeout", "shutdown_timeout"},
        "[proxy]",
    )

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ConfigurationError("proxy.host must be a non-empty string")

    port = raw.get("port", 8080)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationError("proxy.port must be an integer between 1 and 65535")

    defaults = ProxySettings()
    settings = ProxySettings(
        connect_timeout=_positive_number(
            raw.get("connect_timeout"), "proxy.connect_timeout", defaults.connect_timeout
        ),
        response_timeout=_positive_number(
            raw.get("response_timeout"), "proxy.response_timeout", defaults.response_timeout
        ),
        shutdown_timeout=_positive_number(
            raw.get("shutdown_timeout"), "proxy.shutdown_timeout", defaults.shutdown_timeout
        ),
    )
    return host.strip(), port, settings


def _parse_routes(raw: Any) -> RouteTable:
    if not isinstance(raw, dict) or not raw:
        raise ConfigurationError("[routes] must be a non-empty table")

    routes: list[Route] = []
    for path, value in raw.items():
        strip_prefix = False
        upstream: Any = value
        if isinstance(value, dict):
            _reject_unknown_keys(value, {"upstream", "strip_prefix"}, f"routes.{path}")
            if "upstream" not in value:
                raise ConfigurationError(f"routes.{path}.upstream is required")
            upstream = value["upstream"]
            strip_prefix = value.get("strip_prefix", False)
            if not isinstance(strip_prefix, bool):
                raise ConfigurationError(f"routes.{path}.strip_prefix must be true or false")

        if isinstance(upstream, bool) or not isinstance(upstream, str | int):
            raise ConfigurationError(f"routes.{path}.upstream must be a port or URL")
        if isinstance(upstream, int) and not 1 <= upstream <= 65535:
            raise ConfigurationError(f"routes.{path}.upstream port must be between 1 and 65535")

        try:
            routes.append(Route.parse(f"{path}={upstream}", strip_prefix=strip_prefix))
        except RouteConfigurationError as exc:
            raise ConfigurationError(f"routes.{path}: {exc}") from exc

    try:
        return RouteTable(routes)
    except RouteConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> TunnitupConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"configuration file {path} was not found; run 'tunnitup init' to create one"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(f"could not read configuration file {path}: {exc}") from exc

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc

    _reject_unknown_keys(raw, {"proxy", "routes"}, "configuration")
    host, port, settings = _parse_proxy(raw.get("proxy"))
    routes = _parse_routes(raw.get("routes"))
    return TunnitupConfig(
        source=path,
        host=host,
        port=port,
        settings=settings,
        routes=routes,
    )


def render_starter_config(frontend: str = "3000", api: str | None = None) -> str:
    def toml_value(value: str) -> str:
        return value if value.isdigit() else json.dumps(value)

    lines = [
        "# Tunnitup routes one local proxy across your development services.",
        "[proxy]",
        'host = "127.0.0.1"',
        "port = 8080",
        "connect_timeout = 10",
        "response_timeout = 60",
        "",
        "[routes]",
        f'"/" = {toml_value(frontend)}',
    ]
    if api is not None:
        lines.append(f'"/api" = {{ upstream = {toml_value(api)}, strip_prefix = true }}')
    else:
        lines.append('# "/api" = { upstream = 8000, strip_prefix = true }')
    return "\n".join(lines) + "\n"
