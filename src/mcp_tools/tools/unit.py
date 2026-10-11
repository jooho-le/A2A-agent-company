"""Host-approved unittest scopes, actual Snapshot Sandbox, immutable reports."""

from dataclasses import replace
from functools import partial
import json
import math
from pathlib import Path
from types import MappingProxyType

from mcp_tools.runtime import MCPConfigurationError, MCPExecutionContext, MCPToolExecutionError
from mcp_tools.runtime import delegate_host_context
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.tools.unit_config import UnitTestConfiguration
from mcp_tools.tools.unit_inputs import UnitTestInputsError, prepare_unit_inputs
from mcp_tools.tools.unit_report import UnitReportError, parse_unit_report
from mcp_tools.tools.unit_store import UnitTestOutputStore, UnitTestStoreError
from orchestrator.artifacts.contracts import ArtifactAccessError
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


def _decode_stdout(value, exit_code):
    # Strict UTF-8 + exactly one runner report; no prose/count guessing.
    try:
        text = value.decode("utf-8", errors="strict")
        report = parse_unit_report(text, exit_code)
        return json.dumps(report.to_dict(), ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (UnicodeError, UnitReportError):
        raise MCPToolExecutionError("TEST_RUNNER_ERROR") from None


class UnitTestTools:
    def __init__(self, artifact_store, sandbox, output_store, *, configuration=None, max_call_seconds=60):
        if (
            not isinstance(artifact_store, ArtifactStore)
            or not isinstance(sandbox, SandboxRuntime)
            or not isinstance(output_store, UnitTestOutputStore)
            or configuration is not None and not isinstance(configuration, UnitTestConfiguration)
            or isinstance(max_call_seconds, bool) or not isinstance(max_call_seconds, (float, int))
            or not math.isfinite(max_call_seconds) or not 0 < max_call_seconds <= 600
        ):
            raise MCPConfigurationError()
        self._artifacts, self._sandbox, self._outputs = artifact_store, sandbox, output_store
        self._configuration, self._max_call_seconds = configuration, float(max_call_seconds)

    def __repr__(self):
        return "UnitTestTools()"

    def handlers(self, role):
        if not isinstance(role, AgentRole):
            raise MCPConfigurationError()
        result = {"run_unit_tests": self.run_unit_tests} if role in (AgentRole.DEVELOPER, AgentRole.QA) else {}
        if role is AgentRole.QA:
            result["read_test_report"] = self.read_test_report
        return MappingProxyType(result)

    @staticmethod
    def _context(context, roles):
        if (
            not isinstance(context, MCPExecutionContext) or context.binding.role not in roles
            or context.workspace.role is not context.binding.role
            or context.workspace.run_id != context.binding.run_id
            or context.workspace.workspace_id != context.binding.workspace_id
        ):
            raise MCPToolExecutionError("PERMISSION_DENIED")

    def _scope_profile(self, role, name):
        if self._configuration is None:
            raise MCPToolExecutionError("TEST_RUNNER_ERROR")
        scope = next((item for item in self._configuration.scopes if item.name == name), None)
        if scope is None:
            raise MCPToolExecutionError("TEST_RUNNER_ERROR")
        if role not in scope.roles:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        limits = self._configuration.limits
        timeout = min(limits.timeout_seconds, self._max_call_seconds - 2 * limits.control_timeout_seconds - 1)
        if timeout < 0.01:
            raise MCPToolExecutionError("TEST_RUNNER_ERROR")
        return scope, ExecutionProfile(
            name=scope.name, tool_name="run_unit_tests",
            argv=(self._configuration.python_executable, "-I", "-B", "/inputs/_unit_runner.py",
                  "--kind", scope.kind, "--directory", scope.source_directory, "--pattern", scope.pattern),
            limits=replace(limits, timeout_seconds=timeout), image_reference=self._configuration.image_reference,
        )

    def _prepare(self, context, scope, source_id):
        source = self._artifacts.bind(context.binding.run_id, role=context.binding.role).read(source_id).metadata
        if not isinstance(source, CodeSnapshotArtifact) or source.artifact_id != source_id:
            raise MCPToolExecutionError("TEST_RUNNER_ERROR")
        # Reject invalid/stale context and missing protected baseline before
        # filesystem capture or Docker discovery, not only after execution.
        _, frozen_configuration, _, _ = self._sandbox._context(
            context.binding.run_id, context.binding.role, source_id,
            self._scope_profile(context.binding.role, scope.name)[1],
        )
        if scope.kind == "PROTECTED" and scope.protected_suite_ref != frozen_configuration.configuration.protected_test_suite_ref:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        # This is the platform's trusted runner asset, not project Source or a
        # model-selected Host path. It is loaded only for an explicit Tool call.
        runner_source = Path(__file__).with_name("unit_runner.py").read_bytes()
        return source, prepare_unit_inputs(context, scope, runner_source)

    async def run_unit_tests(self, context, arguments):
        self._context(context, (AgentRole.DEVELOPER, AgentRole.QA))
        scope, profile = self._scope_profile(context.binding.role, arguments["testScope"])
        try:
            source_id = workspace_uuid(arguments["snapshotId"])
            source, inputs = await _run_file_operation(self._prepare, context, scope, source_id)
            result = await self._sandbox.bind(context.binding.run_id, role=context.binding.role).run(
                source_id, profile, inputs=inputs.files, stdout_decoder=_decode_stdout,
            )
            report = parse_unit_report(result.stdout, result.exit_code)
            record = await _run_file_operation(partial(
                self._outputs.publish, context.binding, source, result,
                profile=profile, scope=scope, inputs=inputs, report=report,
            ))
            return record.tool_output()
        except UnitTestInputsError as error:
            raise MCPToolExecutionError(error.code) from None
        except SandboxError as error:
            code = {SandboxErrorCode.TIMEOUT: "TIMEOUT", SandboxErrorCode.DENIED: "PERMISSION_DENIED",
                    SandboxErrorCode.PATH: "PATH_DENIED"}.get(error.code, "TEST_RUNNER_ERROR")
            raise MCPToolExecutionError(code) from None
        except (ArtifactAccessError, WorkspaceAccessError, UnitReportError, UnitTestStoreError, OSError):
            raise MCPToolExecutionError("TEST_RUNNER_ERROR") from None

    async def read_test_report(self, context, arguments):
        context = delegate_host_context(context, "read_test_report")
        self._context(context, (AgentRole.QA,))
        try:
            result = await _run_file_operation(self._outputs.read_report, context.binding, arguments["reportRef"])
            return {"testResult": result}
        except UnitTestStoreError as error:
            code = {"UNIT_TEST_RECORD_NOT_FOUND": "REPORT_NOT_FOUND",
                    "UNIT_TEST_RESULT_INVALID": "PATH_DENIED"}.get(error.code, "PERMISSION_DENIED")
            raise MCPToolExecutionError(code) from None
