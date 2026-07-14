from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

import aiohttp
from aiohttp import ClientSession, ClientTimeout
from yarl import URL

from tunnitup.routing import Route, RouteTable


@dataclass(frozen=True, slots=True)
class RequestEvent:
    timestamp: datetime
    method: str
    path: str
    route_path: str | None
    upstream: str | None
    status: int
    duration_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RouteHealth:
    timestamp: datetime
    route_path: str
    upstream: str
    healthy: bool
    status: int | None
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    timestamp: datetime
    active_requests: int


Observation = RequestEvent | RouteHealth | ActivityEvent


def display_upstream(url: URL) -> str:
    """Render an upstream URL without credentials."""
    return str(url.with_user(None))


class ObservationStore:
    """Bounded request history and current route health with live subscriptions."""

    def __init__(self, history_size: int = 200, subscriber_queue_size: int = 256) -> None:
        if history_size <= 0:
            raise ValueError("history size must be greater than zero")
        if subscriber_queue_size <= 0:
            raise ValueError("subscriber queue size must be greater than zero")
        self._requests: deque[RequestEvent] = deque(maxlen=history_size)
        self._health: dict[str, RouteHealth] = {}
        self._subscribers: set[asyncio.Queue[Observation]] = set()
        self._subscriber_queue_size = subscriber_queue_size
        self._active_requests = 0

    @property
    def requests(self) -> tuple[RequestEvent, ...]:
        return tuple(self._requests)

    def clear_requests(self) -> None:
        """Clear captured request history without discarding route health."""
        self._requests.clear()

    @property
    def health(self) -> tuple[RouteHealth, ...]:
        return tuple(self._health[path] for path in sorted(self._health))

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def record(self, observation: Observation) -> None:
        if isinstance(observation, RequestEvent):
            self._requests.append(observation)
        elif isinstance(observation, RouteHealth):
            self._health[observation.route_path] = observation

        self._publish(observation)

    def request_started(self) -> None:
        self._active_requests += 1
        self._publish(ActivityEvent(datetime.now(UTC), self._active_requests))

    def request_finished(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        self._publish(ActivityEvent(datetime.now(UTC), self._active_requests))

    def _publish(self, observation: Observation) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(observation)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Observation]]:
        queue: asyncio.Queue[Observation] = asyncio.Queue(self._subscriber_queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


class HealthMonitor:
    """Periodically checks whether configured HTTP upstreams are reachable."""

    def __init__(
        self,
        routes: RouteTable,
        observations: ObservationStore,
        *,
        interval: float = 5.0,
        timeout: float = 2.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("health check interval must be greater than zero")
        if timeout <= 0:
            raise ValueError("health check timeout must be greater than zero")
        self._routes = routes
        self._observations = observations
        self._interval = interval
        self._timeout = timeout
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="tunnitup-health-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def check_once(self) -> tuple[RouteHealth, ...]:
        timeout = ClientTimeout(total=self._timeout)
        async with ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        ) as session:
            results = await asyncio.gather(
                *(self._check_route(session, route) for route in self._routes.routes)
            )
        for result in results:
            self._observations.record(result)
        return tuple(results)

    async def _run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(self._interval)

    async def _check_route(self, session: ClientSession, route: Route) -> RouteHealth:
        started = perf_counter()
        status: int | None = None
        error: str | None = None
        healthy = False
        try:
            async with session.head(route.upstream, allow_redirects=False) as response:
                status = response.status
                healthy = status < 500
        except TimeoutError:
            error = "timed out"
        except aiohttp.ClientError as exc:
            error = str(exc)
        return RouteHealth(
            timestamp=datetime.now(UTC),
            route_path=route.path,
            upstream=display_upstream(route.upstream),
            healthy=healthy,
            status=status,
            latency_ms=(perf_counter() - started) * 1000,
            error=error,
        )
