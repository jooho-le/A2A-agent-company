"""Owned listener/lifespan coordination without cloud, Docker or sockets.

Real Uvicorn shutdown/startup methods are tested through controlled fixtures;
the five-listener tests use owned fake sockets, not live TCP ports.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import io
import logging
import signal
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import uvicorn

from agents.platform import __main__ as cli
from agents.platform.composition import OwnedAgentPlatform, PlatformEndpoint, _Lifecycle
from agents.platform import runner


_NAMES = ("orchestrator", "planner", "developer", "qa", "security")
_PRIVATE = "private-source-and-credential-body"


class Provider:
    def __init__(self, events, *, fails=False):
        self.events, self.fails, self.calls = events, fails, 0

    async def aclose(self):
        self.calls += 1
        self.events.append(("provider-close",))
        if self.fails:
            raise RuntimeError(_PRIVATE)


def platform_fixture(events=None, *, provider_fails=False):
    events = [] if events is None else events
    apps = []
    for name in _NAMES:
        app = FastAPI()
        app.state.fixture_name = name
        apps.append(app)
    provider = Provider(events, fails=provider_fails)
    platform = OwnedAgentPlatform(orchestrator_app=apps[0],
        agent_apps=dict(zip(_NAMES[1:], apps[1:])), dispatcher=object(), budgets=object(),
        endpoints=tuple(PlatformEndpoint(name=name, host="127.0.0.1", port=18000 + index, app=app)
                        for index, (name, app) in enumerate(zip(_NAMES, apps))),
        _lifecycle=_Lifecycle(providers=(provider,)))
    return platform, provider


class FakeSocket:
    def __init__(self, harness, family, kind, ordinal):
        self.harness, self.family, self.kind, self.ordinal = harness, family, kind, ordinal
        self.bound, self.closed, self.close_calls = None, False, 0
        self.options = []

    def setsockopt(self, *values):
        self.options.append(values)

    def bind(self, address):
        if self.harness.bind_failure == self.ordinal:
            raise OSError(_PRIVATE)
        self.bound = address
        self.harness.events.append(("bind", address))

    def listen(self, backlog):
        self.backlog = backlog

    def setblocking(self, selected):
        self.blocking = selected

    def close(self):
        self.closed = True
        self.close_calls += 1


class FakeServer:
    def __init__(self, harness, config):
        self.harness, self.config = harness, config
        self.name = config.app.state.fixture_name
        self.started = self.should_exit = self.force_exit = self.return_now = self.closed = False
        self.servers = []

    async def serve(self, sockets=None):
        self.harness.events.append(("start", self.name, asyncio.get_running_loop(), tuple(sockets or ())))
        try:
            if self.name == self.harness.startup_failure:
                raise RuntimeError(_PRIVATE)
            gate = self.harness.startup_gates.get(self.name)
            while gate is not None and not gate.is_set() and not self.should_exit:
                await asyncio.sleep(.001)
            if self.should_exit:
                return
            self.started = True
            self.harness.events.append(("ready", self.name))
            while not self.should_exit and not self.return_now:
                await asyncio.sleep(.001)
        finally:
            self.harness.events.append(("shutdown", self.name))
            gate = self.harness.shutdown_gates.get(self.name)
            if gate is not None:
                await gate.wait()
            self.closed = True
            self.harness.events.append(("closed", self.name))
            if self.name == self.harness.shutdown_failure:
                raise RuntimeError(_PRIVATE)


class Harness:
    def __init__(self):
        self.events, self.sockets, self.servers = [], [], []
        self.bind_failure = self.socket_failure = None
        self.startup_failure = self.shutdown_failure = None
        self.startup_gates, self.shutdown_gates = {}, {}
        self.originals = {signal.SIGINT: object(), signal.SIGTERM: object()}
        self.handlers = dict(self.originals)
        self.signal_events = []

    def socket(self, family, kind):
        ordinal = len(self.sockets) + 1
        if self.socket_failure == ordinal:
            raise OSError(_PRIVATE)
        selected = FakeSocket(self, family, kind, ordinal)
        self.sockets.append(selected)
        return selected

    def server(self, config):
        selected = FakeServer(self, config)
        self.servers.append(selected)
        return selected

    def handle_signal(self, selected, handler):
        previous = self.handlers[selected]
        self.handlers[selected] = handler
        self.signal_events.append((selected, handler))
        return previous

    def request_stop(self, selected=signal.SIGINT):
        self.handlers[selected](selected, None)

    @contextmanager
    def installed(self):
        with patch.object(runner.socket, "socket", side_effect=self.socket), \
                patch.object(runner, "_OwnedServer", side_effect=self.server), \
                patch.object(runner.signal, "signal", side_effect=self.handle_signal):
            yield

    async def wait_for(self, predicate):
        deadline = asyncio.get_running_loop().time() + 2
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("fixture condition did not become ready")
            await asyncio.sleep(.001)

    def by_name(self, name):
        return next(server for server in self.servers if server.name == name)


class PlatformRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_prebound_listeners_one_loop_and_agents_ready_before_orchestrator(self):
        harness = Harness()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            harness.request_stop()
            await owned
        starts = [event for event in harness.events if event[0] == "start"]
        self.assertEqual([event[1] for event in starts], [*_NAMES[1:], "orchestrator"])
        self.assertEqual(len({id(event[2]) for event in starts}), 1)
        first_start = harness.events.index(starts[0])
        self.assertEqual(sum(event[0] == "bind" for event in harness.events[:first_start]), 5)
        orchestrator_start = harness.events.index(starts[-1])
        self.assertEqual({event[1] for event in harness.events[:orchestrator_start] if event[0] == "ready"}, set(_NAMES[1:]))
        for server, listener in zip(harness.servers, harness.sockets):
            self.assertEqual(server.config.workers, 1)
            self.assertFalse(server.config.reload)
            self.assertEqual(server.config.loop, "asyncio")
            self.assertEqual(server.config.lifespan, "on")
            self.assertIsNone(server.config.log_config)
            self.assertFalse(server.config.access_log)
            self.assertTrue(server.closed)
            self.assertFalse(server.force_exit)
            self.assertTrue(listener.closed)
            self.assertFalse(listener.blocking)
            self.assertEqual(listener.backlog, 128)
            passed = next(event[3] for event in starts if event[1] == server.name)
            self.assertEqual(passed, (listener,))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(harness.events[-1], ("provider-close",))
        self.assertEqual(harness.handlers, harness.originals)

    async def test_slow_agent_prevents_orchestrator_admission(self):
        harness = Harness()
        gate = harness.startup_gates["security"] = asyncio.Event()
        platform, _ = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len([event for event in harness.events if event[0] == "ready"]) == 3)
            self.assertFalse(harness.by_name("orchestrator").started)
            self.assertFalse(any(event[:2] == ("start", "orchestrator") for event in harness.events))
            gate.set()
            await harness.wait_for(lambda: harness.by_name("orchestrator").started)
            harness.request_stop(signal.SIGTERM)
            await owned

    async def test_startup_failure_stops_siblings_before_any_orchestrator_start(self):
        harness = Harness()
        harness.startup_failure = "qa"
        platform, provider = platform_fixture(harness.events)
        with harness.installed(), self.assertRaises(runner.PlatformRunnerError) as caught:
            await runner.run_platform(platform)
        self.assertEqual(caught.exception.code, "PLATFORM_STARTUP_FAILED")
        self.assertNotIn(_PRIVATE, repr(caught.exception))
        self.assertNotIn(("start", "orchestrator"), [event[:2] for event in harness.events])
        self.assertTrue(all(server.closed for server in harness.servers if server.name != "orchestrator"))
        self.assertTrue(all(listener.closed for listener in harness.sockets))
        self.assertEqual(provider.calls, 1)

    async def test_startup_deadline_rolls_back_all_started_lifespans(self):
        harness = Harness()
        harness.startup_gates["security"] = asyncio.Event()
        platform, provider = platform_fixture(harness.events)
        with harness.installed(), self.assertRaises(runner.PlatformRunnerError) as caught:
            await runner.run_platform(platform, startup_timeout_seconds=.02)
        self.assertEqual(caught.exception.code, "PLATFORM_STARTUP_TIMEOUT")
        self.assertTrue(all(server.closed for server in harness.servers if server.name != "orchestrator"))
        self.assertTrue(all(listener.closed for listener in harness.sockets))
        self.assertEqual(provider.calls, 1)

    async def test_stop_during_agent_startup_never_opens_orchestrator(self):
        harness = Harness()
        harness.startup_gates["security"] = asyncio.Event()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5)
            harness.request_stop()
            await owned
        self.assertFalse(harness.by_name("orchestrator").started)
        self.assertEqual(provider.calls, 1)

    async def test_shutdown_drains_orchestrator_dispatch_while_agents_still_accept(self):
        harness = Harness()
        gate = harness.shutdown_gates["orchestrator"] = asyncio.Event()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            harness.request_stop()
            await harness.wait_for(lambda: ("shutdown", "orchestrator") in harness.events)
            for name in _NAMES[1:]:
                self.assertFalse(harness.by_name(name).should_exit)
                self.assertFalse(harness.by_name(name).closed)
            self.assertEqual(provider.calls, 0)
            gate.set()
            await owned
        closed_orchestrator = harness.events.index(("closed", "orchestrator"))
        self.assertTrue(all(harness.events.index(("shutdown", name)) > closed_orchestrator for name in _NAMES[1:]))
        self.assertEqual(provider.calls, 1)

    async def test_shutdown_deadline_stops_public_accept_but_does_not_detach_cleanup(self):
        harness = Harness()
        gate = harness.shutdown_gates["orchestrator"] = asyncio.Event()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform, shutdown_timeout_seconds=.02))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            harness.request_stop()
            await harness.wait_for(lambda: harness.sockets[0].closed)
            self.assertFalse(owned.done())
            self.assertEqual(provider.calls, 0)
            self.assertTrue(all(not harness.by_name(name).should_exit for name in _NAMES[1:]))
            gate.set()
            with self.assertRaises(runner.PlatformRunnerError) as caught:
                await owned
        self.assertEqual(caught.exception.code, "PLATFORM_SHUTDOWN_TIMEOUT")
        self.assertTrue(all(server.closed for server in harness.servers))
        self.assertEqual(provider.calls, 1)

    async def test_repeated_sigint_sigterm_keep_graceful_handler_until_cleanup_done(self):
        harness = Harness()
        gate = harness.shutdown_gates["qa"] = asyncio.Event()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            harness.request_stop()
            await harness.wait_for(lambda: ("shutdown", "qa") in harness.events)
            harness.request_stop(signal.SIGINT)
            harness.request_stop(signal.SIGTERM)
            self.assertNotEqual(harness.handlers, harness.originals)
            self.assertTrue(all(not server.force_exit for server in harness.servers))
            self.assertEqual(provider.calls, 0)
            gate.set()
            await owned
        self.assertEqual(harness.handlers, harness.originals)
        self.assertEqual(provider.calls, 1)

    async def test_repeated_caller_cancellation_drains_then_propagates(self):
        harness = Harness()
        gate = harness.shutdown_gates["qa"] = asyncio.Event()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            owned.cancel()
            await harness.wait_for(lambda: ("shutdown", "qa") in harness.events)
            owned.cancel()
            await asyncio.sleep(.005)
            self.assertFalse(owned.done())
            self.assertEqual(provider.calls, 0)
            self.assertNotEqual(harness.handlers, harness.originals)
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await owned
        self.assertTrue(all(server.closed for server in harness.servers))
        self.assertTrue(all(listener.closed for listener in harness.sockets))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(harness.handlers, harness.originals)

    async def test_unexpected_server_stop_coordinates_all_siblings(self):
        harness = Harness()
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            await asyncio.sleep(.03)  # Let the launcher's startup readiness poll finish.
            harness.by_name("planner").return_now = True
            with self.assertRaises(runner.PlatformRunnerError) as caught:
                await owned
        self.assertEqual(caught.exception.code, "PLATFORM_SERVER_STOPPED")
        self.assertTrue(all(server.closed for server in harness.servers))
        self.assertEqual(provider.calls, 1)

    async def test_each_partial_bind_failure_closes_only_owned_sockets_before_lifespan(self):
        for ordinal in range(1, 6):
            with self.subTest(ordinal=ordinal):
                harness = Harness()
                harness.bind_failure = ordinal
                platform, provider = platform_fixture(harness.events)
                with harness.installed(), self.assertRaises(runner.PlatformRunnerError) as caught:
                    await runner.run_platform(platform)
                self.assertEqual(caught.exception.code, "PLATFORM_BIND_FAILED")
                self.assertEqual(len(harness.sockets), ordinal)
                self.assertTrue(all(listener.closed for listener in harness.sockets))
                self.assertEqual(harness.servers, [])
                self.assertEqual(provider.calls, 1)
                self.assertNotIn(_PRIVATE, str(caught.exception))

    async def test_socket_constructor_failure_rolls_back_previous_listeners(self):
        harness = Harness()
        harness.socket_failure = 3
        platform, provider = platform_fixture(harness.events)
        with harness.installed(), self.assertRaises(runner.PlatformRunnerError) as caught:
            await runner.run_platform(platform)
        self.assertEqual(caught.exception.code, "PLATFORM_BIND_FAILED")
        self.assertEqual(len(harness.sockets), 2)
        self.assertTrue(all(listener.closed for listener in harness.sockets))
        self.assertEqual(provider.calls, 1)

    async def test_config_failure_after_binding_closes_listeners_and_provider(self):
        harness = Harness()
        platform, provider = platform_fixture(harness.events)
        with harness.installed(), patch.object(runner.uvicorn, "Config", side_effect=RuntimeError(_PRIVATE)), \
                self.assertRaises(runner.PlatformRunnerError) as caught:
            await runner.run_platform(platform)
        self.assertEqual(caught.exception.code, "PLATFORM_STARTUP_FAILED")
        self.assertTrue(all(listener.closed for listener in harness.sockets))
        self.assertEqual(provider.calls, 1)

    async def test_invalid_deadlines_are_native_bounded_and_close_transferred_provider(self):
        for value in (True, None, "30", -.1, .001, float("nan"), float("inf"), 601):
            with self.subTest(value=value):
                harness = Harness()
                platform, provider = platform_fixture(harness.events)
                with harness.installed(), self.assertRaises(runner.PlatformRunnerError) as caught:
                    await runner.run_platform(platform, shutdown_timeout_seconds=value)
                self.assertEqual(caught.exception.code, "PLATFORM_RUNNER_CONFIGURATION_INVALID")
                self.assertEqual(harness.sockets, [])
                self.assertEqual(provider.calls, 1)

    async def test_cleanup_failure_is_safe_and_no_success_is_returned(self):
        harness = Harness()
        platform, provider = platform_fixture(harness.events, provider_fails=True)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            harness.request_stop()
            with self.assertRaises(runner.PlatformRunnerError) as caught:
                await owned
        self.assertEqual(caught.exception.code, "PLATFORM_CLEANUP_FAILED")
        self.assertNotIn(_PRIVATE, repr(caught.exception))
        self.assertEqual(provider.calls, 1)

    async def test_lifespan_failure_is_not_misreported_as_normal_shutdown(self):
        harness = Harness()
        harness.shutdown_failure = "security"
        platform, provider = platform_fixture(harness.events)
        with harness.installed():
            owned = asyncio.create_task(runner.run_platform(platform))
            await harness.wait_for(lambda: len(harness.servers) == 5 and all(s.started for s in harness.servers))
            harness.request_stop()
            with self.assertRaises(runner.PlatformRunnerError) as caught:
                await owned
        self.assertEqual(caught.exception.code, "PLATFORM_STARTUP_FAILED")
        self.assertEqual(provider.calls, 1)


class OwnedUvicornLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def server(self):
        return runner._OwnedServer(uvicorn.Config(FastAPI(), log_config=None, timeout_graceful_shutdown=.02))

    async def test_uvicorn_startup_systemexit_is_sanitized_without_killing_siblings(self):
        server = self.server()
        with patch.object(uvicorn.Server, "startup", side_effect=SystemExit(_PRIVATE)), \
                self.assertRaises(runner.PlatformRunnerError) as caught:
            await server.startup()
        self.assertEqual(caught.exception.code, "PLATFORM_STARTUP_FAILED")
        self.assertNotIn(_PRIVATE, repr(caught.exception))

    async def test_uvicorn_shutdown_owns_and_drains_background_before_lifespan(self):
        server = self.server()
        task_finished, release = asyncio.Event(), asyncio.Event()
        events = []

        async def dispatch():
            try:
                await release.wait()
                events.append("background-drained")
            finally:
                task_finished.set()

        async def lifespan_shutdown():
            self.assertTrue(task_finished.is_set())
            events.append("lifespan-closed")

        background = asyncio.create_task(dispatch())
        server.server_state.tasks.add(background)
        background.add_done_callback(server.server_state.tasks.discard)
        server.lifespan = SimpleNamespace(shutdown=lifespan_shutdown)
        server.servers = []
        shutdown = asyncio.create_task(server.shutdown())
        await asyncio.sleep(.15)  # Beyond configured .02; no dispatch cancellation/detachment.
        self.assertFalse(shutdown.done())
        self.assertFalse(background.cancelled())
        self.assertIsNone(server.config.timeout_graceful_shutdown)
        release.set()
        await shutdown
        self.assertEqual(events, ["background-drained", "lifespan-closed"])
        self.assertEqual(server.config.timeout_graceful_shutdown, .02)

    async def test_repeated_cancel_of_uvicorn_shutdown_still_awaits_exactly_one_cleanup(self):
        server = self.server()
        started, release = asyncio.Event(), asyncio.Event()

        async def owned_shutdown(*args, **kwargs):
            started.set()
            await release.wait()

        with patch.object(uvicorn.Server, "shutdown", side_effect=owned_shutdown) as parent:
            shutdown = asyncio.create_task(server.shutdown())
            await started.wait()
            shutdown.cancel()
            await asyncio.sleep(.001)
            shutdown.cancel()
            await asyncio.sleep(.001)
            self.assertFalse(shutdown.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await shutdown
            await server.shutdown()
        self.assertEqual(parent.await_count, 1)

    async def test_partial_startup_after_lifespan_is_unwound_even_without_started_flag(self):
        server = self.server()
        started = asyncio.Event()
        started.set()
        server.lifespan = SimpleNamespace(startup_event=started, error_occurred=False,
                                         startup_failed=False, shutdown_failed=False)
        with patch.object(uvicorn.Server, "serve", side_effect=RuntimeError(_PRIVATE)), \
                patch.object(uvicorn.Server, "shutdown", new_callable=AsyncMock) as closing, \
                self.assertRaises(runner.PlatformRunnerError):
            await server.serve()
        self.assertEqual(closing.await_count, 1)
        self.assertEqual(server.servers, [])

    async def test_uvicorn_each_server_signal_capture_is_disabled(self):
        server = self.server()
        with patch.object(runner.signal, "signal", side_effect=AssertionError("single launcher only")):
            with server.capture_signals():
                pass


class PlatformRunnerAdmissionTests(unittest.TestCase):
    def test_exact_platform_five_native_unique_loopback_endpoints_only(self):
        platform, _ = platform_fixture()
        with self.assertRaises(runner.PlatformRunnerError):
            runner._owned_endpoints(SimpleNamespace(endpoints=platform.endpoints))
        changes = [list(platform.endpoints), platform.endpoints[:4],
                   (platform.endpoints[0], *platform.endpoints[1:4], platform.endpoints[0])]
        for endpoints in changes:
            with self.subTest(endpoints=len(endpoints)), self.assertRaises(runner.PlatformRunnerError):
                runner._owned_endpoints(replace(platform, endpoints=endpoints))
        alias = replace(platform.endpoints[1], host="localhost", port=platform.endpoints[0].port)
        with self.assertRaises(runner.PlatformRunnerError):
            runner._owned_endpoints(replace(platform, endpoints=(platform.endpoints[0], alias, *platform.endpoints[2:])))
        self.assertIs(runner._owned_endpoints(platform), platform.endpoints)

    def test_ipv6_loopback_is_v6_only_and_localhost_binds_numeric_without_dns(self):
        harness = Harness()
        platform, _ = platform_fixture()
        endpoints = (replace(platform.endpoints[0], host="::1"),
                     replace(platform.endpoints[1], host="localhost"), *platform.endpoints[2:])
        with patch.object(runner.socket, "socket", side_effect=harness.socket), \
                patch.object(runner.socket, "getaddrinfo", side_effect=AssertionError("no remote DNS")):
            listeners = runner._bind(endpoints)
        self.assertEqual(listeners[0].family, socket.AF_INET6)
        self.assertIn((socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1), listeners[0].options)
        self.assertEqual(listeners[0].bound, ("::1", endpoints[0].port))
        self.assertEqual(listeners[1].bound, ("127.0.0.1", endpoints[1].port))
        for listener in listeners:
            listener.close()

    def test_factory_rejects_filepaths_commands_uris_unicode_and_bad_shape_before_import(self):
        submitted = (None, True, "", "factory", "/private/factory.py:make", "../factory:make",
                     "https://private:make", "module:make()", "module:make.child", "module:make;exec",
                     "module:make\n", "modulé:make", "module:ｍake", "m" * 257 + ":make")
        with patch.object(runner.importlib, "import_module", side_effect=AssertionError("must not import")):
            for value in submitted:
                with self.subTest(value=value), self.assertRaises(runner.PlatformRunnerError) as caught:
                    runner.load_platform_factory(value)
                self.assertEqual(caught.exception.code, "PLATFORM_FACTORY_INVALID")

    def test_factory_explicit_module_callable_load_and_safe_failures(self):
        factory = lambda: None
        with patch.object(runner.importlib, "import_module", return_value=SimpleNamespace(make=factory)) as loaded:
            self.assertIs(runner.load_platform_factory("trusted_operator.local:make"), factory)
        loaded.assert_called_once_with("trusted_operator.local")
        for module in (SimpleNamespace(), SimpleNamespace(make=1)):
            with patch.object(runner.importlib, "import_module", return_value=module), \
                    self.assertRaises(runner.PlatformRunnerError):
                runner.load_platform_factory("trusted_operator.local:make")
        with patch.object(runner.importlib, "import_module", side_effect=RuntimeError(_PRIVATE)), \
                self.assertRaises(runner.PlatformRunnerError) as caught:
            runner.load_platform_factory("trusted_operator.local:make")
        self.assertEqual(str(caught.exception), "PLATFORM_FACTORY_INVALID")

    def test_uvicorn_raw_exception_messages_are_stable_and_filter_scope_is_restored(self):
        logger = logging.getLogger("uvicorn.error")
        original = tuple(logger.filters)
        captured = []

        class Recording(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = Recording()
        logger.addHandler(handler)
        try:
            with runner._server_logs():
                try:
                    raise RuntimeError(_PRIVATE)
                except RuntimeError:
                    logger.error(_PRIVATE, exc_info=True, stack_info=True)
            self.assertEqual(tuple(logger.filters), original)
        finally:
            logger.removeHandler(handler)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].getMessage(), "OWNED_PLATFORM_SERVER_EVENT")
        self.assertIsNone(captured[0].exc_info)
        self.assertIsNone(captured[0].exc_text)
        self.assertIsNone(captured[0].stack_info)

    def test_error_codes_do_not_accept_arbitrary_public_content(self):
        error = runner.PlatformRunnerError(_PRIVATE)
        self.assertEqual(error.code, "PLATFORM_RUNNER_CONFIGURATION_INVALID")
        self.assertNotIn(_PRIVATE, repr(error))


class PlatformCLITests(unittest.TestCase):
    def call(self, arguments):
        stderr = io.StringIO()
        with patch.object(cli.sys, "stderr", stderr):
            status = cli.main(arguments)
        return status, stderr.getvalue()

    def test_cli_requires_explicit_factory_and_never_echoes_bad_input(self):
        with patch.object(cli, "load_platform_factory", side_effect=AssertionError("no automatic factory")):
            status, stderr = self.call([])
            self.assertEqual((status, stderr), (1, "PLATFORM_FACTORY_INVALID\n"))
            status, stderr = self.call(["--unrecognized", _PRIVATE])
            self.assertEqual((status, stderr), (1, "PLATFORM_FACTORY_INVALID\n"))
            status, stderr = self.call(["--factory", "trusted:make", "--startup-timeout", _PRIVATE])
            self.assertEqual((status, stderr), (1, "PLATFORM_FACTORY_INVALID\n"))

    def test_cli_passes_exact_owned_platform_and_explicit_deadlines(self):
        platform, _ = platform_fixture()
        with patch.object(cli, "load_platform_factory", return_value=lambda: platform) as loading, \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as running:
            status, stderr = self.call(["--factory", "trusted_operator.local:make",
                                       "--startup-timeout", "12", "--shutdown-timeout", "34"])
        self.assertEqual((status, stderr), (0, ""))
        loading.assert_called_once_with("trusted_operator.local:make")
        running.assert_awaited_once_with(platform, startup_timeout_seconds=12., shutdown_timeout_seconds=34.)

    def test_cli_wrong_return_type_and_factory_error_are_safe(self):
        factories = (lambda: object(), lambda: (_ for _ in ()).throw(RuntimeError(_PRIVATE)),
                     lambda: (_ for _ in ()).throw(SystemExit(_PRIVATE)))
        for factory in factories:
            with self.subTest(factory=factory), patch.object(cli, "load_platform_factory", return_value=factory):
                status, stderr = self.call(["--factory", "trusted:make"])
                self.assertEqual((status, stderr), (1, "PLATFORM_FACTORY_INVALID\n"))

    def test_async_factory_is_not_accepted_or_left_as_unawaited_coroutine(self):
        returned = []

        async def unsupported():
            return object()

        def factory():
            coroutine = unsupported()
            returned.append(coroutine)
            return coroutine

        with patch.object(cli, "load_platform_factory", return_value=factory):
            status, stderr = self.call(["--factory", "trusted:make"])
        self.assertEqual((status, stderr), (1, "PLATFORM_FACTORY_INVALID\n"))
        self.assertIsNone(returned[0].cr_frame)

    def test_cli_runner_errors_and_unexpected_errors_never_render_raw_prose(self):
        platform, _ = platform_fixture()
        for failure, expected in ((runner.PlatformRunnerError("PLATFORM_BIND_FAILED"), "PLATFORM_BIND_FAILED"),
                                  (RuntimeError(_PRIVATE), "PLATFORM_STARTUP_FAILED")):
            with self.subTest(expected=expected), patch.object(cli, "load_platform_factory", return_value=lambda: platform), \
                    patch.object(cli, "run_platform", new_callable=AsyncMock, side_effect=failure):
                status, stderr = self.call(["--factory", "trusted:make"])
                self.assertEqual((status, stderr), (1, expected + "\n"))


if __name__ == "__main__":
    unittest.main()
