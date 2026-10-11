"""Exercise a fake control CLI, never Docker or project code on the Host."""

import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.sandbox.docker_cli import DOCKER_SEARCH_PATH, DockerCLI


_FAKE_DOCKER = r'''
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[5:]
mode = args[0]
if mode == "inspect-client":
    config = Path(sys.argv[4])
    print(json.dumps({
        "argv": sys.argv[1:], "environment": dict(os.environ),
        "configMode": config.stat().st_mode & 0o777,
        "configFiles": sorted(p.name for p in config.iterdir()),
    }))
elif mode == "exit":
    os.write(1, b"result-without-newline")
    os.write(2, b"sensitive-stderr")
    sys.exit(int(args[1]))
elif mode == "stdout":
    os.write(1, b"x" * int(args[1]))
elif mode == "stderr":
    os.write(2, b"private-secret-" + b"x" * int(args[1]))
elif mode == "both":
    os.write(1, b"x" * int(args[1]))
    os.write(2, b"y" * int(args[1]))
elif mode == "sleep":
    time.sleep(30)
elif mode == "descendant-pipe":
    # The original CLI exits, but its child retains stdout/stderr handles.
    # Process-only termination would not close these pipes.
    child = os.fork()
    if child == 0:
        time.sleep(30)
        os._exit(0)
    os._exit(0)
else:
    raise RuntimeError("unexpected fake mode")
'''


@unittest.skipUnless(os.name == "posix", "Docker sandbox control uses POSIX process groups")
class DockerCLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-fake-docker-")
        self.binary = Path(self.temporary.name) / "docker"
        self.binary.write_text(f"#!{sys.executable}\n" + _FAKE_DOCKER, encoding="utf-8")
        self.binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.cli = DockerCLI(executable=str(self.binary))

    def tearDown(self):
        self.temporary.cleanup()

    async def run_cli(self, arguments, *, timeout=2.0, stdout=4096, stderr=4096):
        return await self.cli.run(
            arguments, timeout_seconds=timeout,
            max_stdout_bytes=stdout, max_stderr_bytes=stderr,
        )

    async def test_constructor_performs_no_binary_discovery_or_subprocess_io(self):
        with (
            patch("orchestrator.sandbox.docker_cli.shutil.which", side_effect=AssertionError("I/O")),
            patch("orchestrator.sandbox.docker_cli.tempfile.TemporaryDirectory", side_effect=AssertionError("I/O")),
            patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=AssertionError("I/O")),
        ):
            DockerCLI()
            DockerCLI(endpoint="unix:///Users/local/.docker/run/docker.sock", executable=str(self.binary))

    async def test_remote_and_malformed_endpoints_are_rejected_without_io(self):
        for endpoint in (
            "tcp://localhost:2375", "ssh://host", "http://host", "unix://relative", "unix:///",
            "unix:////var/run/docker.sock", "unix:///var/../docker.sock", "unix:///var/./docker.sock",
            "unix:///var/run/docker.sock?secret=key", "unix:///var/run/docker.sock#fragment",
            "unix:///var\\run\\docker.sock", "unix:///var/run/\x00docker.sock", "unix:///var/run/\ud800",
            "unix:///var/run/\ndocker.sock", None, 12,
        ):
            with self.subTest(endpoint=repr(endpoint)), self.assertRaises(SandboxError) as caught:
                DockerCLI(endpoint=endpoint)
            self.assertEqual(caught.exception.code, SandboxErrorCode.INVALID)

    async def test_executable_is_a_host_absolute_docker_path_only(self):
        for executable in ("docker", "./docker", "/usr/bin/python3", "/usr/bin/../bin/docker", "/tmp/docker\n", False):
            with self.subTest(executable=executable), self.assertRaises(SandboxError) as caught:
                DockerCLI(executable=executable)
            self.assertEqual(caught.exception.code, SandboxErrorCode.INVALID)

    async def test_missing_binary_is_generic_unavailable_and_not_auto_installed(self):
        cli = DockerCLI()
        with (
            patch("orchestrator.sandbox.docker_cli.shutil.which", return_value=None) as find,
            patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec") as spawn,
        ):
            with self.assertRaises(SandboxError) as caught:
                await cli.run(("info",), timeout_seconds=1.0, max_stdout_bytes=100, max_stderr_bytes=100)
        find.assert_called_once_with("docker", path=DOCKER_SEARCH_PATH)
        spawn.assert_not_called()
        self.assertEqual(str(caught.exception), SandboxErrorCode.UNAVAILABLE.value)

    async def test_discovery_ignores_arbitrary_host_path(self):
        with patch("orchestrator.sandbox.docker_cli.shutil.which", return_value=str(self.binary)) as find:
            result = await DockerCLI().run(("exit", "0"), timeout_seconds=2, max_stdout_bytes=100, max_stderr_bytes=100)
        self.assertEqual(result.returncode, 0)
        find.assert_called_once_with("docker", path=DOCKER_SEARCH_PATH)

    async def test_local_endpoint_and_empty_private_config_are_explicit(self):
        cli = DockerCLI(endpoint="unix:///Users/local/.docker/run/docker.sock", executable=str(self.binary))
        result = await cli.run(("inspect-client",), timeout_seconds=2, max_stdout_bytes=4096, max_stderr_bytes=100)
        report = json.loads(result.stdout)
        self.assertEqual(report["argv"][:2], ["--host", cli.endpoint])
        self.assertEqual(report["argv"][2], "--config")
        self.assertEqual(report["configFiles"], [])
        self.assertEqual(report["configMode"], 0o700)
        self.assertFalse(Path(report["argv"][3]).exists())

    async def test_client_environment_drops_home_context_proxies_keys_and_loaders(self):
        dangerous = {
            "HOME": "/private/home", "DOCKER_CONFIG": "/private/config", "DOCKER_HOST": "tcp://attacker:2375",
            "DOCKER_CONTEXT": "remote", "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/private/cert",
            "DOCKER_API_VERSION": "99", "HTTP_PROXY": "http://secret@proxy", "HTTPS_PROXY": "http://secret@proxy",
            "ALL_PROXY": "http://secret@proxy", "http_proxy": "http://secret@proxy", "NO_PROXY": "*",
            "OPENAI_API_KEY": "private-secret", "AWS_SECRET_ACCESS_KEY": "private-secret",
            "LD_PRELOAD": "/private/loader", "DYLD_INSERT_LIBRARIES": "/private/loader",
            "PYTHONPATH": "/private/project", "PATH": "/private/untrusted",
        }
        real_spawn = asyncio.create_subprocess_exec
        passed_environments = []

        async def record(*args, **kwargs):
            passed_environments.append(kwargs["env"])
            return await real_spawn(*args, **kwargs)

        with (
            patch.dict(os.environ, dangerous),
            patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=record),
        ):
            result = await self.run_cli(("inspect-client",))
        environment = json.loads(result.stdout)["environment"]
        # macOS's Python startup may add its own CoreFoundation encoding key;
        # it was not inherited or passed by the control CLI.
        environment.pop("__CF_USER_TEXT_ENCODING", None)
        self.assertEqual(passed_environments, [{"PATH": DOCKER_SEARCH_PATH, "LANG": "C", "LC_ALL": "C"}])
        self.assertEqual(environment, {"PATH": DOCKER_SEARCH_PATH, "LANG": "C", "LC_ALL": "C"})

    async def test_arguments_are_not_shell_interpolated_and_stdin_is_devnull(self):
        real_spawn = asyncio.create_subprocess_exec
        observed = []

        async def record(*args, **kwargs):
            observed.append((args, kwargs))
            return await real_spawn(*args, **kwargs)

        literal = "${OPENAI_API_KEY}; touch /tmp/should-never-exist `hostname`"
        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=record):
            result = await self.run_cli(("inspect-client", literal))
        report = json.loads(result.stdout)
        self.assertEqual(report["argv"][-1], literal)
        self.assertEqual(observed[0][1]["stdin"], asyncio.subprocess.DEVNULL)
        self.assertTrue(observed[0][1]["start_new_session"])
        self.assertNotIn("shell", observed[0][1])

    async def test_nonzero_result_retains_only_bounded_bytes_not_a_product_verdict(self):
        result = await self.run_cli(("exit", "7"))
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b"result-without-newline")
        self.assertEqual(result.stderr, b"sensitive-stderr")
        self.assertNotIn("sensitive-stderr", repr(result))
        self.assertNotIn("result-without-newline", repr(result))
        self.assertFalse(hasattr(result, "passed"))

    async def test_stdout_exact_cap_and_both_streams_are_supported(self):
        result = await self.run_cli(("both", "4096"), stdout=4096, stderr=4096)
        self.assertEqual(result.stdout, b"x" * 4096)
        self.assertEqual(result.stderr, b"y" * 4096)

    async def test_stdout_overflow_is_generic_and_kills_child(self):
        with self.assertRaises(SandboxError) as caught:
            await self.run_cli(("stdout", "65536"), stdout=16)
        self.assertEqual(caught.exception.code, SandboxErrorCode.OUTPUT_LIMIT)
        self.assertEqual(str(caught.exception), "SANDBOX_OUTPUT_LIMIT")

    async def test_stderr_overflow_does_not_leak_stderr(self):
        with self.assertRaises(SandboxError) as caught:
            await self.run_cli(("stderr", "65536"), stderr=16)
        self.assertEqual(caught.exception.code, SandboxErrorCode.OUTPUT_LIMIT)
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_timeout_reaps_control_process(self):
        processes = []
        real_spawn = asyncio.create_subprocess_exec

        async def record(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            return process

        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=record):
            with self.assertRaises(SandboxError) as caught:
                await self.run_cli(("sleep",), timeout=0.08)
        self.assertEqual(caught.exception.code, SandboxErrorCode.TIMEOUT)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)

    async def test_descendant_open_pipe_is_killed_on_timeout(self):
        started = time.monotonic()
        with self.assertRaises(SandboxError) as caught:
            await self.run_cli(("descendant-pipe",), timeout=0.08)
        self.assertEqual(caught.exception.code, SandboxErrorCode.TIMEOUT)
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_cancellation_waits_for_kill_and_reap(self):
        processes = []
        spawned = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec

        async def record(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            return process

        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=record):
            task = asyncio.create_task(self.run_cli(("sleep",)))
            await asyncio.wait_for(spawned.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)

    async def test_repeated_cancellation_cannot_abandon_cleanup(self):
        from orchestrator.sandbox import docker_cli

        spawned = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        processes = []
        real_spawn = asyncio.create_subprocess_exec
        real_reap = docker_cli._reap

        async def record(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            return process

        async def delayed_reap(process, tasks):
            cleanup_started.set()
            await release_cleanup.wait()
            await real_reap(process, tasks)

        with (
            patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=record),
            patch("orchestrator.sandbox.docker_cli._reap", side_effect=delayed_reap),
        ):
            task = asyncio.create_task(self.run_cli(("sleep",)))
            await asyncio.wait_for(spawned.wait(), 2)
            task.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 2)
            task.cancel()
            release_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertIsNotNone(processes[0].returncode)

    async def test_startup_cancellation_and_repeated_cancel_keep_process_ownership(self):
        process_created = asyncio.Event()
        release_handle = asyncio.Event()
        processes = []
        config_paths = []
        real_spawn = asyncio.create_subprocess_exec

        async def delayed_handle(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            config_paths.append(Path(args[4]))
            process_created.set()
            await release_handle.wait()
            return process

        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=delayed_handle):
            task = asyncio.create_task(self.run_cli(("sleep",)))
            await asyncio.wait_for(process_created.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "Do not abandon startup before acquiring its Process handle")
            release_handle.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
        self.assertFalse(config_paths[0].exists())

    async def test_startup_timeout_acquires_handle_before_reaping_and_removing_config(self):
        processes = []
        config_paths = []
        real_spawn = asyncio.create_subprocess_exec

        async def delayed_handle(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            config_paths.append(Path(args[4]))
            await asyncio.sleep(0.1)
            return process

        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=delayed_handle):
            with self.assertRaises(SandboxError) as caught:
                await asyncio.wait_for(self.run_cli(("sleep",), timeout=0.05), 2)
        self.assertEqual(caught.exception.code, SandboxErrorCode.TIMEOUT)
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(config_paths[0].exists())

    async def test_startup_time_consumes_the_same_timeout_budget(self):
        real_spawn = asyncio.create_subprocess_exec

        async def delayed_handle(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            await asyncio.sleep(0.12)
            return process

        started = time.monotonic()
        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=delayed_handle):
            with self.assertRaises(SandboxError) as caught:
                await self.run_cli(("sleep",), timeout=0.2)
        self.assertEqual(caught.exception.code, SandboxErrorCode.TIMEOUT)
        self.assertLess(time.monotonic() - started, 0.3)

    async def test_large_burst_paused_pipe_is_discarded_and_reaped_after_output_limit(self):
        from orchestrator.sandbox import docker_cli

        read_bounded = docker_cli._read_bounded

        async def delayed_reader(stream, maximum):
            # Let the child's burst fill and pause StreamReader transports.
            await asyncio.sleep(0.1)
            return await read_bounded(stream, maximum)

        with patch("orchestrator.sandbox.docker_cli._read_bounded", side_effect=delayed_reader):
            with self.assertRaises(SandboxError) as caught:
                await asyncio.wait_for(self.run_cli(("stdout", str(8 * 1024 * 1024)), stdout=16), 2)
        self.assertEqual(caught.exception.code, SandboxErrorCode.OUTPUT_LIMIT)

    async def test_invalid_arguments_and_limits_fail_before_subprocess(self):
        examples = (
            {"arguments": []}, {"arguments": ()}, {"arguments": ("a",) * 257},
            {"arguments": (None,)}, {"arguments": ("a\x00b",)}, {"arguments": ("a\nb",)},
            {"arguments": ("\ud800",)}, {"arguments": ("a" * 8193,)},
            {"arguments": ("a" * 8192,) * 17},
            {"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")}, {"timeout_seconds": 601},
            {"max_stdout_bytes": 0}, {"max_stdout_bytes": True}, {"max_stderr_bytes": 4 * 1024 * 1024 + 1},
            {"max_stderr_bytes": "100"},
        )
        for changes in examples:
            inputs = {"arguments": ("info",), "timeout_seconds": 1.0, "max_stdout_bytes": 100, "max_stderr_bytes": 100}
            inputs.update(changes)
            with (
                self.subTest(inputs=inputs),
                patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec") as spawn,
                self.assertRaises(SandboxError) as caught,
            ):
                await self.cli.run(**inputs)
            spawn.assert_not_called()
            self.assertEqual(caught.exception.code, SandboxErrorCode.INVALID)

    async def test_spawn_failure_hides_host_path(self):
        with patch("orchestrator.sandbox.docker_cli.asyncio.create_subprocess_exec", side_effect=PermissionError("/private/path/secret")):
            with self.assertRaises(SandboxError) as caught:
                await self.run_cli(("info",))
        self.assertEqual(caught.exception.code, SandboxErrorCode.UNAVAILABLE)
        self.assertNotIn("/private", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_stream_io_failure_is_sanitized_and_child_reaped(self):
        with patch("orchestrator.sandbox.docker_cli._read_bounded", side_effect=OSError("sensitive-stream-secret")):
            with self.assertRaises(SandboxError) as caught:
                await self.run_cli(("sleep",))
        self.assertEqual(caught.exception.code, SandboxErrorCode.EXECUTION)
        self.assertNotIn("sensitive", str(caught.exception))

    async def test_control_temporary_directory_failure_is_sanitized(self):
        with patch("orchestrator.sandbox.docker_cli.tempfile.TemporaryDirectory", side_effect=PermissionError("private-root-secret")):
            with self.assertRaises(SandboxError) as caught:
                await self.run_cli(("info",))
        self.assertEqual(caught.exception.code, SandboxErrorCode.EXECUTION)
        self.assertNotIn("private-root", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
