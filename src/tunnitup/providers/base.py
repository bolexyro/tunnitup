from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderError(RuntimeError):
    """Raised when a tunnel provider cannot start or remain healthy."""


@dataclass(frozen=True, slots=True)
class Tunnel:
    provider: str
    public_url: str
    local_url: str


class TunnelProvider(Protocol):
    name: str

    async def start(
        self,
        local_url: str,
        *,
        public_url: str | None = None,
        startup_timeout: float = 15.0,
    ) -> Tunnel: ...

    async def wait(self) -> None: ...

    async def stop(self) -> None: ...
