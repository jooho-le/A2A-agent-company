"""QA-only browser execution on an immutable Source, never Host product code."""

from dataclasses import replace
from functools import partial
import json
import math
from pathlib import Path
from types import MappingProxyType

from mcp_tools.runtime import MCPConfigurationError, MCPExecutionContext, MCPToolExecutionError
from mcp_tools.runtime import delegate_host_context
from mcp_tools.tools.browser_config import BrowserTestConfiguration
from mcp_tools.tools.browser_inputs import BrowserInputsError, prepare_browser_inputs
from mcp_tools.tools.browser_report import BrowserReportError, parse_browser_report
from mcp_tools.tools.browser_store import BrowserTestOutputStore, BrowserStoreError
from mcp_tools.tools.files import _run_file_operation
from orchestrator.artifacts.contracts import ArtifactAccessError
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


def _decode_stdout(value, exit_code):
    try:
        report = parse_browser_report(value.decode("utf-8", errors="strict"), exit_code)
        return json.dumps(report.to_dict(), ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except UnicodeError:
        raise MCPToolExecutionError("TEST_RUNNER_ERROR") from None
    except BrowserReportError as error:
        if error.code == "BROWSER_START_FAILED":
            # Runtime retains only safe Sandbox codes and always cleans up.
            raise SandboxError(SandboxErrorCode.CONFIGURATION) from None
        raise MCPToolExecutionError("TEST_RUNNER_ERROR") from None


class BrowserTestTools:
    def __init__(self, artifact_store, sandbox, output_store, *, configuration=None, max_call_seconds=60):
        if (
            not isinstance(artifact_store, ArtifactStore)
            or not isinstance(sandbox, SandboxRuntime)
            or not isinstance(output_store, BrowserTestOutputStore)
            or configuration is not None and not isinstance(configuration, BrowserTestConfiguration)
            or isinstance(max_call_seconds, bool) or not isinstance(max_call_seconds, (float, int))
            or not math.isfinite(max_call_seconds) or not 0 < max_call_seconds <= 600
        ):
            raise MCPConfigurationError()
        self._artifacts, self._sandbox, self._outputs = artifact_store, sandbox, output_store
        self._configuration, self._max_call_seconds = configuration, float(max_call_seconds)

    def __repr__(self):
        return "BrowserTestTools()"

    def handlers(self, role):
        if not isinstance(role, AgentRole):
            raise MCPConfigurationError()
        return MappingProxyType({"run_browser_tests": self.run_browser_tests,
                                 "read_test_report": self.read_test_report} if role is AgentRole.QA else {})

    @staticmethod
    def _context(context):
        if (
            not isinstance(context, MCPExecutionContext) or context.binding.role is not AgentRole.QA
            or context.workspace.role is not AgentRole.QA or context.workspace.run_id != context.binding.run_id
            or context.workspace.workspace_id != context.binding.workspace_id
        ):
            raise MCPToolExecutionError("PERMISSION_DENIED")

    def _suite_profile(self, name):
        if self._configuration is None:
            raise MCPToolExecutionError("BROWSER_START_FAILED")
        suite = next((item for item in self._configuration.suites if item.name == name), None)
        if suite is None:
            raise MCPToolExecutionError("TEST_RUNNER_ERROR")
        limits = self._configuration.limits
        timeout = min(limits.timeout_seconds, self._max_call_seconds - 2 * limits.control_timeout_seconds - 1)
        if timeout < 0.01:
            raise MCPToolExecutionError("TIMEOUT")
        return suite, ExecutionProfile(
            name=suite.name, tool_name="run_browser_tests",
            argv=(self._configuration.python_executable, "-I", "-B", "/inputs/_browser_runner.py"),
            limits=replace(limits, timeout_seconds=timeout), image_reference=self._configuration.image_reference,
        )

    def _prepare(self, context, suite, source_id, profile):
        source = self._artifacts.bind(context.binding.run_id, role=AgentRole.QA).read(source_id).metadata
        if not isinstance(source, CodeSnapshotArtifact) or source.artifact_id != source_id:
            raise MCPToolExecutionError("TEST_RUNNER_ERROR")
        _, frozen_configuration, _, _ = self._sandbox._context(context.binding.run_id, AgentRole.QA, source_id, profile)
        if suite.kind == "PROTECTED" and suite.protected_suite_ref != frozen_configuration.configuration.protected_test_suite_ref:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        asset = Path(__file__)
        return source, prepare_browser_inputs(
            context, suite, self._configuration,
            asset.with_name("browser_runner.py").read_bytes(), asset.with_name("browser_contract.py").read_bytes(),
        )

    async def run_browser_tests(self, context, arguments):
        self._context(context)
        suite, profile = self._suite_profile(arguments["testSuite"])
        try:
            source_id = workspace_uuid(arguments["snapshotId"])
            source, inputs = await _run_file_operation(self._prepare, context, suite, source_id, profile)
            result = await self._sandbox.bind(context.binding.run_id, role=AgentRole.QA).run(
                source_id, profile, inputs=inputs.files, stdout_decoder=_decode_stdout,
            )
            report = parse_browser_report(result.stdout, result.exit_code)
            record = await _run_file_operation(partial(self._outputs.publish, context.binding, source, result,
                profile=profile, suite=suite, inputs=inputs, report=report, configuration=self._configuration))
            return record.tool_output()
        except BrowserInputsError as error:
            raise MCPToolExecutionError(error.code) from None
        except SandboxError as error:
            code = {SandboxErrorCode.TIMEOUT: "TIMEOUT", SandboxErrorCode.DENIED: "PERMISSION_DENIED",
                    SandboxErrorCode.PATH: "PATH_DENIED", SandboxErrorCode.CONFIGURATION: "BROWSER_START_FAILED",
                    SandboxErrorCode.UNAVAILABLE: "BROWSER_START_FAILED", SandboxErrorCode.IMAGE: "BROWSER_START_FAILED"}.get(
                        error.code, "TEST_RUNNER_ERROR")
            raise MCPToolExecutionError(code) from None
        except BrowserReportError as error:
            raise MCPToolExecutionError(error.code) from None
        except (ArtifactAccessError, WorkspaceAccessError, BrowserStoreError, OSError):
            raise MCPToolExecutionError("TEST_RUNNER_ERROR") from None

    async def read_test_report(self, context, arguments):
        context = delegate_host_context(context, "read_test_report")
        self._context(context)
        try:
            result = await _run_file_operation(self._outputs.read_report, context.binding, arguments["reportRef"])
            return {"testResult": result}
        except BrowserStoreError as error:
            code = {"BROWSER_TEST_RECORD_NOT_FOUND": "REPORT_NOT_FOUND",
                    "BROWSER_TEST_RESULT_INVALID": "PATH_DENIED"}.get(error.code, "PERMISSION_DENIED")
            raise MCPToolExecutionError(code) from None
