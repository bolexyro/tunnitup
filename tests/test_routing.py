import pytest

from tunnitup.cli import build_route_table
from tunnitup.routing import Route, RouteConfigurationError, RouteTable


def test_port_shorthand_uses_localhost() -> None:
    route = Route.parse("/api=8000")

    assert str(route.upstream) == "http://127.0.0.1:8000"


def test_longest_boundary_aware_prefix_wins() -> None:
    root = Route.parse("/=3000")
    api = Route.parse("/api=8000")
    admin = Route.parse("/api/admin=9000")
    table = RouteTable([root, api, admin])

    assert table.match("/api/admin/users") is admin
    assert table.match("/api/users") is api
    assert table.match("/apix") is root


def test_strip_prefix_rewrites_only_the_matching_prefix() -> None:
    route = Route.parse("/api=http://localhost:8000/v1", strip_prefix=True)

    assert route.forwarded_path("/api/users") == "/users"
    assert str(route.target_url("/api/users", "active=true")) == (
        "http://localhost:8000/v1/users?active=true"
    )


def test_duplicate_routes_are_rejected() -> None:
    with pytest.raises(RouteConfigurationError, match="defined more than once"):
        RouteTable([Route.parse("/api=8000"), Route.parse("/api/=9000")])


def test_cli_default_route_and_stripped_route_are_combined() -> None:
    table = build_route_table("3000", ["/api=8000"], ["/api"])

    assert table.match("/").upstream.port == 3000  # type: ignore[union-attr]
    assert table.match("/api/users").strip_prefix is True  # type: ignore[union-attr]


@pytest.mark.parametrize("spec", ["api=8000", "/api", "/api=ftp://localhost:21", "/api=0"])
def test_invalid_route_specs_explain_the_problem(spec: str) -> None:
    with pytest.raises(RouteConfigurationError):
        Route.parse(spec)
