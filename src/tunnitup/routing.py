from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from yarl import URL


class RouteConfigurationError(ValueError):
    """Raised when a route cannot be parsed or is ambiguous."""


def normalize_path(path: str) -> str:
    path = path.strip()
    if not path:
        raise RouteConfigurationError("route paths cannot be empty")
    if not path.startswith("/"):
        raise RouteConfigurationError(f"route path {path!r} must start with '/'")
    if "?" in path or "#" in path:
        raise RouteConfigurationError(f"route path {path!r} cannot contain a query or fragment")
    return path.rstrip("/") or "/"


def normalize_upstream(value: str) -> URL:
    value = value.strip()
    if not value:
        raise RouteConfigurationError("route upstreams cannot be empty")

    if value.isdigit():
        value = f"http://127.0.0.1:{value}"
    elif "://" not in value:
        value = f"http://{value}"

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise RouteConfigurationError("upstream scheme must be http or https")
    if not parsed.hostname:
        raise RouteConfigurationError(f"upstream {value!r} is missing a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RouteConfigurationError(f"upstream {value!r} has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise RouteConfigurationError("upstream port must be between 1 and 65535")
    if parsed.query or parsed.fragment:
        raise RouteConfigurationError("upstream URLs cannot contain a query or fragment")
    return URL(value)


@dataclass(frozen=True, slots=True)
class Route:
    path: str
    upstream: URL
    strip_prefix: bool = False

    @classmethod
    def parse(cls, specification: str, *, strip_prefix: bool = False) -> Route:
        if "=" not in specification:
            raise RouteConfigurationError(
                f"route {specification!r} must use PATH=UPSTREAM, for example /api=8000"
            )
        path, upstream = specification.split("=", 1)
        return cls(
            path=normalize_path(path),
            upstream=normalize_upstream(upstream),
            strip_prefix=strip_prefix,
        )

    def matches(self, request_path: str) -> bool:
        if self.path == "/":
            return request_path.startswith("/")
        return request_path == self.path or request_path.startswith(f"{self.path}/")

    def forwarded_path(self, request_path: str) -> str:
        if not self.strip_prefix or self.path == "/" or not self.matches(request_path):
            return request_path
        return request_path[len(self.path) :] or "/"

    def target_url(self, request_path: str, query_string: str = "") -> URL:
        forwarded_path = self.forwarded_path(request_path)
        base_path = self.upstream.path.rstrip("/")
        target = self.upstream.with_path(f"{base_path}{forwarded_path}")
        return target.with_query(query_string) if query_string else target


class RouteTable:
    """Immutable route table using boundary-aware longest-prefix matching."""

    def __init__(self, routes: list[Route]) -> None:
        paths = [route.path for route in routes]
        if len(paths) != len(set(paths)):
            duplicate = next(path for path in paths if paths.count(path) > 1)
            raise RouteConfigurationError(f"route {duplicate!r} is defined more than once")
        self._routes = tuple(sorted(routes, key=lambda route: len(route.path), reverse=True))

    @property
    def routes(self) -> tuple[Route, ...]:
        return self._routes

    def match(self, request_path: str) -> Route | None:
        return next((route for route in self._routes if route.matches(request_path)), None)


def _is_local_listener_host(host: str) -> bool:
    normalized = host.strip("[]").casefold()
    if normalized == "localhost":
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _targets_listener(proxy_host: str, upstream_host: str) -> bool:
    proxy = proxy_host.strip("[]").casefold()
    upstream = upstream_host.strip("[]").casefold()
    if proxy == upstream:
        return True
    return _is_local_listener_host(proxy) and _is_local_listener_host(upstream)


def validate_proxy_routes(routes: RouteTable, host: str, port: int) -> None:
    """Reject upstreams that resolve back to Tunnitup's listening socket."""
    if not routes.routes:
        raise RouteConfigurationError("at least one route is required")
    for route in routes.routes:
        upstream_host = route.upstream.host
        if (
            upstream_host is not None
            and route.upstream.port == port
            and _targets_listener(host, upstream_host)
        ):
            display_host = "localhost" if _is_local_listener_host(upstream_host) else upstream_host
            raise RouteConfigurationError(
                f"route {route.path!r} points to Tunnitup's own proxy at "
                f"{display_host}:{port}; use a different upstream port or change proxy.port"
            )
