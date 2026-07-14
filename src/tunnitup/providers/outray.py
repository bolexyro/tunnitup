from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from tunnitup.providers.base import ProviderError, Tunnel

OUTRAY_SUBDOMAIN_SUFFIXES = (".tunnel.outray.app", ".outray.app")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class OutrayProvider:
    name = "outray"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable
        self._process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task[None] | None = None
        self._recent_output: deque[str] = deque(maxlen=20)
        self._public_url: str | None = None

    def _find_executable(self) -> str:
        executable = self._executable or shutil.which("outray")
        if executable is None:
            raise ProviderError(
                "Outray was not found on PATH; install it with 'npm install -g outray'"
            )
        return str(Path(executable))

    @staticmethod
    def _command_prefix(executable: str) -> tuple[str, ...]:
        path = Path(executable)
        if path.suffix.casefold() in {".cmd", ".ps1"}:
            script = path.parent / "node_modules" / "outray" / "dist" / "index.js"
            bundled_node = path.parent / "node.exe"
            node = str(bundled_node) if bundled_node.exists() else shutil.which("node")
            if script.exists() and node is not None:
                return (str(node), str(script))
        return (executable,)

    async def _run_preflight(self, command_prefix: tuple[str, ...]) -> None:
        process = await asyncio.create_subprocess_exec(
            *command_prefix,
            "whoami",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError("Outray authentication check timed out") from exc

        if process.returncode != 0:
            detail = self._redact(output.decode(errors="replace").strip())
            raise ProviderError(
                "Outray is not authenticated; run 'outray login'"
                + (f": {detail}" if detail else "")
            )

    @staticmethod
    def _redact(value: str) -> str:
        clean = ANSI_ESCAPE.sub("", value)
        return re.sub(r"outray_sk_[A-Za-z0-9_-]+", "[redacted]", clean)

    @staticmethod
    def _url_from_output(line: str) -> str | None:
        match = re.search(
            r"(?i)(?:tunnel ready|tunnel)\s*:\s*(https://[^\s]+)",
            line,
        )
        if match is None:
            return None
        candidate = match.group(1).rstrip(".,;)")
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            return None
        return candidate.rstrip("/")

    async def _capture_output(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.readline():
            line = self._redact(chunk.decode(errors="replace").strip())
            if not line:
                continue
            self._recent_output.append(line)
            discovered = self._url_from_output(line)
            if discovered is not None:
                self._public_url = discovered

    def _exit_message(self, returncode: int | None) -> str:
        detail = "\n".join(self._recent_output)
        message = f"Outray exited unexpectedly with code {returncode}"
        if detail:
            message = f"{message}\n{detail}"
        lowered = detail.casefold()
        if "auth" in lowered or "login" in lowered or "unauthorized" in lowered:
            message += "\nAuthenticate with 'outray login'."
        return message

    @staticmethod
    def _local_port(local_url: str) -> int:
        parsed = urlsplit(local_url)
        if parsed.scheme not in {"http", "https"} or parsed.port is None:
            raise ProviderError("Outray requires a local HTTP URL with an explicit port")
        return parsed.port

    @staticmethod
    def _public_url_args(public_url: str | None) -> list[str]:
        if public_url is None:
            return []
        parsed = urlsplit(public_url)
        hostname = parsed.hostname
        if parsed.scheme != "https" or hostname is None or parsed.path not in {"", "/"}:
            raise ProviderError("Outray public URL must be HTTPS and cannot contain a path")
        subdomain = OutrayProvider._reserved_subdomain(hostname)
        if subdomain is not None:
            return ["--subdomain", subdomain]
        return ["--domain", hostname]

    @staticmethod
    def _reserved_subdomain(hostname: str) -> str | None:
        for suffix in OUTRAY_SUBDOMAIN_SUFFIXES:
            if hostname.endswith(suffix):
                subdomain = hostname[: -len(suffix)]
                if subdomain and "." not in subdomain:
                    return subdomain
        return None

    @classmethod
    def _matches_requested_url(cls, discovered: str, requested: str) -> bool:
        discovered_host = urlsplit(discovered).hostname
        requested_host = urlsplit(requested).hostname
        if discovered_host == requested_host:
            return True
        if discovered_host is None or requested_host is None:
            return False
        subdomain = cls._reserved_subdomain(requested_host)
        return subdomain is not None and cls._reserved_subdomain(discovered_host) == subdomain

    async def _discover_public_url(
        self,
        requested_url: str | None,
        startup_timeout: float,
    ) -> str:
        expected = requested_url.rstrip("/") if requested_url else None
        deadline = asyncio.get_running_loop().time() + startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            process = self._process
            if process is None:
                raise ProviderError("Outray process was not started")
            if process.returncode is not None:
                if self._output_task is not None:
                    await self._output_task
                raise ProviderError(self._exit_message(process.returncode))
            if self._public_url is not None:
                if expected is None or self._matches_requested_url(self._public_url, expected):
                    return self._public_url
            await asyncio.sleep(0.05)

        if expected and self._public_url:
            raise ProviderError(
                f"Outray published {self._public_url} instead of requested URL {expected}"
            )
        raise ProviderError(
            "Outray did not publish an HTTPS URL before the startup timeout; "
            "check that the CLI is authenticated with 'outray whoami'"
        )

    async def start(
        self,
        local_url: str,
        *,
        public_url: str | None = None,
        startup_timeout: float = 15.0,
    ) -> Tunnel:
        if self._process is not None:
            raise ProviderError("Outray is already running")
        executable = self._find_executable()
        command_prefix = self._command_prefix(executable)
        await self._run_preflight(command_prefix)

        command = [
            *command_prefix,
            str(self._local_port(local_url)),
            *self._public_url_args(public_url),
            "--no-logs",
        ]
        self._public_url = None
        self._recent_output.clear()
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert self._process.stdout is not None
        self._output_task = asyncio.create_task(self._capture_output(self._process.stdout))

        try:
            discovered_url = await self._discover_public_url(public_url, startup_timeout)
        except BaseException:
            await self.stop()
            raise
        return Tunnel(provider=self.name, public_url=discovered_url, local_url=local_url)

    async def wait(self) -> None:
        if self._process is None:
            raise ProviderError("Outray is not running")
        returncode = await self._process.wait()
        if self._output_task is not None:
            await self._output_task
        raise ProviderError(self._exit_message(returncode))

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._output_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._output_task
        self._process = None
        self._output_task = None
        self._public_url = None
