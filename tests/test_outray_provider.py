import asyncio
from typing import Any

import pytest

from tunnitup.providers import create_provider
from tunnitup.providers.base import ProviderError
from tunnitup.providers.outray import OutrayProvider


class FakeProcess:
    def __init__(self, output: bytes = b"", *, returncode: int | None = None) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, None]:
        output = await self.stdout.read()
        if self.returncode is None:
            self.returncode = 0
        return output, None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def test_missing_outray_has_an_install_hint(monkeypatch: Any) -> None:
    monkeypatch.setattr("tunnitup.providers.outray.shutil.which", lambda _name: None)

    with pytest.raises(ProviderError, match="npm install -g outray"):
        OutrayProvider()._find_executable()


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        (None, []),
        (
            "https://my-app.tunnel.outray.app",
            ["--subdomain", "my-app"],
        ),
        (
            "https://my-app.outray.app",
            ["--subdomain", "my-app"],
        ),
        (
            "https://preview.example.com",
            ["--domain", "preview.example.com"],
        ),
    ],
)
def test_public_url_becomes_the_correct_cli_flag(
    public_url: str | None,
    expected: list[str],
) -> None:
    assert OutrayProvider._public_url_args(public_url) == expected


def test_public_url_rejects_paths() -> None:
    with pytest.raises(ProviderError, match="cannot contain a path"):
        OutrayProvider._public_url_args("https://my-app.tunnel.outray.app/path")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "Tunnel ready: https://random-name.tunnel.outray.app",
            "https://random-name.tunnel.outray.app",
        ),
        (
            "✨ Tunnel: https://my-app.outray.app",
            "https://my-app.outray.app",
        ),
        ("GET https://example.com/api 200", None),
    ],
)
def test_public_url_is_discovered_only_from_tunnel_status_lines(
    line: str,
    expected: str | None,
) -> None:
    assert OutrayProvider._url_from_output(line) == expected


async def test_start_uses_proxy_port_subdomain_and_disables_cli_request_logs(
    monkeypatch: Any,
) -> None:
    commands: list[tuple[str, ...]] = []
    process = FakeProcess()
    provider = OutrayProvider(executable="outray")

    async def fake_create(*command: str, **_kwargs: Any) -> FakeProcess:
        commands.append(command)
        return process

    async def fake_preflight(_command_prefix: tuple[str, ...]) -> None:
        return None

    async def fake_discover(
        _requested_url: str | None,
        _startup_timeout: float,
    ) -> str:
        return "https://stable.tunnel.outray.app"

    monkeypatch.setattr("tunnitup.providers.outray.asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr(provider, "_run_preflight", fake_preflight)
    monkeypatch.setattr(provider, "_discover_public_url", fake_discover)

    tunnel = await provider.start(
        "http://127.0.0.1:8080",
        public_url="https://stable.tunnel.outray.app",
    )
    await provider.stop()

    assert tunnel.public_url == "https://stable.tunnel.outray.app"
    assert commands == [
        (
            "outray",
            "8080",
            "--subdomain",
            "stable",
            "--no-logs",
        )
    ]


async def test_output_capture_discovers_url_and_redacts_api_keys() -> None:
    provider = OutrayProvider(executable="outray")
    process = FakeProcess(
        b"using outray_sk_super-secret\nTunnel ready: https://found.tunnel.outray.app\n"
    )
    provider._process = process

    await provider._capture_output(process.stdout)

    assert provider._public_url == "https://found.tunnel.outray.app"
    assert "outray_sk_super-secret" not in "\n".join(provider._recent_output)


async def test_failed_preflight_suggests_login(monkeypatch: Any) -> None:
    process = FakeProcess(b"Unauthorized", returncode=1)

    async def fake_create(*_command: str, **_kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr("tunnitup.providers.outray.asyncio.create_subprocess_exec", fake_create)

    with pytest.raises(ProviderError, match="outray login"):
        await OutrayProvider(executable="outray")._run_preflight(("outray",))


def test_local_proxy_url_requires_an_explicit_port() -> None:
    with pytest.raises(ProviderError, match="explicit port"):
        OutrayProvider._local_port("http://localhost")


def test_provider_factory_creates_outray() -> None:
    assert isinstance(create_provider("outray"), OutrayProvider)


def test_windows_npm_shim_is_resolved_to_the_node_entrypoint(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    shim = tmp_path / "outray.CMD"
    script = tmp_path / "node_modules" / "outray" / "dist" / "index.js"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr("tunnitup.providers.outray.shutil.which", lambda name: "node.exe")

    assert OutrayProvider._command_prefix(str(shim)) == (
        "node.exe",
        str(script),
    )


def test_ansi_colored_tunnel_output_is_parsed() -> None:
    line = "\x1b[35m✨ Tunnel ready: https://color.tunnel.outray.app\x1b[39m"

    assert OutrayProvider._url_from_output(OutrayProvider._redact(line)) == (
        "https://color.tunnel.outray.app"
    )


def test_reserved_subdomain_matches_both_outray_hostname_formats() -> None:
    assert OutrayProvider._matches_requested_url(
        "https://my-app.outray.app",
        "https://my-app.tunnel.outray.app",
    )
    assert not OutrayProvider._matches_requested_url(
        "https://other.outray.app",
        "https://my-app.tunnel.outray.app",
    )
