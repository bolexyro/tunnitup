import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tunnitup.providers.base import ProviderError
from tunnitup.providers.ngrok import NgrokProvider


class FakeProcess:
    def __init__(self, output: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, None]:
        self.returncode = 0
        return await self.stdout.read(), None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def test_missing_ngrok_has_an_install_hint(monkeypatch: Any) -> None:
    monkeypatch.setattr("tunnitup.providers.ngrok.shutil.which", lambda _name: None)

    with pytest.raises(ProviderError, match="ngrok.com/download"):
        NgrokProvider()._find_executable()


async def test_start_uses_requested_url_and_stops_cleanly(monkeypatch: Any) -> None:
    commands: list[tuple[str, ...]] = []
    process = FakeProcess()
    provider = NgrokProvider(executable="ngrok")

    async def fake_create(*command: str, **_kwargs: Any) -> FakeProcess:
        commands.append(command)
        return process

    async def fake_preflight(_executable: str) -> None:
        return None

    async def fake_discover(
        _local_url: str,
        _requested_url: str | None,
        _startup_timeout: float,
    ) -> str:
        return "https://stable.ngrok.app"

    monkeypatch.setattr("tunnitup.providers.ngrok.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr(provider, "_run_preflight", fake_preflight)
    monkeypatch.setattr(provider, "_discover_public_url", fake_discover)

    tunnel = await provider.start(
        "http://127.0.0.1:8080",
        public_url="https://stable.ngrok.app",
    )
    await provider.stop()

    assert tunnel.public_url == "https://stable.ngrok.app"
    assert commands == [
        (
            "ngrok",
            "http",
            "http://127.0.0.1:8080",
            "--url",
            "https://stable.ngrok.app",
        )
    ]
    assert process.returncode == 0


async def test_agent_api_discovers_only_the_matching_proxy_port() -> None:
    async def tunnels(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "tunnels": [
                    {
                        "public_url": "https://wrong.ngrok.app",
                        "config": {"addr": "localhost:9999"},
                    },
                    {
                        "public_url": "https://right.ngrok.app",
                        "config": {"addr": "http://127.0.0.1:8080"},
                    },
                ]
            }
        )

    app = web.Application()
    app.router.add_get("/api/tunnels", tunnels)
    async with TestServer(app) as server:
        provider = NgrokProvider(inspection_url=str(server.make_url("/api/tunnels")))
        provider._process = FakeProcess()  # noqa: SLF001 - controlled provider state for API test
        url = await provider._discover_public_url(  # noqa: SLF001
            "http://127.0.0.1:8080",
            None,
            1,
        )

    assert url == "https://right.ngrok.app"


def test_ngrok_output_redacts_auth_tokens() -> None:
    line = 'error authtoken=super-secret region="us"'

    assert "super-secret" not in NgrokProvider._redact(line)  # noqa: SLF001
