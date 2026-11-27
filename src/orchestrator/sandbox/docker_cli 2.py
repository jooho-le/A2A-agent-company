"""Bounded Docker control-plane subprocesses; never a Host project executor.

Only trusted Host code supplies ``arguments`` or the optional binary path.
The constructor is side-effect free: Docker discovery and execution happen
only in an explicitly awaited ``run``.  CLI environment values are not
container environment values, and the runtime validates container settings
separately before starting project code.
"""

import asyncio
import math
import os
from pathlib import PurePath
import shutil
import signal
import tempfile
import time

from orchestrator.sandbox.contracts import CLIResult, SandboxError, SandboxErrorCode


DOCKER_SEARCH_PATH = (
    "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:"
    "/Applications/Docker.app/Contents/Resources/bin"
)
_MAX_ARGUMENTS = 256
_MAX_ARGUMENT_BYTES = 8192
_MAX_ARGUMENTS_BYTES = 131072
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def _safe_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and len(value.encode("utf-8", errors="surrogatepass")) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def _client_environment() -> dict[str, str]:
    # Do not inherit DOCKER_HOST/CONTEXT/TLS/API_VERSION, API keys, proxy
    # variables, Python settings, loader settings, or project environment.
    return {"PATH": DOCKER_SEARCH_PATH, "LANG": "C", "LC_ALL": "C"}


async def _read_bounded(stream: asyncio.StreamReader, maximum: int) -> bytes:
    output = bytearray()
    while True:
        # Read at most one byte beyond the cap, even when a child writes a
        # large stream. Total retained output is bounded by both stream caps.
        chunk = await stream.read(min(65536, maximum + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > maximum:
            raise SandboxError(SandboxErrorCode.OUTPUT_LIMIT)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        # start_new_session below makes this the group created by this call,
        # including descendants that keep stdout/stderr pipes open.
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass


async def _reap(process: asyncio.subprocess.Process, tasks: list[asyncio.Task]) -> None:
    _kill_group(process)
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # StreamReader may have paused its pipe transport after a large burst.
    # Cancelling a bounded reader alone leaves buffered bytes above that
    # threshold and can prevent EOF/process.wait() completion. After kill,
    # discard remaining pipe data in fixed chunks, never retain or return it.
    async def discard(stream):
        while await stream.read(65536):
            pass
    await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


async def _finish_cleanup(process: asyncio.subprocess.Process, tasks: list[asyncio.Task]) -> None:
    cleanup = asyncio.create_task(_reap(process, tasks))
    # Repeated external cancellation must not abandon a child/pipe reader.
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
        except (OSError, RuntimeError):
            break
    try:
        cleanup.result()
    except (OSError, RuntimeError):
        raise SandboxError(SandboxErrorCode.EXECUTION) from None


async def _finish_startup_cleanup(startup: asyncio.Task) -> None:
    # Keep ownership of a subprocess whose creation has already begun, even
    # if cancellation arrives before create_subprocess_exec returns its
    # Process handle. Repeated cancellation cannot discard this handle.
    while not startup.done():
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            continue
        except (OSError, RuntimeError, TypeError, ValueError):
            break
    try:
        process = startup.result()
    except (OSError, RuntimeError, TypeError, ValueError):
        return
    await _finish_cleanup(process, [])


async def _wait_bounded(future: asyncio.Future, timeout_seconds: float):
    # asyncio.wait does not cancel its children and does not turn an external
    # cancellation racing with child completion into a successful result
    # (the historical wait_for race on supported Python versions).
    loop = asyncio.get_running_loop()
    expired = loop.create_future()
    timer = loop.call_later(timeout_seconds, expired.set_result, None)
    try:
        await asyncio.wait((future, expired), return_when=asyncio.FIRST_COMPLETED)
        if expired.done():
            raise asyncio.TimeoutError
        return future.result()
    finally:
        timer.cancel()
        expired.cancel()


class DockerCLI:
    """Host-owned local Docker CLI configuration, not a Model/Tool argument."""

    def __init__(self, endpoint: str = "unix:///var/run/docker.sock", executable: str | None = None):
        if (
            not _safe_text(endpoint, maximum=4096)
            or not endpoint.startswith("unix:///")
            or any(char in endpoint for char in "\\?#")
            or any(part in {"", ".", ".."} for part in endpoint[8:].split("/"))
        ):
            raise SandboxError(SandboxErrorCode.INVALID)
        if executable is not None and (
            not _safe_text(executable, maximum=4096)
            or not os.path.isabs(executable)
            or PurePath(executable).name != "docker"
            or ".." in PurePath(executable).parts
        ):
            raise SandboxError(SandboxErrorCode.INVALID)
        self.endpoint = endpoint
        self._executable = executable

    async def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CLIResult:
        if (
            not isinstance(arguments, tuple)
            or not 1 <= len(arguments) <= _MAX_ARGUMENTS
            or any(not _safe_text(arg, maximum=_MAX_ARGUMENT_BYTES) for arg in arguments)
            or sum(len(arg.encode("utf-8", errors="surrogatepass")) for arg in arguments) > _MAX_ARGUMENTS_BYTES
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (float, int))
            or not math.isfinite(timeout_seconds)
            or not 0.01 <= timeout_seconds <= 600.0
            or any(
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_OUTPUT_BYTES
                for limit in (max_stdout_bytes, max_stderr_bytes)
            )
        ):
            raise SandboxError(SandboxErrorCode.INVALID)
        if os.name != "posix":
            raise SandboxError(SandboxErrorCode.UNAVAILABLE)
        executable = self._executable or shutil.which("docker", path=DOCKER_SEARCH_PATH)
        if executable is None:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE)
        # A private, empty Docker client configuration prevents implicit
        # proxy credentials/container environment from ~/.docker/config.json.
        # This control-plane temporary directory is never a container mount.
        try:
            with tempfile.TemporaryDirectory(prefix="a2a-docker-control-") as config_directory:
                return await self._run_process(
                    executable, config_directory, arguments, timeout_seconds,
                    max_stdout_bytes, max_stderr_bytes,
                )
        except OSError:
            raise SandboxError(SandboxErrorCode.EXECUTION) from None

    async def _run_process(
        self, executable: str, config_directory: str, arguments: tuple[str, ...],
        timeout_seconds: float, max_stdout_bytes: int, max_stderr_bytes: int,
    ) -> CLIResult:
        started = time.monotonic()
        startup = asyncio.create_task(asyncio.create_subprocess_exec(
                executable,
                "--host",
                self.endpoint,
                "--config",
                config_directory,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_client_environment(),
                start_new_session=True,
            ))
        try:
            process = await _wait_bounded(startup, timeout_seconds)
        except asyncio.TimeoutError:
            await _finish_startup_cleanup(startup)
            raise SandboxError(SandboxErrorCode.TIMEOUT) from None
        except asyncio.CancelledError:
            await _finish_startup_cleanup(startup)
            raise
        except OSError:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE) from None
        except (RuntimeError, TypeError, ValueError):
            raise SandboxError(SandboxErrorCode.EXECUTION) from None
        remaining_seconds = timeout_seconds - (time.monotonic() - started)
        if remaining_seconds <= 0:
            await _finish_cleanup(process, [])
            raise SandboxError(SandboxErrorCode.TIMEOUT) from None
        tasks = [
            asyncio.create_task(_read_bounded(process.stdout, max_stdout_bytes)),
            asyncio.create_task(_read_bounded(process.stderr, max_stderr_bytes)),
            asyncio.create_task(process.wait()),
        ]
        joined = asyncio.gather(*tasks)
        try:
            stdout, stderr, returncode = await _wait_bounded(joined, remaining_seconds)
            return CLIResult(returncode=returncode, stdout=stdout, stderr=stderr)
        except asyncio.TimeoutError:
            await _finish_cleanup(process, tasks)
            raise SandboxError(SandboxErrorCode.TIMEOUT) from None
        except asyncio.CancelledError:
            await _finish_cleanup(process, tasks)
            raise
        except SandboxError:
            await _finish_cleanup(process, tasks)
            raise
        except (OSError, RuntimeError):
            await _finish_cleanup(process, tasks)
            raise SandboxError(SandboxErrorCode.EXECUTION) from None
        finally:
            # Its children were reaped above on failure, but gather's
            # aggregate CancelledError must also be consumed before exiting.
            if joined.done() and not joined.cancelled():
                joined.exception()
