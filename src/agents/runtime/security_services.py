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
from agents.runtime.generated_revalidation import run_previous_scopes
from mcp_tools.tools.generated_recheck import completed_count as completed_generated_rechecks
from agents.runtime.protected_scopes import (
    claim_protected_scope, commit_protected_scope, completed_protected_scopes, load_protected_receipt,
)
from agents.runtime.evaluation_policy_store import EvaluationPolicyStore, security_policy_sha256
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, open_mcp_client
from mcp_tools.execution_policy import arguments_sha256
from mcp_tools.execution_runtime import TrackedMCPError, TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore, _digest
from mcp_tools.runtime import MCPExecutionContext
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_store import UnitTestOutputStore
from mcp_tools.tools.snapshots import SnapshotReader
from orchestrator.artifacts.service import ArtifactStore
from agents.runtime.test_fix import TestFixRequired, technical_reasons
from agents.runtime.security_test_scope import (generated_requirement_coverage, measured_hash_complies,
                                               protected_evidence_covers, simple_assertion_limits)
from orchestrator.domain import test_not_run
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
            # Measured Security test mode: input, ID binding and run evidence.
            "SECURITY_TEST_INPUT_INVALID", "SECURITY_TEST_BINDING_INVALID", "SECURITY_TEST_EVIDENCE_INVALID", "SECURITY_TEST_FIX_INVALID",
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

    def read_paths(self, measured):
        """Source paths read in this step for this measured Snapshot (Host-observed reads only)."""
        return frozenset(self._reads.get(getattr(measured, "_token", None), {}))

    def refresh_output(self, output):
        """Security's generated-test IDs come from the Host catalog, as QA's do (Codex review 2026-11-18 #1)."""
        from agents.runtime.qa_catalog import SECURITY_ID_POLICY, refresh_qa_output
        binding = self.configuration.binding
        workspace = self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.SECURITY)
        unit = self.configuration.unit_test_configuration
        scopes = [(s.name, s.pattern) for s in (() if unit is None else unit.scopes)
                  if s.kind == 'QA_TESTS' and s.name in self._test_selectors]
        return refresh_qa_output(output, Path(workspace.record.root_path) / 'outputs' / 'security' / 'tests',
            scopes, snapshot_sha256=self.configuration.frozen_source.snapshot_sha256, unit_only=True,
            field='testCases', policy=SECURITY_ID_POLICY)

    def generated_test_issues(self, data):
        """Read-only pre-READY check of the generated Security test files (handoff P5, points 4·6)."""
        from agents.runtime.generated_tests_precheck import inspect_generated_tests
        if not self._test_selectors:
            return []
        binding = self.configuration.binding
        workspace = self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.SECURITY)
        patterns = {scope.name: scope.pattern for scope in self.configuration.unit_test_configuration.scopes
                    if getattr(scope, "kind", None) == "QA_TESTS"}
        scopes = [(name, patterns[name]) for name in self._test_selectors if name in patterns]
        rows = [row for row in (data.get("testCases") or []) if isinstance(row, dict)] if isinstance(data, dict) else []
        source = Path(workspace.record.root_path) / "source"
        from orchestrator.domain import SCN_001_ID
        run = self._repository.get_run(binding.run_id)
        return inspect_generated_tests(Path(workspace.record.root_path) / "outputs" / "security" / "tests",
                                       scopes, rows, "testCases", source_root=source if source.is_dir() else None,
                                       host_app_fixture=run is not None and run.scenario_id == SCN_001_ID)

    def reference_issues(self, measured, data):
        """The ``_anchors`` rules that a draft can still fix (handoff P5, point 5).

        A cited file must have been read in this step and the cited lines must
        exist in it. Run before READY so the model can read the file or fix the
        range once; ``finalize`` still applies the full anchor proof.
        """
        reads = self._reads.get(getattr(measured, "_token", None), {})
        issues = []
        for field in ("requirementReviews", "findingReviews"):
            for index, row in enumerate((data.get(field) or []) if isinstance(data, dict) else []):
                for number, reference in enumerate((row.get("references") or []) if isinstance(row, dict) else []):
                    if not isinstance(reference, dict) or reference.get("path") not in measured.source_files:
                        continue  # unknown paths are the contract's (and its explainer's) business
                    pointer = f"/{field}/{index}/references/{number}"
                    if reference["path"] not in reads:
                        issues.append({"fieldPath": pointer + "/path", "reasonCode": "REFERENCE_FILE_NOT_READ"})
                        continue
                    lines = measured.source_files[reference["path"]].decode("utf-8").replace(
                        "\r\n", "\n").replace("\r", "\n").split("\n")
                    if lines[-1] == "":
                        lines.pop()
                    end = reference.get("endLine")
                    if type(end) is int and end > len(lines):
                        issues.append({"fieldPath": pointer + "/endLine", "reasonCode": "REFERENCE_PAST_END_OF_FILE"})
        return issues[:16]

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

    def test_fix_available(self, execution) -> bool:
        """An early protected measurement does not consume the generated-test repair."""
        step = execution.metadata.workflow_step_id
        calls = self._tools.count_step_executions(self.configuration.binding, step, ("run_unit_tests",))
        protected = completed_protected_scopes(self._repository, run_id=execution.metadata.run_id,
                                              workflow_step_id=step, source_artifact_id=execution.source.artifact_id)
        return calls == len(protected) + completed_generated_rechecks(self._repository, step)

    async def run_protected(self, execution, tracked):
        """Measure Host-approved scopes once before any generated draft, without exposing results to the model."""
        if self._protected_selectors:
            self._tracked(tracked, execution)
            await self._measure_scopes(execution, tracked, self._protected_selectors)

        async def measure(selector):
            self._tracked(tracked, execution)
            results, _not_run, _limits = await self._measure_scopes(execution, tracked, (selector,))
            return results.get(selector)

        await run_previous_scopes(self, execution, tracked, self._test_selectors, measure)

    async def finalize(self, execution, decision, tracked, measured, *, task_id, context_id, test_fix_available=False):
        from agents.roles.security_contract import SecurityDecision, validate_security_decision
        from agents.runtime.requirement_scope import validate_generated_case_expectations
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
            if self._test_selectors:
                validate_generated_case_expectations(validated.test_cases, configuration=execution.configuration,
                    metadata=execution.metadata, role=AgentRole.SECURITY)
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
            tests = await self._run_tests(execution, validated, tracked, test_fix_available)
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
        limits = simple_assertion_limits(captured.files, scope.source_directory) if scope.kind == 'QA_TESTS' else {}
        return self._unit._inputs_payload(captured, scope), profile, self._unit._scope_payload(scope), limits

    def _not_run_evidence(self, execution, selector, logical_call_id):
        """Host evidence that a generated Security scope was attempted and could not run."""
        try:
            record = self._tools.get(self.configuration.binding, logical_call_id)
            evidence = record.to_tool_evidence()
            if (record.workflow_step_id != execution.metadata.workflow_step_id
                    or record.role is not AgentRole.SECURITY or record.tool_name != "run_unit_tests"
                    or record.source_artifact_id != execution.source.artifact_id
                    or record.execution_manifest != execution.source.execution_manifest()
                    or record.selector_sha256 != sha256(selector.encode()).hexdigest()
                    or evidence.outcome is ToolExecutionOutcome.PASS):
                raise ValueError
            return evidence
        except Exception:
            raise SecurityServicesError("SECURITY_TEST_EVIDENCE_INVALID") from None

    async def _measure_scopes(self, execution, tracked, selectors):
        binding, measured, not_run = self.configuration.binding, {}, {}
        assertion_limits = {}
        for selector in selectors:
            execution.budget.check()
            try:
                protected = selector in self._protected_selectors
                if protected:
                    await _run_file_operation(self._verify, execution)
                    saved = await _run_file_operation(claim_protected_scope, self._repository, binding,
                        execution.metadata.workflow_step_id, execution.source.artifact_id, 'run_unit_tests', selector)
                    if saved is not None:
                        measured[selector] = await _run_file_operation(load_protected_receipt,
                            self._repository, binding, execution, saved)
                        continue
                inputs, profile, policy, limits = await _run_file_operation(self._capture_tests, execution, selector)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SecurityServicesError("SECURITY_TEST_INPUT_INVALID") from None
            arguments = {"workspaceId": str(execution.configuration.workspace_id),
                         "snapshotId": str(execution.source.artifact_id), "testScope": selector}
            tool_sequence, timeout = execution.budget.reserve_tracked_tool_call()
            try:
                result = await asyncio.wait_for(tracked.invoke("run_unit_tests", arguments,
                    deadline_monotonic=execution.budget.deadline_monotonic), timeout)
            except TrackedMCPError as error:
                # docs/live-api/05 3절: same rule as QA. Only a generated scope's
                # runner failure is kept as not-run cases; everything else stops.
                if (selector in self._protected_selectors or error.error_kind != "TEST_RUNNER_ERROR"
                        or error.logical_call_id is None):
                    raise
                execution.budget.account_tool_call(tool_sequence)
                execution.budget.check()
                not_run[selector] = await _run_file_operation(
                    self._not_run_evidence, execution, selector, error.logical_call_id)
                continue
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
                if protected:
                    await _run_file_operation(commit_protected_scope, self._repository,
                        execution.metadata.workflow_step_id, selector, record, receipt)
                measured[selector] = (receipt, evidence)
                assertion_limits[selector] = limits
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SecurityServicesError("SECURITY_TEST_EVIDENCE_INVALID") from None
        return measured, not_run, assertion_limits

    async def _run_tests(self, execution, decision, tracked, test_fix_available=False):
        """Run every generated and protected Security scope; decide each Requirement from measured cases."""
        cases = decision.test_cases + self._protected_cases
        if any(case.selector not in self._test_selectors for case in decision.test_cases):
            raise SecurityServicesError("SECURITY_TEST_BINDING_INVALID")
        measured, not_run, assertion_limits = await self._measure_scopes(
            execution, tracked, self._protected_selectors + self._test_selectors)
        for selector, (receipt, _evidence) in measured.items():
            bound = {case.test_id for case in cases if case.selector == selector}
            if any(actual.test_id not in bound and not test_not_run.is_fixture_row(actual.test_id)
                   for actual in receipt.report.tests):
                # Never guess a Requirement for an unbound executed case.
                raise SecurityServicesError("SECURITY_TEST_BINDING_INVALID")
        correction_reasons = technical_reasons(list(not_run), [
            actual for selector in self._test_selectors if selector in measured
            for actual in measured[selector][0].report.tests]) if test_fix_available else set()
        from orchestrator.domain.validation_feedback import case_feedback, measured_test_version
        rows, technical = {}, set()
        contract = execution.configuration.scenario_contract
        requirement_keys = {r['requirementId']: r['key'] for r in contract['requirements']
                            if contract.get('scenarioKey') == 'SCN-001'}
        hash_policy = contract.get('securityPolicy', {}).get('passwordHashPolicy')
        for case in cases:
            identity = f"run_unit_tests:{case.selector}:{case.test_id}"
            if case.selector in not_run:
                rows.setdefault(case.requirement_id, []).append(
                    ("UNVERIFIED", not_run[case.selector], identity, {"notRunReason": test_not_run.SCOPE_FAILED}))
                technical.add(identity)
                continue
            receipt, evidence = measured[case.selector]
            actual = next((item for item in receipt.report.tests if item.test_id == case.test_id), None)
            outcome = actual.outcome if actual is not None and actual.outcome in {"PASS", "FAIL"} else "UNVERIFIED"
            reason = test_not_run.runner_reason(actual)
            feedback = {**measured_test_version(receipt), **case_feedback(actual)}
            if actual is not None and actual.signup_checks_json:
                facts = json.loads(actual.signup_checks_json)
                feedback['signupChecks'] = facts
                key = requirement_keys.get(str(case.requirement_id))
                if (key == 'REQ-005' and hash_policy and 'storedHashParameters' in facts
                        and not measured_hash_complies(facts['storedHashParameters'], hash_policy)):
                    outcome = 'FAIL'
            limit = assertion_limits.get(case.selector, {}).get(case.test_id)
            if actual is not None and limit:
                feedback['sourceAssertionScope'] = limit
            if actual is not None and actual.output_checks:
                feedback['outputChecks'] = dict(actual.output_checks)
                if (actual.outcome == 'SKIP' and actual.details == 'REQUEST_RESPONSE_UNAVAILABLE'
                        and 'INCOMPLETE' in feedback['outputChecks'].values()):
                    feedback['unobservedReason'] = 'APP_RESPONSE_NOT_CAPTURED'
                if 'EXPOSED' in feedback['outputChecks'].values():
                    outcome = 'FAIL'
            if reason is not None:
                feedback = {**feedback, "notRunReason": reason}
                if reason in test_not_run.TECHNICAL:
                    technical.add(identity)
            rows.setdefault(case.requirement_id, []).append((outcome, evidence, identity, feedback))
        decided = {}
        for requirement_id in execution.metadata.requirement_ids:
            items = rows.get(requirement_id, [])
            failed = [item for item in items if item[0] == "FAIL"]
            unrun = [item for item in items if item[0] != "PASS" and item[2] in technical]
            if failed:
                value, evidence = "FAIL", failed[0][1]
            elif items and all(item[0] == "PASS" for item in items):
                value, evidence = "PASS", items[0][1]
            elif unrun and all(item[0] == "PASS" or item[2] in technical for item in items):
                # Every non-PASS case is a technical not-run: keep its attempt evidence.
                value, evidence = test_not_run.SECURITY_TESTS_NOT_RUN, unrun[0][1]
            else:
                value, evidence = "UNVERIFIED", None
            cases_measured = tuple({"testId": test_id, "outcome": outcome,
                                    "evidenceRefs": [receipt.evidence_ref], **feedback}
                                   for outcome, receipt, test_id, feedback in items)
            key = requirement_keys.get(str(requirement_id))
            generated = [row for row in cases_measured if row['testId'].split(':', 2)[1] in self._test_selectors]
            coverage, gaps = generated_requirement_coverage(key, generated, hash_policy)
            protected = [row for row in cases_measured if row['testId'].split(':', 2)[1] not in self._test_selectors]
            only_missing_observations = generated and all(
                row['outcome'] == 'PASS' or (row['outcome'] == 'UNVERIFIED'
                    and row.get('unobservedReason') == 'APP_RESPONSE_NOT_CAPTURED')
                for row in generated)
            if gaps and only_missing_observations:
                correction_reasons.update(gaps)
                if value in ('PASS', 'UNVERIFIED') and not protected_evidence_covers(key, protected):
                    value = 'OUTPUT_COVERAGE_INCOMPLETE'
            decided[requirement_id] = (value, evidence, cases_measured,
                tuple({str(row[1].execution_id): row[1] for row in items}.values()), coverage, gaps)
        if test_fix_available and correction_reasons:
            raise TestFixRequired(correction_reasons, self._test_selectors)
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
                    value, test_evidence, cases, attempt_evidence, coverage, gaps = tests[review.requirement_id]
                    location = None if not anchors[review.requirement_id] else (
                        anchors[review.requirement_id][0]["path"] + ":" + str(anchors[review.requirement_id][0]["startLine"]))
                    unrun = value == test_not_run.SECURITY_TESTS_NOT_RUN
                    coverage_gap = value == 'OUTPUT_COVERAGE_INCOMPLETE'
                    requirements.append(SecurityRequirementResult(requirement_id=review.requirement_id,
                        outcome=ValidationOutcome.UNVERIFIED if unrun or coverage_gap else ValidationOutcome(value), details=json_text({
                            "code": value if unrun else "SECURITY_TESTS_UNVERIFIED" if coverage_gap else "MEASURED_SECURITY_TESTS" if value != "UNVERIFIED"
                            else "SECURITY_TESTS_UNVERIFIED",
                            "tests": list(cases), "codeReferences": list(anchors[review.requirement_id]),
                            **({"generatedOutputCoverage": coverage} if coverage is not None else {}),
                            **({"generatedEvidenceGaps": gaps} if gaps else {})}),
                        actual_result="UNVERIFIED" if unrun else value, normalized_location=location,
                        execution_evidence=attempt_evidence,
                        # A technical not-run keeps the attempt evidence (docs/live-api/05 3절).
                        tool_evidence=test_evidence if value != "UNVERIFIED" and not coverage_gap else None))
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
