"""Host-measured static scans and conservatively grounded Security reports.

Static warnings and model prose are proposals, not proof of vulnerabilities or
Requirement satisfaction. Only an explicitly configured trusted Host verifier
may upgrade grounded proposals. Nothing executes project Source on the Host.

When the Host configures Security unit-test scopes (the same unit-test path QA
uses), Requirement outcomes come instead from measured Sandbox test receipts:
the model writes and binds its own tests, every configured protected Security
case is always run, and a Requirement is PASS only when all its bound tests
pass. A proof verifier and test scopes are never combined for one Run.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
from types import MappingProxyType
from urllib.parse import quote
from uuid import UUID, uuid4

from a2a.helpers import new_data_part
from a2a.types import Artifact, Task, TaskStatus, TaskState
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, json_text, parse_json
from agents.roles.qa_contract import QACaseBinding
from agents.roles.outputs import validate_completed_role_output
from agents.runtime.qa_context import validation_cycle, verify_report_predecessor
from agents.runtime.evaluation_policy_store import EvaluationPolicyStore, security_policy_sha256
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, open_mcp_client
from mcp_tools.execution_policy import arguments_sha256
from mcp_tools.execution_runtime import TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore, _digest
from mcp_tools.runtime import MCPExecutionContext
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_store import UnitTestOutputStore
from mcp_tools.tools.snapshots import SnapshotReader
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.validation_artifacts import (
    FindingDisposition, SecurityFinding, SecurityReportArtifact, SecurityRequirementResult,
    SecuritySeverity, ValidationOutcome,
)
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry


class SecurityServicesError(ValueError):
    def __init__(self, code="SECURITY_SERVICES_INVALID"):
        self.code = code if code in {
            "SECURITY_SERVICES_INVALID", "SECURITY_SCAN_EVIDENCE_INVALID",
            "SECURITY_CODE_REFERENCE_INVALID", "SECURITY_REVIEW_INVALID",
            "SECURITY_PROOF_INVALID", "SECURITY_ARTIFACT_INVALID",
        } else "SECURITY_SERVICES_INVALID"
        super().__init__(self.code)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _uuid(value):
    return type(value) is UUID and value.version == 4


@dataclass(frozen=True, kw_only=True)
class SecuritySemanticProof:
    """A typed result of an independent, trusted Host semantic verifier.

    This is not a model-callable Tool, signature, attestation, or default
    SCN-001 validator. Scope checks do not make an untrusted verifier honest.
    """

    run_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    snapshot_sha256: str = field(repr=False)
    requirement_outcomes: tuple = field(default=(), repr=False)
    finding_dispositions: tuple = field(default=(), repr=False)
    verification_receipt_id: UUID | None = field(default=None, repr=False)
    approval_sha256: str | None = field(default=None, repr=False)
    request_sha256: str | None = field(default=None, repr=False)

    def __post_init__(self):
        invalid = False
        try:
            if (not all(_uuid(value) for value in (self.run_id, self.workflow_step_id, self.source_artifact_id))
                    or type(self.snapshot_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256) is None
                    or type(self.requirement_outcomes) is not tuple or len(self.requirement_outcomes) > 256
                    or type(self.finding_dispositions) is not tuple or len(self.finding_dispositions) > 1000):
                raise ValueError
            for pair in self.requirement_outcomes:
                if (type(pair) is not tuple or len(pair) != 2 or not _uuid(pair[0])
                        or type(pair[1]) is not str or pair[1] not in {"PASS", "FAIL", "UNVERIFIED"}):
                    raise ValueError
            for pair in self.finding_dispositions:
                if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                        or len(pair[0]) > 512 or not pair[0] or type(pair[1]) is not str
                        or pair[1] not in {"CONFIRMED", "FALSE_POSITIVE", "SUSPECTED", "UNVERIFIED"}):
                    raise ValueError
            if (len(dict(self.requirement_outcomes)) != len(self.requirement_outcomes)
                    or len(dict(self.finding_dispositions)) != len(self.finding_dispositions)):
                raise ValueError
            receipt_fields = (self.verification_receipt_id, self.approval_sha256, self.request_sha256)
            if any(value is not None for value in receipt_fields) and (
                    not _uuid(self.verification_receipt_id)
                    or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                           for value in (self.approval_sha256, self.request_sha256))):
                raise ValueError
        except Exception:
            invalid = True
        if invalid:
            raise SecurityServicesError("SECURITY_PROOF_INVALID") from None


@dataclass(frozen=True, kw_only=True)
class SecurityMeasuredBundle:
    run_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    snapshot_sha256: str = field(repr=False)
    source_files: Mapping = field(repr=False)
    scans: tuple = field(repr=False)
    findings: tuple = field(repr=False)
    _analysis_json: str = field(repr=False)
    _token: UUID = field(repr=False)

    @property
    def analysis_input(self):
        return parse_json(self._analysis_json)

    @property
    def finding_ids(self):
        return tuple(identity for identity, _scanner, _finding, _receipt in self.findings)

    @property
    def source_paths(self):
        return tuple(self.source_files)


@dataclass(frozen=True, kw_only=True)
class _ReadProof:
    path: str = field(repr=False)
    content_sha256: str = field(repr=False)
    logical_call_id: UUID = field(repr=False)
    output_sha256: str = field(repr=False)
    requested_path: str = field(repr=False)


def _schemas():
    root = Path(__file__).resolve().parents[3] / "schemas" / "project"
    result, resources = {}, []
    for name in ("security_report.schema.json", "execution_manifest.schema.json", "tool_execution_evidence.schema.json"):
        schema = parse_json((root / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        result[name] = schema
        resource = Resource.from_contents(schema)
        resources.extend(((schema["$id"], resource),
                          ("https://a2a-agent-company.local/schemas/project/" + name, resource)))
    return result, Registry().with_resources(resources)


class SecurityRuntimeServices:
    """One opt-in Host Run capability; its constructor performs no I/O."""

    def __init__(self, repository, workspace_registry, artifact_store, *, mcp_configuration,
                 client_factory=open_mcp_client, proof_verifier=None, protected_cases=()):
        try:
            if (not isinstance(repository, SQLiteWorkflowRepository)
                    or not isinstance(workspace_registry, WorkspaceRegistry)
                    or not isinstance(artifact_store, ArtifactStore)
                    or type(mcp_configuration) is not MCPChildConfiguration
                    or workspace_registry._repository is not repository
                    or artifact_store._repository is not repository
                    or artifact_store._workspaces is not workspace_registry
                    or mcp_configuration.database_path != repository.database_path
                    or mcp_configuration.workspace_root != workspace_registry.base_path
                    or mcp_configuration.binding.role is not AgentRole.SECURITY
                    or mcp_configuration.binding.agent_role is not AgentRole.SECURITY
                    or mcp_configuration.frozen_source is None
                    or mcp_configuration.security_scan_configuration is None
                    or mcp_configuration.build_configuration is not None
                    or mcp_configuration.browser_test_configuration is not None
                    or not callable(client_factory) or proof_verifier is not None and not callable(proof_verifier)
                    or type(protected_cases) is not tuple or len(protected_cases) > 256
                    or any(type(case) is not QACaseBinding for case in protected_cases)):
                raise ValueError
            policy = mcp_configuration.security_scan_configuration
            timeout = min(policy.limits.timeout_seconds,
                mcp_configuration.max_call_seconds - 2 * policy.limits.control_timeout_seconds - 1)
            if timeout < .01:
                raise ValueError
            generated, protected = (), ()
            unit = mcp_configuration.unit_test_configuration
            if unit is not None:
                timeout = min(unit.limits.timeout_seconds,
                    mcp_configuration.max_call_seconds - 2 * unit.limits.control_timeout_seconds - 1)
                if (proof_verifier is not None or timeout < .01
                        or any(AgentRole.SECURITY not in scope.roles for scope in unit.scopes)):
                    raise ValueError
                generated = tuple(scope.name for scope in unit.scopes if scope.kind == "QA_TESTS")
                protected = tuple(scope.name for scope in unit.scopes if scope.kind == "PROTECTED")
                if not generated:
                    raise ValueError
            keys = [(case.tool_name, case.selector, case.test_id) for case in protected_cases]
            if (len(set(keys)) != len(keys)
                    or {(case.tool_name, case.selector) for case in protected_cases}
                       != {("run_unit_tests", name) for name in protected}):
                raise ValueError
            approved = tuple(QACaseBinding(**{name: getattr(case, name) for name in (
                "tool_name", "selector", "test_id", "requirement_id", "title", "expected_result")})
                for case in protected_cases)
        except Exception:
            raise SecurityServicesError() from None
        self.configuration, self.client_factory = mcp_configuration, client_factory
        self._repository, self._workspaces, self._artifacts = repository, workspace_registry, artifact_store
        self._tools, self._scans = ToolExecutionStore(repository), SecurityScanOutputStore(repository)
        self._evaluation_policies = EvaluationPolicyStore(repository)
        self._verifier = proof_verifier
        self._issued, self._reads = {}, {}
        self._scan_started = False
        self._unit = UnitTestOutputStore(repository)
        self._test_selectors, self._protected_selectors, self._protected_cases = generated, protected, approved

    def __repr__(self):
        return "SecurityRuntimeServices()"

    @property
    def test_selectors(self):
        """Generated Security test scopes offered to the model; empty means the proof path."""
        return self._test_selectors

    def _verify(self, execution):
        binding, source = self.configuration.binding, execution.source
        if (binding.run_id != execution.metadata.run_id
                or binding.workspace_id != execution.configuration.workspace_id
                or self.configuration.frozen_source.project_artifact_id != source.artifact_id
                or self.configuration.frozen_source.snapshot_sha256 != source.snapshot_sha256
                or source.run_id != binding.run_id
                or execution.metadata.code_version != source.code_version):
            raise ValueError
        run = self._repository.get_run(binding.run_id)
        configuration = self._repository.get_run_configuration(binding.run_id)
        active = [step for step in self._repository.list_steps(binding.run_id)
                  if step.agent_role is AgentRole.SECURITY and step.status is WorkflowStepStatus.RUNNING]
        if run is None:
            raise ValueError
        validation_cycle(run)
        if (run.fix_attempt != getattr(execution, "fix_attempt", 0)
                or run.workspace_id != binding.workspace_id or run.code_version != source.code_version
                or configuration != execution.configuration or len(active) != 1
                or active[0].workflow_step_id != execution.metadata.workflow_step_id
                or active[0].attempt != execution.metadata.attempt or active[0].code_version != source.code_version
                or active[0].input_artifact_ids != [source.artifact_id]
                or execution.metadata.project_artifact_ids != (source.artifact_id,)
                or tuple(active[0].requirement_ids) != execution.metadata.requirement_ids
                or not execution.metadata.requirement_ids
                or not set(execution.metadata.requirement_ids) <= set(source.requirement_ids)):
            raise ValueError
        registered = [artifact for artifact in self._repository.list_project_artifacts(binding.run_id)
                      if artifact.artifact_type == "SOURCE" and artifact.code_version == source.code_version]
        if len(registered) != 1 or registered[0] != source:
            raise ValueError
        verify_report_predecessor(self._repository, execution, AgentRole.SECURITY, SecurityReportArtifact)
        self._artifacts.verify_candidate(source)
        staged, files = SnapshotReader(self._artifacts)._load(binding, self.configuration.frozen_source)
        if staged.execution_manifest() != source.execution_manifest():
            raise ValueError
        environment = configuration.configuration.environment
        policy = self.configuration.security_scan_configuration
        if (environment is None or environment.network_policy != "DENY"
                or source.container_image_digest != environment.container_image_digest
                or source.dependency_lock_hash != environment.dependency_lock_hash
                or configuration.configuration.scanner_profile_ref is None
                or any(profile.profile_ref != configuration.configuration.scanner_profile_ref for profile in policy.profiles)
                or policy.image_reference is not None
                and policy.image_reference.rsplit("@", 1)[-1] != environment.container_image_digest):
            raise ValueError
        unit = self.configuration.unit_test_configuration
        if unit is not None:
            # A frozen protected-suite obligation cannot disappear because the
            # Security child lacks its protected scopes, and vice versa.
            reference = configuration.configuration.protected_test_suite_ref
            if ((reference is not None) != bool(self._protected_selectors)
                    or any(scope.kind == "PROTECTED" and scope.protected_suite_ref != reference for scope in unit.scopes)
                    or unit.image_reference is not None
                    and unit.image_reference.rsplit("@", 1)[-1] != environment.container_image_digest
                    or any(case.requirement_id not in execution.metadata.requirement_ids for case in self._protected_cases)):
                raise ValueError
        self._evaluation_policies.pin(binding, workflow_step_id=execution.metadata.workflow_step_id,
            source_artifact_id=source.artifact_id,
            policy_sha256=security_policy_sha256(self.configuration, self._protected_cases))
        return dict(sorted(files.items()))

    async def prepare(self, execution):
        execution.budget.check()
        try:
            await _run_file_operation(self._verify, execution)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise SecurityServicesError() from None
        execution.budget.check()

    def tracked(self, client, execution):
        if type(client) is not BoundMCPClient or client.configuration != self.configuration:
            raise SecurityServicesError()
        return TrackedMCPExecutor(client, self._tools, workflow_step_id=execution.metadata.workflow_step_id)

    def _tracked(self, tracked, execution):
        if (type(tracked) is not TrackedMCPExecutor or tracked._client.configuration != self.configuration
                or tracked._store._repository is not self._repository
                or tracked._step_id != execution.metadata.workflow_step_id):
            raise SecurityServicesError()

    def _bundle(self, measured, execution):
        if (type(measured) is not SecurityMeasuredBundle or self._issued.get(measured._token) is not measured
                or measured.run_id != execution.metadata.run_id
                or measured.workflow_step_id != execution.metadata.workflow_step_id
                or measured.source_artifact_id != execution.source.artifact_id
                or measured.snapshot_sha256 != execution.source.snapshot_sha256):
            raise SecurityServicesError("SECURITY_SCAN_EVIDENCE_INVALID")

    def _capture(self, execution, selector):
        files = self._verify(execution)
        binding = self.configuration.binding
        context = MCPExecutionContext(binding=binding,
            workspace=self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.SECURITY))
        tools = SecurityScanTools(self._artifacts,
            SandboxRuntime(self._repository, self._workspaces, self._artifacts), self._scans,
            configuration=self.configuration.security_scan_configuration,
            max_call_seconds=self.configuration.max_call_seconds)
        scanner, profile = tools._scanner_profile(selector)
        source, inputs = tools._prepare(context, scanner, execution.source.artifact_id, profile)
        content = self._artifacts.bind(binding.run_id, role=AgentRole.SECURITY).read(source.artifact_id)
        inventory, _python_files = self._scans.source_inventory(content.content)
        return files, scanner, profile, self._scans._inputs_payload(inputs,
            self.configuration.security_scan_configuration, scanner), self._scans._host_policy(
            self.configuration.security_scan_configuration, scanner), inventory

    async def scan(self, execution, tracked):
        execution.budget.check()
        self._tracked(tracked, execution)
        if self._scan_started:
            raise SecurityServicesError("SECURITY_SCAN_EVIDENCE_INVALID")
        self._scan_started = True
        scans, findings, profiles = [], [], []
        source_files = None
        for scanner in self.configuration.security_scan_configuration.profiles:
            try:
                files, expected_scanner, profile, inputs, host, inventory = await _run_file_operation(
                    self._capture, execution, scanner.name)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SecurityServicesError("SECURITY_SCAN_EVIDENCE_INVALID") from None
            execution.budget.check()
            tool_sequence, timeout = execution.budget.reserve_tracked_tool_call()
            measured = await asyncio.wait_for(tracked.invoke("run_security_scan", {
                "workspaceId": str(execution.configuration.workspace_id), "snapshotId": str(execution.source.artifact_id),
                "scannerProfile": scanner.name}, deadline_monotonic=execution.budget.deadline_monotonic), timeout)
            execution.budget.account_tool_call(tool_sequence)
            execution.budget.check()
            try:
                record = await _run_file_operation(self._tools.get, self.configuration.binding, measured.record.logical_call_id)
                receipt = await _run_file_operation(self._scans.get, execution.metadata.run_id,
                                                   measured.data["executionManifestId"])
                evidence = record.to_tool_evidence()
                if (record != measured.record or measured.data != receipt.tool_output()
                        or record.role is not AgentRole.SECURITY or record.tool_name != "run_security_scan"
                        or record.workflow_step_id != execution.metadata.workflow_step_id
                        or record.source_artifact_id != execution.source.artifact_id
                        or evidence.outcome is not ToolExecutionOutcome.PASS
                        or record.execution_manifest != execution.source.execution_manifest()
                        or record.selector_sha256 != sha256(scanner.name.encode()).hexdigest()
                        or receipt.run_id != execution.metadata.run_id
                        or receipt.workflow_step_id != execution.metadata.workflow_step_id
                        or receipt.workspace_id != execution.configuration.workspace_id
                        or receipt.role is not AgentRole.SECURITY
                        or receipt.source_artifact_id != execution.source.artifact_id
                        or receipt.execution_manifest != execution.source.execution_manifest()
                        or receipt.execution_profile != profile or receipt.scanner_profile != expected_scanner
                        or receipt.profile_name != scanner.name or _plain(receipt.inputs) != inputs
                        or _plain(receipt.host_configuration) != host or _plain(receipt.source_files) != inventory
                        or source_files is not None and files != source_files):
                    raise ValueError
                source_files = files
                scans.append((record, receipt, evidence))
                summaries = []
                for finding in receipt.report.findings:
                    identity = str(uuid4())
                    findings.append((identity, scanner.name, finding, receipt))
                    summaries.append({"findingId": identity, **finding.to_dict()})
                if len(findings) > 1000:
                    raise ValueError
                profiles.append({"profileName": scanner.name, "scannerVersion": receipt.report.scanner_version,
                    "ruleIds": list(receipt.report.rule_ids), "reportRef": receipt.report_ref,
                    "executionManifestId": str(receipt.execution_manifest_id), "findings": summaries})
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SecurityServicesError("SECURITY_SCAN_EVIDENCE_INVALID") from None
        execution.budget.check()
        payload = {"executionManifest": execution.source.execution_manifest().model_dump(mode="json", by_alias=True),
            "profiles": profiles, "sourceFiles": [{"path": path, "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)}
                for path, content in source_files.items()]}
        try:
            sanitize_content(payload, reject_secrets=True)
            bundle = SecurityMeasuredBundle(run_id=execution.metadata.run_id,
                workflow_step_id=execution.metadata.workflow_step_id, source_artifact_id=execution.source.artifact_id,
                snapshot_sha256=execution.source.snapshot_sha256, source_files=MappingProxyType(source_files),
                scans=tuple(scans), findings=tuple(findings), _analysis_json=json_text(payload), _token=uuid4())
            self._issued[bundle._token] = bundle
            self._reads[bundle._token] = MappingProxyType({})
            return bundle
        except Exception:
            raise SecurityServicesError("SECURITY_SCAN_EVIDENCE_INVALID") from None

    async def invoke(self, tracked, execution, name, arguments, measured):
        """Record only independently verified exact frozen reads, never prose."""
        execution.budget.check()
        self._tracked(tracked, execution)
        self._bundle(measured, execution)
        if name == "write_test_file":
            # Generated Security tests are scratch only; the Host still runs them.
            if (not self._test_selectors or type(arguments.get("path")) is not str
                    or not arguments["path"].startswith("outputs/security/tests/")):
                raise SecurityServicesError("SECURITY_TEST_INPUT_INVALID")
            result = await tracked.invoke(name, arguments, deadline_monotonic=execution.budget.deadline_monotonic)
            execution.budget.check()
            return result
        if name not in {"read_project_file", "read_security_report"}:
            raise SecurityServicesError("SECURITY_CODE_REFERENCE_INVALID")
        if name == "read_security_report":
            if arguments.get("reportRef") not in {receipt.report_ref for _record, receipt, _evidence in measured.scans}:
                raise SecurityServicesError("SECURITY_SCAN_EVIDENCE_INVALID")
        else:
            requested = arguments.get("path")
            prefix = f"snapshots/{execution.source.artifact_id}/source/"
            relative = requested[len(prefix):] if type(requested) is str and requested.startswith(prefix) else (
                requested[7:] if type(requested) is str and requested.startswith("source/") else None)
            if relative not in measured.source_files:
                raise SecurityServicesError("SECURITY_CODE_REFERENCE_INVALID")
        result = await tracked.invoke(name, arguments, deadline_monotonic=execution.budget.deadline_monotonic)
        execution.budget.check()
        try:
            record = await _run_file_operation(self._tools.get, self.configuration.binding, result.record.logical_call_id)
            if (record != result.record or record.workflow_step_id != execution.metadata.workflow_step_id
                    or record.role is not AgentRole.SECURITY or record.tool_name != name
                    or not record.attempts or record.attempts[-1].status != "FINISHED"
                    or record.attempts[-1].outcome is not ToolExecutionOutcome.PASS
                    or record.attempts[-1].output_sha256 != _digest(result.data)):
                raise ValueError
            if name == "read_project_file":
                content = measured.source_files[relative]
                expected = {"path": requested, "content": content.decode("utf-8"),
                            "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)}
                if result.data != expected:
                    raise ValueError
                proof = _ReadProof(path=relative, requested_path=requested, content_sha256=expected["sha256"],
                    logical_call_id=record.logical_call_id, output_sha256=record.attempts[-1].output_sha256)
                self._reads[measured._token] = MappingProxyType({**self._reads[measured._token], relative: proof})
            else:
                receipt = next(receipt for _record, receipt, _evidence in measured.scans
                               if receipt.report_ref == arguments["reportRef"])
                if result.data != {"securityResult": receipt.report.to_dict()}:
                    raise ValueError
            return result
        except Exception:
            raise SecurityServicesError("SECURITY_CODE_REFERENCE_INVALID") from None

    def _anchors(self, execution, measured, references):
        anchors = []
        reads = self._reads[measured._token]
        for reference in references:
            if reference.path not in reads or reference.path not in measured.source_files:
                raise SecurityServicesError("SECURITY_CODE_REFERENCE_INVALID")
            read = reads[reference.path]
            record = self._tools.get(self.configuration.binding, read.logical_call_id)
            content = measured.source_files[reference.path]
            lines = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if lines[-1] == "":
                lines.pop()
            output = {"path": read.requested_path, "content": content.decode("utf-8"),
                      "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)}
            arguments = {"workspaceId": str(execution.configuration.workspace_id), "path": read.requested_path}
            if (not 1 <= reference.start_line <= reference.end_line <= len(lines)
                    or record.workflow_step_id != execution.metadata.workflow_step_id
                    or record.role is not AgentRole.SECURITY or record.tool_name != "read_project_file"
                    or record.input_sha256 != arguments_sha256("read_project_file", arguments)
                    or record.attempts[-1].outcome is not ToolExecutionOutcome.PASS
                    or read.content_sha256 != sha256(content).hexdigest()
                    or read.output_sha256 != record.attempts[-1].output_sha256
                    or read.output_sha256 != _digest(output)):
                raise SecurityServicesError("SECURITY_CODE_REFERENCE_INVALID")
            selected = "\n".join(lines[reference.start_line - 1:reference.end_line]).encode("utf-8")
            anchors.append({"path": reference.path, "startLine": reference.start_line, "endLine": reference.end_line,
                "fileSha256": read.content_sha256, "linesSha256": sha256(selected).hexdigest(),
                "readEvidenceRef": record.evidence_ref,
                "sourceRef": f"artifact://{execution.source.artifact_id}/source.tar#path="
                    + quote(reference.path, safe="/") + "&startLine=" + str(reference.start_line)
                    + "&endLine=" + str(reference.end_line)})
        return tuple(anchors)

    async def finalize(self, execution, decision, tracked, measured, *, task_id, context_id):
        from agents.roles.security_contract import SecurityDecision, validate_security_decision
        execution.budget.check()
        self._tracked(tracked, execution)
        self._bundle(measured, execution)
        try:
            if type(decision) is not SecurityDecision or decision.kind != "READY":
                raise ValueError
            reference = lambda value: {"path": value.path, "startLine": value.start_line, "endLine": value.end_line}
            data = {"kind": decision.kind, "questions": list(decision.questions), "requirementReviews": [{
                "requirementId": str(review.requirement_id), "proposedOutcome": review.proposed_outcome,
                "rationale": review.rationale, "references": [reference(value) for value in review.references]}
                for review in decision.requirement_reviews], "findingReviews": [{
                "findingId": review.finding_id, "proposedDisposition": review.proposed_disposition,
                "rationale": review.rationale, "references": [reference(value) for value in review.references]}
                for review in decision.finding_reviews]}
            if self._test_selectors:
                data["testCases"] = [{"selector": case.selector, "testId": case.test_id,
                    "requirementId": str(case.requirement_id), "title": case.title,
                    "expectedResult": case.expected_result} for case in decision.test_cases]
            validated = validate_security_decision(data, execution.metadata.requirement_ids,
                                                  measured.finding_ids, measured.source_paths,
                                                  self._test_selectors or None)
            anchors = {}
            for review in (*validated.requirement_reviews, *validated.finding_reviews):
                identity = getattr(review, "requirement_id", getattr(review, "finding_id", None))
                anchors[identity] = await _run_file_operation(self._anchors, execution, measured, review.references)
        except asyncio.CancelledError:
            raise
        except SecurityServicesError:
            raise
        except Exception:
            raise SecurityServicesError("SECURITY_REVIEW_INVALID") from None
        tests = None
        if self._test_selectors:
            tests = await self._run_tests(execution, validated, tracked)
        proof = None
        if self._verifier is not None:
            try:
                if inspect.iscoroutinefunction(self._verifier):
                    proof = await asyncio.wait_for(self._verifier(execution, validated, measured),
                                                   execution.budget.remaining_seconds())
                else:
                    # A synchronous trusted verifier is drained on cancel;
                    # deadline exhaustion is checked again before publishing.
                    proof = await _run_file_operation(self._verifier, execution, validated, measured)
                    if inspect.isawaitable(proof):
                        proof = await asyncio.wait_for(proof, execution.budget.remaining_seconds())
                execution.budget.check()
            except asyncio.CancelledError:
                raise
            except LLMRuntimeError as error:
                if error.code in {LLMErrorCode.BUDGET, LLMErrorCode.TIMEOUT}:
                    raise LLMRuntimeError(error.code) from None
                raise SecurityServicesError("SECURITY_PROOF_INVALID") from None
            except asyncio.TimeoutError:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            except Exception:
                raise SecurityServicesError("SECURITY_PROOF_INVALID") from None
        execution.budget.check()
        return await _run_file_operation(self._assemble, execution, validated, measured, anchors, proof,
                                         task_id, context_id, tests)

    def _capture_tests(self, execution, selector):
        # Copy the exact generated or protected inputs before the Tool runs,
        # as QA does, so the receipt can be compared with what was offered.
        self._verify(execution)
        binding = self.configuration.binding
        context = MCPExecutionContext(binding=binding,
            workspace=self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.SECURITY))
        tools = UnitTestTools(self._artifacts, SandboxRuntime(self._repository, self._workspaces, self._artifacts),
            self._unit, configuration=self.configuration.unit_test_configuration,
            max_call_seconds=self.configuration.max_call_seconds)
        scope, profile = tools._scope_profile(AgentRole.SECURITY, selector)
        _source, captured = tools._prepare(context, scope, execution.source.artifact_id)
        return self._unit._inputs_payload(captured, scope), profile, self._unit._scope_payload(scope)

    async def _run_tests(self, execution, decision, tracked):
        """Run every generated and protected Security scope; decide each Requirement from measured cases."""
        cases = decision.test_cases + self._protected_cases
        if any(case.selector not in self._test_selectors for case in decision.test_cases):
            raise SecurityServicesError("SECURITY_TEST_BINDING_INVALID")
        binding, measured = self.configuration.binding, {}
        for selector in self._test_selectors + self._protected_selectors:
            execution.budget.check()
            try:
                inputs, profile, policy = await _run_file_operation(self._capture_tests, execution, selector)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SecurityServicesError("SECURITY_TEST_INPUT_INVALID") from None
            arguments = {"workspaceId": str(execution.configuration.workspace_id),
                         "snapshotId": str(execution.source.artifact_id), "testScope": selector}
            tool_sequence, timeout = execution.budget.reserve_tracked_tool_call()
            result = await asyncio.wait_for(tracked.invoke("run_unit_tests", arguments,
                deadline_monotonic=execution.budget.deadline_monotonic), timeout)
            execution.budget.account_tool_call(tool_sequence)
            execution.budget.check()
            try:
                record = await _run_file_operation(self._tools.get, binding, result.record.logical_call_id)
                receipt = await _run_file_operation(self._unit.get, execution.metadata.run_id,
                                                   result.data["executionManifestId"])
                evidence = record.to_tool_evidence()
                if (record != result.record or result.data != receipt.tool_output()
                        or record.workflow_step_id != execution.metadata.workflow_step_id
                        or record.role is not AgentRole.SECURITY or record.tool_name != "run_unit_tests"
                        or record.source_artifact_id != execution.source.artifact_id
                        or evidence.outcome is not ToolExecutionOutcome.PASS
                        or record.execution_manifest != execution.source.execution_manifest()
                        or record.selector_sha256 != sha256(selector.encode()).hexdigest()
                        or receipt.run_id != execution.metadata.run_id
                        or receipt.workflow_step_id != execution.metadata.workflow_step_id
                        or receipt.workspace_id != execution.configuration.workspace_id
                        or receipt.role is not AgentRole.SECURITY or receipt.tool_name != "run_unit_tests"
                        or receipt.source_artifact_id != execution.source.artifact_id
                        or receipt.execution_manifest != execution.source.execution_manifest()
                        or receipt.execution_profile != profile or receipt.profile_name != selector
                        or _plain(receipt.inputs) != _plain(inputs)
                        or self._unit._scope_payload(receipt.scope) != policy):
                    raise ValueError
                measured[selector] = (receipt, evidence)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SecurityServicesError("SECURITY_TEST_EVIDENCE_INVALID") from None
        for selector, (receipt, _evidence) in measured.items():
            bound = {case.test_id for case in cases if case.selector == selector}
            if any(actual.test_id not in bound for actual in receipt.report.tests):
                # Never guess a Requirement for an unbound executed case.
                raise SecurityServicesError("SECURITY_TEST_BINDING_INVALID")
        rows = {}
        for case in cases:
            receipt, evidence = measured[case.selector]
            actual = next((item for item in receipt.report.tests if item.test_id == case.test_id), None)
            outcome = actual.outcome if actual is not None and actual.outcome in {"PASS", "FAIL"} else "UNVERIFIED"
            rows.setdefault(case.requirement_id, []).append(
                (outcome, evidence, f"run_unit_tests:{case.selector}:{case.test_id}"))
        decided = {}
        for requirement_id in execution.metadata.requirement_ids:
            items = rows.get(requirement_id, [])
            failed = [item for item in items if item[0] == "FAIL"]
            if failed:
                value, evidence = "FAIL", failed[0][1]
            elif items and all(item[0] == "PASS" for item in items):
                value, evidence = "PASS", items[0][1]
            else:
                value, evidence = "UNVERIFIED", None
            decided[requirement_id] = (value, evidence, tuple({"testId": test_id, "outcome": outcome}
                                                              for outcome, _evidence, test_id in items))
        return decided

    def _assemble(self, execution, decision, measured, anchors, proof, task_id, context_id, tests=None):
        requirement_values, finding_values = {}, {}
        if proof is not None:
            if type(proof) is not SecuritySemanticProof:
                raise SecurityServicesError("SECURITY_PROOF_INVALID")
            # Frozen constructors are not validation of deserialized or
            # privileged-mutated Host callback results at this admission point.
            proof.__post_init__()
            if (type(proof) is not SecuritySemanticProof or proof.run_id != execution.metadata.run_id
                    or proof.workflow_step_id != execution.metadata.workflow_step_id
                    or proof.source_artifact_id != execution.source.artifact_id
                    or proof.snapshot_sha256 != execution.source.snapshot_sha256
                    or not set(dict(proof.requirement_outcomes)) <= set(execution.metadata.requirement_ids)
                    or not set(dict(proof.finding_dispositions)) <= set(measured.finding_ids)):
                raise SecurityServicesError("SECURITY_PROOF_INVALID")
            requirement_values, finding_values = dict(proof.requirement_outcomes), dict(proof.finding_dispositions)
        try:
            self._verify(execution)
            # Recheck every immutable scan receipt/journal after LLM analysis.
            for expected_record, expected_receipt, expected_evidence in measured.scans:
                record = self._tools.get(self.configuration.binding, expected_record.logical_call_id)
                receipt = self._scans.get(execution.metadata.run_id, expected_receipt.execution_manifest_id)
                if record != expected_record or receipt != expected_receipt or record.to_tool_evidence() != expected_evidence:
                    raise ValueError
            semantic_details = {}
            if proof is not None and proof.verification_receipt_id is not None:
                # An opaque reference is not evidence by itself. The approved
                # adapter must reread its immutable record and current inputs.
                check = getattr(self._verifier, "verify_receipt", None)
                if not callable(check):
                    raise SecurityServicesError("SECURITY_PROOF_INVALID")
                checked = check(proof, execution, decision, measured)
                if inspect.isawaitable(checked):
                    if inspect.iscoroutine(checked):
                        checked.close()
                    raise SecurityServicesError("SECURITY_PROOF_INVALID")
                semantic_details = {
                    "semanticProofRef": f"artifact://{proof.verification_receipt_id}/security-semantic-proof.json",
                    "semanticProofApprovalSha256": proof.approval_sha256,
                    "semanticProofRequestSha256": proof.request_sha256,
                }
            evidence = measured.scans[0][2]
            requirements, findings = [], []
            for review in decision.requirement_reviews:
                if tests is not None:
                    # Measured Security test receipts decide the Requirement.
                    value, test_evidence, cases = tests[review.requirement_id]
                    location = None if not anchors[review.requirement_id] else (
                        anchors[review.requirement_id][0]["path"] + ":" + str(anchors[review.requirement_id][0]["startLine"]))
                    requirements.append(SecurityRequirementResult(requirement_id=review.requirement_id,
                        outcome=ValidationOutcome(value), details=json_text({
                            "code": "MEASURED_SECURITY_TESTS" if value != "UNVERIFIED" else "SECURITY_TESTS_UNVERIFIED",
                            "tests": list(cases), "codeReferences": list(anchors[review.requirement_id])}),
                        actual_result=value, normalized_location=location,
                        tool_evidence=test_evidence if value != "UNVERIFIED" else None))
                    continue
                value = requirement_values.get(review.requirement_id, "UNVERIFIED")
                if value in {"PASS", "FAIL"} and (review.proposed_outcome not in {"PASS", "FAIL"} or not anchors[review.requirement_id]):
                    raise SecurityServicesError("SECURITY_PROOF_INVALID")
                location = None if not anchors[review.requirement_id] else (
                    anchors[review.requirement_id][0]["path"] + ":" + str(anchors[review.requirement_id][0]["startLine"]))
                requirements.append(SecurityRequirementResult(requirement_id=review.requirement_id,
                    outcome=ValidationOutcome(value), details=json_text({
                        "code": "HOST_SEMANTIC_PROOF" if value != "UNVERIFIED" else "SEMANTIC_PROOF_REQUIRED",
                        "codeReferences": list(anchors[review.requirement_id]), **semantic_details}),
                    actual_result=value, normalized_location=location,
                    tool_evidence=evidence if value != "UNVERIFIED" else None))
            proposals = {review.finding_id: review for review in decision.finding_reviews}
            for identity, _scanner, actual, receipt in measured.findings:
                review = proposals[identity]
                value = finding_values.get(identity, "UNVERIFIED" if review.proposed_disposition == "UNVERIFIED" else "SUSPECTED")
                if value in {"CONFIRMED", "FALSE_POSITIVE"} and (
                        review.proposed_disposition not in {"CONFIRMED", "FALSE_POSITIVE"}
                        or not any(anchor["path"] == actual.path
                                   and anchor["startLine"] <= actual.line <= anchor["endLine"]
                                   for anchor in anchors[identity])):
                    raise SecurityServicesError("SECURITY_PROOF_INVALID")
                findings.append(SecurityFinding(finding_id=identity, severity=SecuritySeverity(actual.severity),
                    disposition=FindingDisposition(value), title=actual.rule_id + ":" + actual.test_name,
                    description=json_text({"code": "HOST_SEMANTIC_PROOF" if value in {"CONFIRMED", "FALSE_POSITIVE"}
                        else "SCANNER_CANDIDATE_REQUIRES_VERIFICATION", "codeReferences": list(anchors[identity]), **semantic_details}),
                    evidence_ref=receipt.report_ref, rule_id=actual.rule_id,
                    normalized_location=actual.path + ":" + str(actual.line)))
            version = verify_report_predecessor(self._repository, execution, AgentRole.SECURITY, SecurityReportArtifact)
            previous = getattr(execution, "previous_report", None)
            report = SecurityReportArtifact(artifact_id=uuid4(), artifact_version=version,
                previous_artifact_id=None if previous is None else previous.artifact_id,
                run_id=execution.metadata.run_id, workflow_step_id=execution.metadata.workflow_step_id,
                a2a_task_id=task_id, a2a_artifact_id=str(uuid4()), requirement_ids=execution.metadata.requirement_ids,
                code_version=execution.source.code_version, execution_manifest=execution.source.execution_manifest(),
                requirement_results=tuple(requirements), findings=tuple(findings))
            payload = report.model_dump(mode="json", by_alias=True)
            sanitize_content(payload, reject_secrets=True)
            payload = parse_json(json_text(payload))
            schemas, registry = _schemas()
            Draft202012Validator(schemas["security_report.schema.json"], registry=registry,
                                 format_checker=FormatChecker()).validate(payload)
            artifact = Artifact(artifact_id=report.a2a_artifact_id, name="security-report.json",
                parts=[new_data_part(payload, media_type="application/json")], metadata={
                    "runId": str(report.run_id), "workflowStepId": str(report.workflow_step_id),
                    "projectArtifactId": str(report.artifact_id), "artifactVersion": report.artifact_version})
            task = Task(id=task_id, context_id=context_id, status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                artifacts=[artifact], metadata=execution.metadata.model_dump(mode="json", by_alias=True, exclude_none=True))
            run = WorkflowRun(run_id=report.run_id, workspace_id=execution.configuration.workspace_id,
                scenario_id=execution.metadata.scenario_id, request_text=execution.request_text,
                status=WorkflowStatus.VALIDATING if execution.source.code_version == 1 else WorkflowStatus.REVALIDATING,
                fix_attempt=getattr(execution, "fix_attempt", 0), code_version=execution.source.code_version)
            step = WorkflowStep(run_id=report.run_id, workflow_step_id=report.workflow_step_id,
                agent_role=AgentRole.SECURITY, status=WorkflowStepStatus.SUCCEEDED,
                a2a_task_id=task_id, agent_context_id=context_id, a2a_task_state=A2ATaskState.COMPLETED,
                attempt=execution.metadata.attempt, code_version=execution.source.code_version, requirement_ids=list(report.requirement_ids),
                input_artifact_ids=list(execution.metadata.project_artifact_ids or ()))
            validate_completed_role_output(AgentRole.SECURITY, task=task, run=run, step=step, source=execution.source)
            return (artifact,)
        except SecurityServicesError:
            raise
        except Exception:
            raise SecurityServicesError("SECURITY_ARTIFACT_INVALID") from None
