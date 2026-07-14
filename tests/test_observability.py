from datetime import UTC, datetime

from aiohttp import web
from aiohttp.test_utils import TestServer

from tunnitup.observability import (
    ActivityEvent,
    HealthMonitor,
    ObservationStore,
    RequestEvent,
    RouteHealth,
    display_upstream,
)
from tunnitup.routing import Route, RouteTable, normalize_upstream


def request_event(path: str, status: int = 200) -> RequestEvent:
    return RequestEvent(
        timestamp=datetime.now(UTC),
        method="GET",
        path=path,
        route_path="/",
        upstream="http://127.0.0.1:3000",
        status=status,
        duration_ms=1.5,
    )


def test_observation_store_bounds_request_history() -> None:
    observations = ObservationStore(history_size=2)

    observations.record(request_event("/one"))
    observations.record(request_event("/two"))
    observations.record(request_event("/three"))

    assert [event.path for event in observations.requests] == ["/two", "/three"]


async def test_observation_store_publishes_live_updates_without_blocking() -> None:
    observations = ObservationStore(subscriber_queue_size=1)

    async with observations.subscribe() as queue:
        observations.record(request_event("/old"))
        observations.record(request_event("/new"))

        event = queue.get_nowait()

    assert isinstance(event, RequestEvent)
    assert event.path == "/new"


async def test_observation_store_tracks_active_requests() -> None:
    observations = ObservationStore()

    async with observations.subscribe() as queue:
        observations.request_started()
        observations.request_finished()
        started = queue.get_nowait()
        finished = queue.get_nowait()

    assert isinstance(started, ActivityEvent)
    assert started.active_requests == 1
    assert isinstance(finished, ActivityEvent)
    assert finished.active_requests == 0
    assert observations.active_requests == 0


def test_display_upstream_redacts_credentials() -> None:
    assert display_upstream(normalize_upstream("http://alice:secret@localhost:8000")) == (
        "http://localhost:8000"
    )


def test_observation_store_keeps_latest_health_per_route() -> None:
    observations = ObservationStore()
    first = RouteHealth(
        timestamp=datetime.now(UTC),
        route_path="/api",
        upstream="http://127.0.0.1:8000",
        healthy=False,
        status=None,
        latency_ms=2,
        error="unavailable",
    )
    latest = RouteHealth(
        timestamp=datetime.now(UTC),
        route_path="/api",
        upstream="http://127.0.0.1:8000",
        healthy=True,
        status=200,
        latency_ms=1,
    )

    observations.record(first)
    observations.record(latest)

    assert observations.health == (latest,)


async def test_health_monitor_reports_reachable_and_unreachable_routes() -> None:
    async def healthy(_: web.Request) -> web.Response:
        return web.Response(status=204)

    app = web.Application()
    app.router.add_route("*", "/", healthy)
    async with TestServer(app) as upstream:
        routes = RouteTable(
            [
                Route.parse(f"/={upstream.make_url('')}"),
                Route.parse("/missing=http://127.0.0.1:1"),
            ]
        )
        observations = ObservationStore()
        results = await HealthMonitor(routes, observations).check_once()

    by_path = {result.route_path: result for result in results}
    assert by_path["/"].healthy is True
    assert by_path["/"].status == 204
    assert by_path["/missing"].healthy is False
    assert by_path["/missing"].error
    assert observations.health == tuple(by_path[path] for path in sorted(by_path))
