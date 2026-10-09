"""Explicit single-process Host listener ownership and coordinated shutdown.

All five apps run on one event loop and share the composition's Run budget.
This module never creates per-role budgets, restores a restarted Run, chooses
a provider, or discovers configuration from A2A messages/environment alone.
Shutdown deadlines detect slow drainage; they never authorize abandoning an
already-started ASGI/MCP/Workspace cleanup or claiming a hard exit bound.
"""

import asyncio
from contextlib import contextmanager
import importlib
import logging
import math
import re
import signal
import socket
import threading
from time import monotonic

import uvicorn

from agents.platform.composition import OwnedAgentPlatform, PlatformEndpoint


_NAMES = frozenset({"orchestrator", "planner", "developer", "qa", "security"})
_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_FACTORY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z", re.ASCII)
_CODES = frozenset({
    "PLATFORM_RUNNER_CONFIGURATION_INVALID", "PLATFORM_FACTORY_INVALID",
    "PLATFORM_BIND_FAILED", "PLATFORM_STARTUP_FAILED", "PLATFORM_STARTUP_TIMEOUT",
    "PLATFORM_SERVER_STOPPED", "PLATFORM_SHUTDOWN_TIMEOUT", "PLATFORM_CLEANUP_FAILED",
})


class PlatformRunnerError(RuntimeError):
    """Stable public reason only; no Host paths, factory text or exceptions."""

    def __init__(self, code="PLATFORM_RUNNER_CONFIGURATION_INVALID"):
        self.code = code if code in _CODES else "PLATFORM_RUNNER_CONFIGURATION_INVALID"
        super().__init__(self.code)


class _ServerLogFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn logs lifespan failure messages and ASGI exception tracebacks
        # itself. Those may contain arbitrary request/provider/Host content.
        record.msg, record.args = "OWNED_PLATFORM_SERVER_EVENT", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


@contextmanager
def _server_logs():
    selected = [logging.getLogger(name) for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")]
    safe = _ServerLogFilter()
    try:
        for logger in selected:
            logger.addFilter(safe)
        yield
    finally:
        for logger in selected:
            logger.removeFilter(safe)


async def _drain(task):
    """Repeated cancellation cannot detach a cleanup already owned by Host."""
    canceled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            canceled = True
    result = task.result()
    return result, canceled


class _OwnedServer(uvicorn.Server):
    """One launcher owns signals; each Server still owns its ASGI lifespan."""

    def __init__(self, config):
        super().__init__(config)
        self._owned_shutdown = None

    @contextmanager
    def capture_signals(self):
        # Five nested Uvicorn handlers would replace each other and second
        # Ctrl-C would skip lifespan cleanup. Only the launcher handles them.
        yield

    async def startup(self, sockets=None):
        invalid = False
        try:
            await super().startup(sockets=sockets)
        except SystemExit:
            # Uvicorn turns a failed lifespan into sys.exit. A SystemExit in
            # one concurrent Task must not terminate its sibling event loop.
            invalid = True
        if invalid:
            raise PlatformRunnerError("PLATFORM_STARTUP_FAILED") from None

    async def shutdown(self, sockets=None):
        if self._owned_shutdown is None:
            self._owned_shutdown = asyncio.create_task(self._shutdown_owned(sockets))
        _, canceled = await _drain(self._owned_shutdown)
        if canceled:
            raise asyncio.CancelledError()

    async def _shutdown_owned(self, sockets):
        # Uvicorn otherwise cancels background dispatch at its deadline and
        # closes the lifespan without awaiting that task's cancellation cleanup.
        # The launcher detects the deadline, closes public listeners and keeps
        # owning drainage; it must not close Agents underneath that dispatch.
        original = self.config.timeout_graceful_shutdown
        self.config.timeout_graceful_shutdown = None
        try:
            await super().shutdown(sockets=sockets)
        finally:
            self.config.timeout_graceful_shutdown = original

    async def serve(self, sockets=None):
        invalid, canceled = False, False
        try:
            await super().serve(sockets=sockets)
        except asyncio.CancelledError:
            canceled = True
        except Exception:
            invalid = True
        finally:
            lifespan = getattr(self, "lifespan", None)
            # Also unwind a successful lifespan if socket creation failed
            # before Uvicorn could set self.started=True.
            if (self._owned_shutdown is None and lifespan is not None
                    and lifespan.startup_event.is_set() and not lifespan.error_occurred
                    and not lifespan.startup_failed):
                if not hasattr(self, "servers"):
                    self.servers = []
                self._owned_shutdown = asyncio.create_task(self._shutdown_owned(sockets))
            if self._owned_shutdown is not None:
                try:
                    _, during_cleanup = await _drain(self._owned_shutdown)
                    canceled |= during_cleanup
                    if lifespan is not None and lifespan.shutdown_failed:
                        invalid = True
                except Exception:
                    invalid = True
        if canceled:
            raise asyncio.CancelledError()
        if invalid:
            raise PlatformRunnerError("PLATFORM_STARTUP_FAILED") from None


def _owned_endpoints(platform):
    invalid = False
    try:
        if type(platform) is not OwnedAgentPlatform:
            raise ValueError
        endpoints = platform.endpoints
        if (type(endpoints) is not tuple or len(endpoints) != 5
                or any(type(item) is not PlatformEndpoint for item in endpoints)
                or {item.name for item in endpoints} != _NAMES
                or not callable(platform.aclose)):
            raise ValueError
        identities = []
        for endpoint in endpoints:
            if (type(endpoint.name) is not str or type(endpoint.host) is not str
                    or endpoint.host not in _HOSTS or type(endpoint.port) is not int
                    or not 1 <= endpoint.port <= 65535 or not callable(endpoint.app)):
                raise ValueError
            identities.append(("127.0.0.1" if endpoint.host == "localhost" else endpoint.host, endpoint.port))
        if len(set(identities)) != len(identities):
            raise ValueError
    except Exception:
        invalid = True
    if invalid:
        raise PlatformRunnerError() from None
    return endpoints


def _timeout(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= maximum:
        raise PlatformRunnerError()
    return float(value)


def _bind(endpoints):
    """Reserve every listener before any ASGI lifespan starts; rollback owned FDs."""
    owned, invalid = [], False
    try:
        for endpoint in endpoints:
            family = socket.AF_INET6 if endpoint.host == "::1" else socket.AF_INET
            listener = socket.socket(family, socket.SOCK_STREAM)
            owned.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            host = "127.0.0.1" if endpoint.host == "localhost" else endpoint.host
            listener.bind((host, endpoint.port))
            listener.listen(128)
            listener.setblocking(False)
    except Exception:
        invalid = True
    if invalid:
        for listener in owned:
            listener.close()
        raise PlatformRunnerError("PLATFORM_BIND_FAILED") from None
    return tuple(owned)


@contextmanager
def _signals(stop):
    originals = {}

    def stop_requested(_signal, _frame):
        # Repeated SIGINT/SIGTERM only request the same graceful shutdown.
        stop.set()

    try:
        if threading.current_thread() is threading.main_thread():
            for selected in (signal.SIGINT, signal.SIGTERM):
                originals[selected] = signal.signal(selected, stop_requested)
        yield
    finally:
        for selected, original in originals.items():
            signal.signal(selected, original)


async def _ready(servers, tasks, stop, deadline):
    while True:
        if stop.is_set():
            return False
        if any(task.done() for task in tasks):
            raise PlatformRunnerError("PLATFORM_STARTUP_FAILED")
        if all(server.started for server in servers):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise PlatformRunnerError("PLATFORM_STARTUP_TIMEOUT")
        await asyncio.wait(tasks, timeout=min(.02, remaining), return_when=asyncio.FIRST_COMPLETED)


async def _finish(platform, servers, tasks, listeners, timeout):
    failure = None
    deadline = monotonic() + timeout
    try:
        orchestrator = next((index for index, endpoint in enumerate(platform.endpoints)
                             if endpoint.name == "orchestrator"), None)
        orchestration_tasks = [task for task in tasks if task.get_name() == "owned-platform-orchestrator"]
        if orchestration_tasks and orchestrator is not None:
            servers[orchestrator].should_exit = True
            _, pending = await asyncio.wait(orchestration_tasks, timeout=max(0, deadline - monotonic()))
            if pending:
                failure = "PLATFORM_SHUTDOWN_TIMEOUT"
                for listener in getattr(servers[orchestrator], "servers", ()):
                    listener.close()
                listeners[orchestrator].close()
            # Keep all four Agent listeners/SDK workers available while the
            # already accepted Orchestrator background dispatch is draining.
            await asyncio.gather(*orchestration_tasks, return_exceptions=True)
        for server in servers:
            server.should_exit = True
            # Never use force_exit: it skips official ASGI lifespan shutdown.
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - monotonic()))
            if pending:
                failure = "PLATFORM_SHUTDOWN_TIMEOUT"
                for server in servers:
                    for listener in getattr(server, "servers", ()):
                        listener.close()
                for listener in listeners:
                    listener.close()
                # Timed-out safety cleanup remains owned. Do not detach a
                # mutating Worker, MCP child or durable Task-store lease.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results) and failure is None:
                failure = "PLATFORM_STARTUP_FAILED"
    finally:
        for listener in listeners:
            listener.close()
        try:
            await platform.aclose()
        except Exception:
            failure = "PLATFORM_CLEANUP_FAILED"
    return failure


async def run_platform(platform, *, startup_timeout_seconds=30, shutdown_timeout_seconds=60):
    """Own exactly one configured Host, five loopback endpoints, and one loop.

    Agent apps start before the Orchestrator starts accepting work. Port bind
    rollback closes only these sockets; it does not delete databases/history.
    Unknown/restarted Run budgets are handled fail-closed by composition.
    """
    endpoints = _owned_endpoints(platform)
    listeners, servers, tasks = (), [], []
    failure, canceled = None, False
    shutdown_timeout = 60
    stop = asyncio.Event()
    # Keep the signal owner installed THROUGH failure/cancellation cleanup.
    # A second Ctrl-C must not restore the default handler mid-drain.
    with _server_logs(), _signals(stop):
        try:
            startup_timeout = _timeout(startup_timeout_seconds, 300)
            shutdown_timeout = _timeout(shutdown_timeout_seconds, 600)
            listeners = _bind(endpoints)
            for endpoint in endpoints:
                config = uvicorn.Config(endpoint.app, host=endpoint.host, port=endpoint.port,
                    workers=1, reload=False, loop="asyncio", lifespan="on", log_config=None,
                    access_log=False, timeout_graceful_shutdown=shutdown_timeout)
                servers.append(_OwnedServer(config))
            deadline = monotonic() + startup_timeout
            role_indexes = [index for index, endpoint in enumerate(endpoints) if endpoint.name != "orchestrator"]
            for index in role_indexes:
                tasks.append(asyncio.create_task(servers[index].serve(sockets=[listeners[index]]),
                                                 name="owned-platform-" + endpoints[index].name))
            ready = await _ready([servers[index] for index in role_indexes], tasks, stop, deadline)
            if ready:
                index = next(index for index, endpoint in enumerate(endpoints) if endpoint.name == "orchestrator")
                tasks.append(asyncio.create_task(servers[index].serve(sockets=[listeners[index]]),
                                                 name="owned-platform-orchestrator"))
                ready = await _ready(servers, tasks, stop, deadline)
            if ready:
                stopper = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait((*tasks, stopper), return_when=asyncio.FIRST_COMPLETED)
                    if stopper not in done:
                        failure = "PLATFORM_SERVER_STOPPED"
                finally:
                    stopper.cancel()
                    await asyncio.gather(stopper, return_exceptions=True)
        except asyncio.CancelledError:
            canceled = True
        except PlatformRunnerError as error:
            failure = error.code
        except Exception:
            failure = "PLATFORM_STARTUP_FAILED"
        finally:
            cleanup = asyncio.create_task(_finish(platform, servers, tasks, listeners, shutdown_timeout))
            cleanup_failure, during_cleanup = await _drain(cleanup)
            canceled |= during_cleanup
            failure = cleanup_failure or failure
    if canceled:
        raise asyncio.CancelledError()
    if failure:
        raise PlatformRunnerError(failure) from None


def load_platform_factory(value):
    """Load only an operator-supplied module:callable, never a file/URI/eval.

    Importing this explicitly trusted module can execute operator code. It is
    not a sandbox or an allowlist grant for model/user-supplied configuration.
    """
    invalid = False
    try:
        if type(value) is not str or len(value) > 256 or _FACTORY.fullmatch(value) is None:
            raise ValueError
        module, name = value.split(":")
        factory = getattr(importlib.import_module(module), name)
        if not callable(factory):
            raise ValueError
    except Exception:
        invalid = True
    if invalid:
        raise PlatformRunnerError("PLATFORM_FACTORY_INVALID") from None
    return factory
