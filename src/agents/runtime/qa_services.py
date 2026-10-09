"""Host-owned immutable QA inputs, measured tests, and QA Artifact assembly.

The model supplies case bindings only. All executions use approved selectors
and the exact frozen Source. Missing receipts never become test results.
"""

import asyncio
from functools import partial
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from a2a.helpers import new_data_part
from a2a.types import Artifact, Task, TaskStatus, TaskState
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from agents.llm.content import sanitize_content
from agents.llm.contracts import json_text, parse_json
from agents.roles.outputs import validate_completed_role_output
from agents.roles.qa_contract import QACaseBinding, QADecision, validate_qa_decision
from agents.runtime.qa_test_store import QATestInputStore, _plain
from agents.runtime.qa_context import validation_cycle, verify_report_predecessor
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, open_mcp_client
from mcp_tools.execution_runtime import TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPExecutionContext
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.tools.snapshots import SnapshotReader
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.validation_artifacts import QAReportArtifact, QATestResult, ValidationOutcome
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry


class QAServicesError(ValueError):
    def __init__(self, code="QA_SERVICES_INVALID"):
        self.code = code if code in {
            "QA_SERVICES_INVALID", "QA_TEST_INPUT_INVALID", "QA_TEST_EVIDENCE_INVALID",
            "QA_REPORT_BINDING_INVALID", "QA_ARTIFACT_INVALID",
        } else "QA_SERVICES_INVALID"
        super().__init__(self.code)


def _schemas():
    root = Path(__file__).resolve().parents[3] / "schemas" / "project"
    names = ("qa_report.schema.json", "execution_manifest.schema.json", "tool_execution_evidence.schema.json")
    result, resources = {}, []
    for name in names:
        schema = parse_json((root / name).read_text(encoding="utf-8"), max_bytes=1_048_576)
        Draft202012Validator.check_schema(schema)
        result[name] = schema
        resource = Resource.from_contents(schema)
        resources.extend(((schema["$id"], resource),
                          ("https://a2a-agent-company.local/schemas/project/" + name, resource)))
    return result, Registry().with_resources(resources)


def _case_json(case):
    return {"toolName": case.tool_name, "selector": case.selector, "testId": case.test_id,
            "requirementId": str(case.requirement_id), "title": case.title, "expectedResult": case.expected_result}


class QARuntimeServices:
    """Explicit one-Run capability; construction performs no I/O.

    Protected selector bindings are trusted Host policy. They are never shown
    as model-selectable scopes and are always executed after a READY draft.
    client_factory is a trusted transport seam, not a model callback.
    """

    def __init__(self, repository, workspace_registry, artifact_store, *, mcp_configuration,
                 client_factory=open_mcp_client, protected_cases=()):
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
                    or mcp_configuration.binding.role is not AgentRole.QA
                    or mcp_configuration.binding.agent_role is not AgentRole.QA
                    or mcp_configuration.frozen_source is None
                    or mcp_configuration.build_configuration is not None
                    or mcp_configuration.security_scan_configuration is not None
                    or not callable(client_factory) or type(protected_cases) is not tuple
                    or len(protected_cases) > 256
                    or any(type(case) is not QACaseBinding for case in protected_cases)):
                raise ValueError
            selected, protected = {}, {}
            unit, browser = mcp_configuration.unit_test_configuration, mcp_configuration.browser_test_configuration
            for tool, configuration, items in (
                ("run_unit_tests", unit, () if unit is None else unit.scopes),
                ("run_browser_tests", browser, () if browser is None else browser.suites),
            ):
                if configuration is None:
                    continue
                timeout = min(configuration.limits.timeout_seconds,
                    mcp_configuration.max_call_seconds - 2 * configuration.limits.control_timeout_seconds - 1)
                if timeout < .01 or any(AgentRole.QA not in item.roles for item in items):
                    raise ValueError
                selected[tool] = tuple(item.name for item in items if item.kind == "QA_TESTS")
                protected[tool] = tuple(item.name for item in items if item.kind == "PROTECTED")
            selected = {tool: names for tool, names in selected.items() if names}
            protected = {tool: names for tool, names in protected.items() if names}
            if not selected:
                raise ValueError
            keys = [(case.tool_name, case.selector, case.test_id) for case in protected_cases]
            if (len(set(keys)) != len(keys)
                    or {(case.tool_name, case.selector) for case in protected_cases}
                       != {(tool, name) for tool, names in protected.items() for name in names}):
                raise ValueError
            # Copy/revalidate the Host case declarations; no mutable model
            # decision or constructor side effect establishes their authority.
            approved = tuple(QACaseBinding(**{name: getattr(case, name) for name in (
                "tool_name", "selector", "test_id", "requirement_id", "title", "expected_result")})
                for case in protected_cases)
        except Exception:
            raise QAServicesError() from None
        self.configuration, self.client_factory = mcp_configuration, client_factory
        self._repository, self._workspaces, self._artifacts = repository, workspace_registry, artifact_store
        self._tools = ToolExecutionStore(repository)
        self._unit, self._browser = UnitTestOutputStore(repository), BrowserTestOutputStore(repository)
        self._inputs = QATestInputStore(repository)
        self._selectors, self._protected, self._protected_cases = selected, protected, approved

    def __repr__(self):
        return "QARuntimeServices()"

    @property
    def selectors(self):
        return dict(self._selectors)

    async def prepare(self, execution):
        execution.budget.check()
        try:
            await _run_file_operation(self._verify, execution)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QAServicesError() from None
        execution.budget.check()

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
                  if step.agent_role is AgentRole.QA and step.status is WorkflowStepStatus.RUNNING]
        if run is None:
            raise ValueError
        validation_cycle(run)
        if (run.fix_attempt != getattr(execution, "fix_attempt", 0)
                or run.workspace_id != binding.workspace_id or run.code_version != source.code_version
                or configuration != execution.configuration or len(active) != 1
                or active[0].workflow_step_id != execution.metadata.workflow_step_id
                or active[0].attempt != execution.metadata.attempt
                or active[0].code_version != source.code_version or active[0].input_artifact_ids != [source.artifact_id]
                or execution.metadata.project_artifact_ids != (source.artifact_id,)
                or tuple(active[0].requirement_ids) != execution.metadata.requirement_ids
                or not set(execution.metadata.requirement_ids or ()) <= set(source.requirement_ids)
                or not execution.metadata.requirement_ids):
            raise ValueError
        registered = [artifact for artifact in self._repository.list_project_artifacts(binding.run_id)
                      if artifact.artifact_type == "SOURCE" and artifact.code_version == source.code_version]
        if len(registered) != 1 or registered[0] != source:
            raise ValueError
        verify_report_predecessor(self._repository, execution, AgentRole.QA, QAReportArtifact)
        self._artifacts.verify_candidate(source)
        # Verify the complete canonical archive and QA READ_ONLY grant now;
        # the actual Tool repeats these checks before creating a Sandbox.
        staged, _files = SnapshotReader(self._artifacts)._load(binding, self.configuration.frozen_source)
        if staged.execution_manifest() != source.execution_manifest():
            raise ValueError
        environment = configuration.configuration.environment
        if environment is None or environment.network_policy != "DENY":
            raise ValueError
        if configuration.configuration.protected_test_suite_ref is not None and not self._protected:
            # A frozen protected-suite obligation cannot disappear because
            # the Host child was accidentally configured with generated-only
            # scopes. No request to remove this obligation reaches the model.
            raise ValueError
        if (source.container_image_digest != environment.container_image_digest
                or source.dependency_lock_hash != environment.dependency_lock_hash
                or any(case.requirement_id not in execution.metadata.requirement_ids for case in self._protected_cases)):
            raise ValueError
        for policy in (self.configuration.unit_test_configuration, self.configuration.browser_test_configuration):
            if policy is None:
                continue
            if (policy.image_reference is not None
                    and policy.image_reference.rsplit("@", 1)[-1] != environment.container_image_digest):
                raise ValueError
            for selector in getattr(policy, "scopes", getattr(policy, "suites", ())):
                if (selector.kind == "PROTECTED"
                        and selector.protected_suite_ref != configuration.configuration.protected_test_suite_ref):
                    raise ValueError

    def tracked(self, client, execution):
        if type(client) is not BoundMCPClient or client.configuration != self.configuration:
            raise QAServicesError()
        return TrackedMCPExecutor(client, self._tools, workflow_step_id=execution.metadata.workflow_step_id)

    def _capture(self, execution, tool, selector):
        self._verify(execution)
        binding = self.configuration.binding
        workspace = self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.QA)
        context = MCPExecutionContext(binding=binding, workspace=workspace)
        sandbox = SandboxRuntime(self._repository, self._workspaces, self._artifacts)
        if tool == "run_unit_tests":
            tools = UnitTestTools(self._artifacts, sandbox, self._unit,
                configuration=self.configuration.unit_test_configuration,
                max_call_seconds=self.configuration.max_call_seconds)
            scope, profile = tools._scope_profile(AgentRole.QA, selector)
            _source, captured = tools._prepare(context, scope, execution.source.artifact_id)
            inventory = self._unit._inputs_payload(captured, scope)
            policy = self._unit._scope_payload(scope)
            host = None
        else:
            tools = BrowserTestTools(self._artifacts, sandbox, self._browser,
                configuration=self.configuration.browser_test_configuration,
                max_call_seconds=self.configuration.max_call_seconds)
            suite, profile = tools._suite_profile(selector)
            _source, captured = tools._prepare(context, suite, execution.source.artifact_id, profile)
            inventory, policy = self._browser._inputs_payload(captured, suite,
                self.configuration.browser_test_configuration)
            host = self._browser._host_policy(self.configuration.browser_test_configuration, suite)
        capture = self._inputs.stage(binding, workflow_step_id=execution.metadata.workflow_step_id,
            source_artifact_id=execution.source.artifact_id, tool_name=tool, selector=selector,
            captured=captured, inputs=inventory)
        return capture, profile, policy, host

    async def finalize(self, execution, decision, tracked, *, task_id, context_id):
        execution.budget.check()
        try:
            if (type(decision) is not QADecision or decision.kind != "READY"
                    or type(tracked) is not TrackedMCPExecutor
                    or tracked._client.configuration != self.configuration
                    or tracked._store._repository is not self._repository
                    or tracked._step_id != execution.metadata.workflow_step_id):
                raise ValueError
            # The public constructor alone does not establish coverage or
            # authorize a selector: repeat the closed local Draft validation.
            validated = validate_qa_decision({"kind": decision.kind, "cases": [_case_json(case) for case in decision.cases],
                "questions": list(decision.questions)}, execution.metadata.requirement_ids, self.selectors)
            cases = validated.cases + self._protected_cases
            pairs = set((case.tool_name, case.selector) for case in cases)
            wanted = {(tool, selector) for tool, names in self._selectors.items() for selector in names}
            # Every approved generated selector runs as well as every
            # protected selector. The model cannot omit a configured suite.
            if not pairs <= wanted | {(tool, selector) for tool, names in self._protected.items() for selector in names}:
                raise ValueError
            ordered = tuple((tool, selector) for tool in ("run_unit_tests", "run_browser_tests")
                for selector in (*self._selectors.get(tool, ()), *self._protected.get(tool, ())))
        except Exception:
            raise QAServicesError("QA_REPORT_BINDING_INVALID") from None
        measured_results = {}
        for tool, selector in ordered:
            execution.budget.check()
            try:
                capture, profile, policy, host = await _run_file_operation(self._capture, execution, tool, selector)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise QAServicesError("QA_TEST_INPUT_INVALID") from None
            execution.budget.check()
            arguments = {"workspaceId": str(execution.configuration.workspace_id),
                         "snapshotId": str(execution.source.artifact_id),
                         "testScope" if tool == "run_unit_tests" else "testSuite": selector}
            tool_sequence, timeout = execution.budget.reserve_tracked_tool_call()
            measured = await asyncio.wait_for(tracked.invoke(tool, arguments,
                deadline_monotonic=execution.budget.deadline_monotonic), timeout)
            execution.budget.account_tool_call(tool_sequence)
            execution.budget.check()
            try:
                record = await _run_file_operation(self._tools.get, self.configuration.binding,
                                                  measured.record.logical_call_id)
                store = self._unit if tool == "run_unit_tests" else self._browser
                receipt = await _run_file_operation(store.get, execution.metadata.run_id,
                                                   measured.data["executionManifestId"])
                evidence = record.to_tool_evidence()
                if (record != measured.record or measured.data != receipt.tool_output()
                        or record.workflow_step_id != execution.metadata.workflow_step_id
                        or record.role is not AgentRole.QA or record.tool_name != tool
                        or record.source_artifact_id != execution.source.artifact_id
                        or evidence.outcome is not ToolExecutionOutcome.PASS
                        or record.execution_manifest != execution.source.execution_manifest()
                        or record.selector_sha256 != sha256(selector.encode()).hexdigest()
                        or receipt.run_id != execution.metadata.run_id
                        or receipt.workflow_step_id != execution.metadata.workflow_step_id
                        or receipt.workspace_id != execution.configuration.workspace_id
                        or receipt.role is not AgentRole.QA or receipt.tool_name != tool
                        or receipt.source_artifact_id != execution.source.artifact_id
                        or receipt.execution_manifest != execution.source.execution_manifest()
                        or receipt.execution_profile != profile or receipt.profile_name != selector
                        or _plain(receipt.inputs) != _plain(capture.inputs)
                        or (tool == "run_unit_tests" and self._unit._scope_payload(receipt.scope) != policy)
                        or (tool == "run_browser_tests" and (_plain(receipt.suite) != policy
                                                            or _plain(receipt.host_configuration) != host))):
                    raise ValueError
                await _run_file_operation(self._inputs.bind_receipt, self.configuration.binding, capture.capture_id, receipt)
                measured_results[(tool, selector)] = (receipt, evidence)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise QAServicesError("QA_TEST_EVIDENCE_INVALID") from None
        execution.budget.check()
        return await _run_file_operation(self._assemble, execution, cases, measured_results, task_id, context_id)

    def _assemble(self, execution, cases, measured, task_id, context_id):
        results, consumed = [], set()
        for (tool, selector), (receipt, _evidence) in measured.items():
            bound = {case.test_id: case for case in cases if case.tool_name == tool and case.selector == selector}
            for actual in receipt.report.tests:
                if actual.test_id not in bound:
                    # Never guess a Requirement from names or discard a
                    # failing, skipped, or passing unbound execution case.
                    raise QAServicesError("QA_REPORT_BINDING_INVALID")
        try:
            for binding in cases:
                receipt, evidence = measured[(binding.tool_name, binding.selector)]
                actual = next((case for case in receipt.report.tests if case.test_id == binding.test_id), None)
                key = (binding.tool_name, binding.selector, binding.test_id)
                if key in consumed:
                    raise ValueError
                consumed.add(key)
                code = "PLANNED_CASE_NOT_REPORTED" if actual is None else "RUNNER_REPORTED_SKIP" if actual.outcome == "SKIP" else "MEASURED_" + actual.outcome
                verified = actual is not None and actual.outcome in {"PASS", "FAIL"}
                outcome = ValidationOutcome(actual.outcome) if verified else ValidationOutcome.UNVERIFIED
                results.append(QATestResult(
                    test_id=f"{binding.tool_name}:{binding.selector}:{binding.test_id}",
                    requirement_id=binding.requirement_id, outcome=outcome, title=binding.title,
                    expected_result=binding.expected_result, actual_result=code, details=code,
                    normalized_location=binding.test_id, tool_evidence=evidence if verified else None))
            if not results or {result.requirement_id for result in results} != set(execution.metadata.requirement_ids):
                raise ValueError
            self._verify(execution)
            project_id, a2a_id = uuid4(), str(uuid4())
            version = verify_report_predecessor(self._repository, execution, AgentRole.QA, QAReportArtifact)
            previous = getattr(execution, "previous_report", None)
            report = QAReportArtifact(artifact_id=project_id, artifact_version=version,
                previous_artifact_id=None if previous is None else previous.artifact_id,
                run_id=execution.metadata.run_id, workflow_step_id=execution.metadata.workflow_step_id,
                a2a_task_id=task_id, a2a_artifact_id=a2a_id,
                requirement_ids=execution.metadata.requirement_ids, code_version=execution.source.code_version,
                execution_manifest=execution.source.execution_manifest(), tests=tuple(results))
            payload = report.model_dump(mode="json", by_alias=True)
            sanitize_content(payload, reject_secrets=True)
            payload = parse_json(json_text(payload, max_bytes=1_048_576), max_bytes=1_048_576)
            schemas, registry = _schemas()
            Draft202012Validator(schemas["qa_report.schema.json"], registry=registry,
                                 format_checker=FormatChecker()).validate(payload)
            artifact = Artifact(artifact_id=a2a_id, name="qa-report.json",
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
                agent_role=AgentRole.QA, status=WorkflowStepStatus.SUCCEEDED,
                a2a_task_id=task_id, agent_context_id=context_id, a2a_task_state=A2ATaskState.COMPLETED,
                attempt=execution.metadata.attempt, code_version=execution.source.code_version, requirement_ids=list(report.requirement_ids),
                input_artifact_ids=list(execution.metadata.project_artifact_ids or ()))
            validate_completed_role_output(AgentRole.QA, task=task, run=run, step=step, source=execution.source)
            return (artifact,)
        except Exception:
            raise QAServicesError("QA_ARTIFACT_INVALID") from None
