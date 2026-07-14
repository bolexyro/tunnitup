from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace

import aiohttp
from aiohttp import ClientSession, ClientTimeout


class PortInputError(ValueError):
    """Raised when guided setup receives invalid localhost ports."""


@dataclass(frozen=True, slots=True)
class ServiceProbe:
    port: int
    reachable: bool
    kind: str
    detail: str
    suggested_path: str = ""


def parse_ports(value: str) -> tuple[int, ...]:
    raw_ports = [part.strip() for part in value.split(",")]
    if not raw_ports or any(not part for part in raw_ports):
        raise PortInputError("enter one or more ports separated by commas")

    ports: list[int] = []
    for raw in raw_ports:
        if not raw.isdigit():
            raise PortInputError(f"port {raw!r} must be a number")
        port = int(raw)
        if not 1 <= port <= 65535:
            raise PortInputError("ports must be between 1 and 65535")
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def assign_suggested_paths(probes: tuple[ServiceProbe, ...]) -> tuple[ServiceProbe, ...]:
    used: set[str] = set()
    assigned: list[ServiceProbe] = []
    for probe in probes:
        if probe.kind == "frontend" and "/" not in used:
            path = "/"
        elif probe.kind == "api" and "/api" not in used:
            path = "/api"
        elif probe.kind == "frontend":
            path = f"/app-{probe.port}"
        elif probe.kind == "api":
            path = f"/api-{probe.port}"
        else:
            path = f"/service-{probe.port}"
        used.add(path)
        assigned.append(replace(probe, suggested_path=path))
    return tuple(assigned)


async def probe_ports(
    ports: tuple[int, ...], request_timeout: float = 1.5
) -> tuple[ServiceProbe, ...]:
    client_timeout = ClientTimeout(total=request_timeout)
    async with ClientSession(
        timeout=client_timeout,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    ) as session:
        probes = await asyncio.gather(*(_probe_port(session, port) for port in ports))
    return assign_suggested_paths(tuple(probes))


async def _probe_port(session: ClientSession, port: int) -> ServiceProbe:
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with session.get(
            base_url,
            allow_redirects=False,
            headers={"Range": "bytes=0-4095"},
        ) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            body = await response.content.read(4096)
            server = response.headers.get("Server", "HTTP service")
            if "text/html" in content_type or b"<html" in body.lower():
                return ServiceProbe(port, True, "frontend", f"HTML · {server}")
            if "json" in content_type:
                detail = _json_detail(body) or f"JSON · {server}"
                return ServiceProbe(port, True, "api", detail)

        try:
            async with session.get(f"{base_url}/openapi.json") as response:
                if (
                    response.status < 500
                    and "json" in response.headers.get("Content-Type", "").lower()
                ):
                    return ServiceProbe(port, True, "api", "OpenAPI detected")
        except (TimeoutError, aiohttp.ClientError):
            pass
        return ServiceProbe(port, True, "service", server)
    except TimeoutError:
        return ServiceProbe(port, False, "service", "Timed out")
    except aiohttp.ClientError:
        return ServiceProbe(port, False, "service", "Not reachable yet")


def _json_detail(body: bytes) -> str | None:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(value, dict) and ("openapi" in value or "swagger" in value):
        return "OpenAPI detected"
    return None
