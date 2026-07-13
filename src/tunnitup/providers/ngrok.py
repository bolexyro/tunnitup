from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from tunnitup.providers.base import ProviderError, Tunnel

DEFAULT_INSPECTION_URL = "http://127.0.0.1:4040/api/tunnels"


class NgrokProvider:
    name = "ngrok"

    def __init__(
        self,
        executable: str | None = None,
        inspection_url: str = DEFAULT_INSPECTION_URL,
    ) -> None:
        self._executable = executable
        self._inspection_url = inspection_url
        self._process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task[None] | None = None
        self._recent_output: deque[str] = deque(maxlen=20)

    def _find_executable(self) -> str:
        executable = self._executable or shutil.which("ngrok")
        if executable is None:
            raise ProviderError(
                "ngrok was not found on PATH; install it from https://ngrok.com/download"
            )
        return str(Path(executable))

    async def _run_preflight(self, executable: str) -> None:
        process = await asyncio.create_subprocess_exec(
            executable,
            "config",
            "check",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError("ngrok configuration check timed out") from exc

        if process.returncode != 0:
            detail = output.decode(errors="replace").strip()
            raise ProviderError(
                "ngrok configuration is invalid; run 'ngrok config check'"
                + (f": {detail}" if detail else "")
            )

    @staticmethod
    def _redact(line: str) -> str:
        return re.sub(
            r"(?i)(authtoken[\s=:]+)([^\s,}]+)",
            r"\1[redacted]",
            line,
        )

    async def _capture_output(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.readline():
            line = self._redact(chunk.decode(errors="replace").strip())
            if line:
                self._recent_output.append(line)

    def _exit_message(self, returncode: int | None) -> str:
        detail = "\n".join(self._recent_output)
        message = f"ngrok exited unexpectedly with code {returncode}"
        if detail:
            message = f"{message}\n{detail}"
        if "auth" in detail.lower() or "authtoken" in detail.lower():
            message += "\nAuthenticate with 'ngrok config add-authtoken <token>'."
        return message

    @staticmethod
    def _same_upstream(candidate: object, expected_url: str) -> bool:
        if not isinstance(candidate, str):
            return False
        candidate_url = candidate if "://" in candidate else f"http://{candidate}"
        candidate_parts = urlsplit(candidate_url)
        expected_parts = urlsplit(expected_url)
        return candidate_parts.port == expected_parts.port

    async def _discover_public_url(
        self,
        local_url: str,
        requested_url: str | None,
        startup_timeout: float,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + startup_timeout
        timeout = aiohttp.ClientTimeout(total=1)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            while asyncio.get_running_loop().time() < deadline:
                if self._process is None:
                    raise ProviderError("ngrok process was not started")
                if self._process.returncode is not None:
                    if self._output_task is not None:
                        await self._output_task
                    raise ProviderError(self._exit_message(self._process.returncode))

                try:
                    async with session.get(self._inspection_url) as response:
                        if response.status == 200:
                            payload = await response.json()
                            for tunnel in payload.get("tunnels", []):
                                public_url = tunnel.get("public_url")
                                upstream = tunnel.get("config", {}).get("addr")
                                if not self._same_upstream(upstream, local_url):
                                    continue
                                if requested_url and public_url != requested_url:
                                    continue
                                if isinstance(public_url, str) and public_url.startswith(
                                    "https://"
                                ):
                                    return public_url
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(0.1)

        raise ProviderError(
            "ngrok did not publish an HTTPS URL before the startup timeout; "
            "check ngrok's local inspector at http://127.0.0.1:4040"
        )

    async def start(
        self,
        local_url: str,
        *,
        public_url: str | None = None,
        startup_timeout: float = 15.0,
    ) -> Tunnel:
        if self._process is not None:
            raise ProviderError("ngrok is already running")
        executable = self._find_executable()
        await self._run_preflight(executable)

        command = [executable, "http", local_url]
        if public_url:
            command.extend(["--url", public_url])
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert self._process.stdout is not None
        self._output_task = asyncio.create_task(self._capture_output(self._process.stdout))

        try:
            discovered_url = await self._discover_public_url(
                local_url,
                public_url,
                startup_timeout,
            )
        except BaseException:
            await self.stop()
            raise
        return Tunnel(provider=self.name, public_url=discovered_url, local_url=local_url)

    async def wait(self) -> None:
        if self._process is None:
            raise ProviderError("ngrok is not running")
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
