"""Security-only static scanning of frozen Source; no Host product execution."""

from dataclasses import replace
from functools import partial
import math
from pathlib import Path
from types import MappingProxyType

from mcp_tools.runtime import MCPConfigurationError, MCPExecutionContext, MCPToolExecutionError
from mcp_tools.runtime import delegate_host_context
from mcp_tools.core.policy import MCPHostPrincipal
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.tools.security_config import SecurityScanConfiguration
from mcp_tools.tools.security_contract import canonical_json
from mcp_tools.tools.security_inputs import SecurityInputsError, prepare_security_inputs
from mcp_tools.tools.security_report import SecurityReportError, parse_security_report
from mcp_tools.tools.security_store import SecurityScanOutputStore, SecurityStoreError
from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


def _decode_stdout(value, exit_code):
    # Keep only the closed, privacy-safe receipt. Never retain Bandit prose,
    # code snippets, issue text, Source paths outside the Snapshot or secrets.
    try:
        report = parse_security_report(value.decode("utf-8", errors="strict"), exit_code)
        return canonical_json(report.to_dict())
    except (UnicodeError, SecurityReportError, ValueError):
        raise MCPToolExecutionError("SCANNER_ERROR") from None


class SecurityScanTools:
    def __init__(self, artifact_store, sandbox, output_store, *, configuration=None, max_call_seconds=60):
        if (
            not isinstance(artifact_store, ArtifactStore)
            or not isinstance(sandbox, SandboxRuntime)
            or not isinstance(output_store, SecurityScanOutputStore)
            or configuration is not None and not isinstance(configuration, SecurityScanConfiguration)
            or isinstance(max_call_seconds, bool) or not isinstance(max_call_seconds, (float, int))
            or not math.isfinite(max_call_seconds) or not 0 < max_call_seconds <= 600
        ):
            raise MCPConfigurationError()
        self._artifacts, self._sandbox, self._outputs = artifact_store, sandbox, output_store
        self._configuration, self._max_call_seconds = configuration, float(max_call_seconds)

    def __repr__(self):
        return "SecurityScanTools()"

    def handlers(self, role):
        if not isinstance(role, (AgentRole, MCPHostPrincipal)):
            raise MCPConfigurationError()
        if role is MCPHostPrincipal.ORCHESTRATOR:
            return MappingProxyType({"read_security_report": self.read_security_report})
        return MappingProxyType({"run_security_scan": self.run_security_scan,
                                 "read_security_report": self.read_security_report}
                                if role is AgentRole.SECURITY else {})

    @staticmethod
    def _context(context):
        if (
            not isinstance(context, MCPExecutionContext) or context.binding.role is not AgentRole.SECURITY
            or context.workspace.role is not AgentRole.SECURITY
            or context.workspace.run_id != context.binding.run_id
            or context.workspace.workspace_id != context.binding.workspace_id
        ):
            raise MCPToolExecutionError("PERMISSION_DENIED")

    def _scanner_profile(self, name):
        if self._configuration is None:
            raise MCPToolExecutionError("PROFILE_NOT_FOUND")
        scanner = next((item for item in self._configuration.profiles if item.name == name), None)
        if scanner is None:
            raise MCPToolExecutionError("PROFILE_NOT_FOUND")
        limits = self._configuration.limits
        timeout = min(limits.timeout_seconds, self._max_call_seconds - 2 * limits.control_timeout_seconds - 1)
        if timeout < 0.01:
            raise MCPToolExecutionError("TIMEOUT")
        return scanner, ExecutionProfile(
            name=scanner.name, tool_name="run_security_scan",
            argv=(self._configuration.python_executable, "-I", "-B", "/inputs/_security_runner.py"),
            limits=replace(limits, timeout_seconds=timeout), image_reference=self._configuration.image_reference,
        )

    def _prepare(self, context, scanner, source_id, profile):
        record = self._artifacts.bind(context.binding.run_id, role=AgentRole.SECURITY).read(source_id)
        source = record.metadata
        if not isinstance(source, CodeSnapshotArtifact) or source.artifact_id != source_id:
            raise MCPToolExecutionError("SCANNER_ERROR")
        _, frozen_configuration, _, _ = self._sandbox._context(
            context.binding.run_id, AgentRole.SECURITY, source_id, profile,
        )
        if scanner.profile_ref != frozen_configuration.configuration.scanner_profile_ref:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        # Validate the complete canonical archive without extraction or Python
        # imports. A Snapshot without Python is unsupported, not a clean scan.
        self._outputs.source_inventory(record.content)
        asset = Path(__file__)
        return source, prepare_security_inputs(
            self._configuration, scanner, asset.with_name("security_runner.py").read_bytes(),
            asset.with_name("security_contract.py").read_bytes(),
        )

    async def run_security_scan(self, context, arguments):
        self._context(context)
        scanner, profile = self._scanner_profile(arguments["scannerProfile"])
        try:
            source_id = workspace_uuid(arguments["snapshotId"])
            source, inputs = await _run_file_operation(self._prepare, context, scanner, source_id, profile)
            result = await self._sandbox.bind(context.binding.run_id, role=AgentRole.SECURITY).run(
                source_id, profile, inputs=inputs.files, stdout_decoder=_decode_stdout,
            )
            report = parse_security_report(result.stdout, result.exit_code)
            record = await _run_file_operation(partial(
                self._outputs.publish, context.binding, source, result,
                profile=profile, scanner_profile=scanner, inputs=inputs, report=report,
                configuration=self._configuration,
            ))
            return record.tool_output()
        except SecurityInputsError as error:
            raise MCPToolExecutionError(error.code) from None
        except SandboxError as error:
            code = {SandboxErrorCode.TIMEOUT: "TIMEOUT", SandboxErrorCode.DENIED: "PERMISSION_DENIED",
                    SandboxErrorCode.PATH: "PATH_DENIED"}.get(error.code, "SCANNER_ERROR")
            raise MCPToolExecutionError(code) from None
        except ArtifactAccessError as error:
            code = "PERMISSION_DENIED" if error.code in {ArtifactErrorCode.DENIED, ArtifactErrorCode.PATH_DENIED} else "SCANNER_ERROR"
            raise MCPToolExecutionError(code) from None
        except (WorkspaceAccessError, SecurityReportError, SecurityStoreError, OSError):
            raise MCPToolExecutionError("SCANNER_ERROR") from None

    async def read_security_report(self, context, arguments):
        context = delegate_host_context(context, "read_security_report")
        self._context(context)
        try:
            result = await _run_file_operation(self._outputs.read_report, context.binding, arguments["reportRef"])
            return {"securityResult": result}
        except SecurityStoreError as error:
            code = {"SECURITY_SCAN_RECORD_NOT_FOUND": "REPORT_NOT_FOUND",
                    "SECURITY_SCAN_RESULT_INVALID": "PATH_DENIED"}.get(error.code, "PERMISSION_DENIED")
            raise MCPToolExecutionError(code) from None
