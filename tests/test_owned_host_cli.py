"""Step 41 explicit Host CLI: inert Git checks, no listeners or external APIs."""

from contextlib import redirect_stderr, redirect_stdout
import asyncio
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from agents.platform import run_host as cli
from agents.platform.host import HostFactoryError
from agents.platform.runner import PlatformRunnerError
from test_agent_platform_runner import platform_fixture
from test_owned_host import _HostFixture, _CONFIG_INVALID, _INIT_FAILED, _SECRET


class OwnedHostCLITests(_HostFixture, unittest.TestCase):
    def argv(self, *extra):
        return ["--runtime", str(self.runtime_file), "--tools", str(self.tools_file),
                "--source", str(self.source_file), *extra]

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = cli.main(arguments)
        return result, output.getvalue(), errors.getvalue()

    def assert_private(self, output):
        self.assertNotIn(str(self.directory), output)
        self.assertNotIn(_SECRET, output)
        for secret in self.environment.values():
            self.assertNotIn(secret, output)

    def test_default_is_read_only_combined_check_without_credentials_or_host_creation(self):
        self.assertEqual(dict(os.environ), {})
        with patch.object(cli, "create_host", new_callable=AsyncMock) as created, \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv())
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        self.assertIs(json.loads(output)["executionReady"], False)
        created.assert_not_called()
        runner.assert_not_called()
        self.assertFalse((self.directory / ".data").exists())
        self.assert_private(output)

    def test_explicit_secret_check_stays_read_only_and_never_starts_platform(self):
        with patch.dict(os.environ, self.environment), \
                patch.object(cli, "create_host", new_callable=AsyncMock) as created, \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--check-secrets"))
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        self.assertIs(json.loads(output)["executionReady"], False)
        self.assertFalse((self.directory / ".data").exists())
        created.assert_not_called()
        runner.assert_not_called()
        self.assert_private(output)

    def test_missing_requested_secrets_is_safe_failure_without_side_effects(self):
        with patch.object(cli, "create_host", new_callable=AsyncMock) as created, \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--check-secrets"))
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn(errors.strip(), {_CONFIG_INVALID, _INIT_FAILED})
        self.assert_private(errors)
        created.assert_not_called()
        runner.assert_not_called()
        self.assertFalse((self.directory / ".data").exists())

    def test_invalid_source_is_safe_offline_failure(self):
        self.source["snapshotSha256"] = "f" * 64
        self.write()
        result, output, errors = self.invoke(self.argv())
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertEqual(errors.strip(), _CONFIG_INVALID)
        self.assert_private(errors)
        self.assertFalse((self.directory / ".data").exists())

    def test_missing_options_and_unknown_option_values_are_not_echoed(self):
        for arguments in ([], ["--runtime", _SECRET], self.argv("--unknown-" + _SECRET)):
            with self.subTest(arguments_count=len(arguments)), \
                    patch.object(cli, "create_host", new_callable=AsyncMock) as created, \
                    patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
                result, output, errors = self.invoke(arguments)
            self.assertEqual(result, 1)
            self.assertEqual(output, "")
            self.assertEqual(errors.strip(), _CONFIG_INVALID)
            self.assert_private(errors)
            created.assert_not_called()
            runner.assert_not_called()

    def test_cli_timeout_validation_precedes_providers_database_and_listener_creation(self):
        for option, value in (("--startup-timeout", "nan"), ("--startup-timeout", "inf"),
                              ("--startup-timeout", "0"), ("--startup-timeout", "300.01"),
                              ("--shutdown-timeout", "nan"), ("--shutdown-timeout", "-1"),
                              ("--shutdown-timeout", "600.01"), ("--shutdown-timeout", _SECRET)):
            with self.subTest(option=option, value=value), \
                    patch.object(cli, "create_host", new_callable=AsyncMock) as created, \
                    patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
                result, output, errors = self.invoke(self.argv("--start", option, value))
            self.assertEqual(result, 1)
            self.assertEqual(output, "")
            self.assert_private(errors)
            created.assert_not_called()
            runner.assert_not_called()
        self.assertFalse((self.directory / ".data").exists())

    def test_explicit_start_passes_exact_files_cases_and_runner_timeouts(self):
        platform, _ = platform_fixture()
        with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform) as created, \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--start", "--qa-cases", str(self.cases_file),
                "--startup-timeout", "12.5", "--shutdown-timeout", "25"))
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        self.assert_private(output)
        created.assert_awaited_once()
        actual = created.await_args.kwargs
        for name, expected in (("runtime_file", self.runtime_file), ("tools_file", self.tools_file),
                               ("source_file", self.source_file), ("protected_cases_file", self.cases_file)):
            self.assertEqual(Path(actual[name]), expected)
        runner.assert_awaited_once_with(platform, startup_timeout_seconds=12.5, shutdown_timeout_seconds=25.0)

    def test_default_start_uses_owned_runner_default_deadlines(self):
        platform, _ = platform_fixture()
        with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform), \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual((result, errors), (0, ""))
        self.assert_private(output)
        runner.assert_awaited_once_with(platform, startup_timeout_seconds=30.0, shutdown_timeout_seconds=60.0)

    def test_host_initialization_failure_does_not_start_listeners_or_leak(self):
        with patch.object(cli, "create_host", new_callable=AsyncMock, side_effect=HostFactoryError(_INIT_FAILED)), \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual((result, output, errors.strip()), (1, "", _INIT_FAILED))
        runner.assert_not_called()
        self.assert_private(errors)

    def test_typed_runner_failure_is_stable_and_private(self):
        platform, _ = platform_fixture()
        with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform), \
                patch.object(cli, "run_platform", new_callable=AsyncMock,
                             side_effect=PlatformRunnerError("PLATFORM_BIND_FAILED")):
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual((result, output, errors.strip()), (1, "", "PLATFORM_BIND_FAILED"))
        self.assert_private(errors)

    def test_unexpected_exception_is_replaced_with_safe_generic_reason(self):
        with patch.object(cli, "create_host", new_callable=AsyncMock,
                          side_effect=RuntimeError(_SECRET + " " + str(self.directory))), \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertEqual(errors.strip(), _INIT_FAILED)
        self.assert_private(errors)
        runner.assert_not_called()

    def test_canceled_host_initialization_returns_interrupt_exit_without_running_listeners(self):
        with patch.object(cli, "create_host", new_callable=AsyncMock, side_effect=asyncio.CancelledError()), \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner:
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual((result, output, errors), (130, "", ""))
        runner.assert_not_called()

    def test_canceled_runner_drains_provider_once_and_preserves_interrupt_even_if_close_fails(self):
        for close_fails in (False, True):
            with self.subTest(close_fails=close_fails):
                platform, provider = platform_fixture(provider_fails=close_fails)
                with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform), \
                        patch.object(cli, "run_platform", new_callable=AsyncMock, side_effect=asyncio.CancelledError()):
                    result, output, errors = self.invoke(self.argv("--start"))
                self.assertEqual((result, output, errors), (130, "", ""))
                self.assertEqual(provider.calls, 1)

    def test_cleanup_failure_does_not_replace_original_typed_runner_failure(self):
        platform, provider = platform_fixture(provider_fails=True)
        with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform), \
                patch.object(cli, "run_platform", new_callable=AsyncMock,
                             side_effect=PlatformRunnerError("PLATFORM_BIND_FAILED")):
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual((result, output, errors.strip()), (1, "", "PLATFORM_BIND_FAILED"))
        self.assertEqual(provider.calls, 1)
        self.assert_private(errors)

    def test_cleanup_failure_after_successful_runner_maps_to_owned_cleanup_code(self):
        platform, provider = platform_fixture(provider_fails=True)
        with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform), \
                patch.object(cli, "run_platform", new_callable=AsyncMock):
            result, output, errors = self.invoke(self.argv("--start"))
        self.assertEqual((result, output, errors.strip()), (1, "", "PLATFORM_CLEANUP_FAILED"))
        self.assertEqual(provider.calls, 1)
        self.assert_private(errors)
