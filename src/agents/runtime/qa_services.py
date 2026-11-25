"""Host-owned immutable QA inputs, measured tests, and QA Artifact assembly.

The model supplies case bindings only. All executions use approved selectors
and the exact frozen Source. Missing receipts never become test results.
"""

import asyncio
import json
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
from agents.runtime.requirement_scope import validate_generated_case_expectations
from agents.runtime.test_fix import TestFixRequired, technical_reasons
from agents.runtime.generated_revalidation import run_previous_scopes
from mcp_tools.tools.generated_recheck import completed_count as completed_generated_rechecks
from agents.runtime.protected_scopes import (
    claim_protected_scope, commit_protected_scope, completed_protected_scopes, load_protected_receipt,
)
from agents.runtime.evaluation_policy_store import EvaluationPolicyStore, qa_policy_sha256
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, open_mcp_client
from mcp_tools.execution_runtime import TrackedMCPError, TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPExecutionContext
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.tools.snapshots import SnapshotReader
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import test_not_run
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.validation_artifacts import QAReportArtifact, QATestResult, ValidationOutcome
from orchestrator.domain.validation_feedback import case_feedback, measured_test_version
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry


class QAServicesError(ValueError):
    def __init__(self, code="QA_SERVICES_INVALID"):
        self.code = code if code in {
            "QA_SERVICES_INVALID", "QA_TEST_INPUT_INVALID", "QA_TEST_EVIDENCE_INVALID",
            "QA_REPORT_BINDING_INVALID", "QA_ARTIFACT_INVALID", "QA_TEST_FIX_INVALID",
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


def _unobserved_concurrency(contract, cases, results, selectors):
    """Evaluate generated cases independently of protected siblings and their syntax."""
    if contract.get('scenarioKey') != 'SCN-001':
        return set()
    requirements = {r['requirementId'] for r in contract['requirements'] if r['key'] == 'REQ-003'}
    passed = set()
    for case in cases:
        if str(case.requirement_id) not in requirements or case.selector not in selectors.get(case.tool_name, ()):
            continue
        key = (case.tool_name, case.selector)
        if key not in results:
            continue
        observed = next((r for r in results[key][0].report.tests if r.test_id == case.test_id), None)
        if observed is None or observed.outcome != 'PASS':
            continue
        facts = json.loads(getattr(observed, 'signup_checks_json', None) or '{}')
        if 'concurrentCanonicalSignup' in facts:
            return set()
        passed.add((*key, case.test_id))
    return passed


class QARuntimeServices:
    """Explicit one-Run capability; construction performs no I/O.

    Protected selector bindings are trusted Host policy. They are never shown
    as model-selectable scopes. The Host journals their receipts before the draft.
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
        self._evaluation_policies = EvaluationPolicyStore(repository)
        self._selectors, self._protected, self._protected_cases = selected, protected, approved

    def __repr__(self):
        return "QARuntimeServices()"

    @property
    def selectors(self):
        return dict(self._selectors)

    def refresh_output(self, output):
        from agents.runtime.qa_catalog import refresh_qa_output
        binding = self.configuration.binding
        workspace = self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.QA)
        unit = self.configuration.unit_test_configuration
        scopes = [(s.name, s.pattern, getattr(s, 'source_directory', 'tests')) for s in (() if unit is None else unit.scopes) if s.kind == 'QA_TESTS']
        browser = self.configuration.browser_test_configuration
        return refresh_qa_output(output, Path(workspace.record.root_path) / 'outputs' / 'qa' / 'tests',
            scopes, snapshot_sha256=self.configuration.frozen_source.snapshot_sha256,
            unit_only=set(self.selectors) == {'run_unit_tests'},
            browser_suites=[(s.name, s.suite_path) for s in (() if browser is None else browser.suites)
                            if s.kind == 'QA_TESTS'])

    def generated_test_issues(self, data):
        """Read-only pre-READY check of the files ``finalize`` will collect (handoff P5, points 4·6).

        Names a missing generated test file or a test the runner would report but
        the draft did not declare; never imports or runs the files.
        """
        from agents.runtime.generated_tests_precheck import inspect_generated_browser_tests, inspect_generated_tests
        binding = self.configuration.binding
        workspace = self._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.QA)
        unit = self.configuration.unit_test_configuration
        patterns = {scope.name: (scope.pattern, getattr(scope, 'source_directory', 'tests')) for scope in (() if unit is None else unit.scopes)
                    if getattr(scope, "kind", None) == "QA_TESTS"}
        scopes = [(name, *patterns[name]) for name in self._selectors.get("run_unit_tests", ()) if name in patterns]
        rows = [row for row in (data.get("cases") or []) if isinstance(row, dict)
                and row.get("toolName") == "run_unit_tests"] if isinstance(data, dict) else []
        source = Path(workspace.record.root_path) / "source"
        from orchestrator.domain import SCN_001_ID
        run = self._repository.get_run(binding.run_id)
        output_root = Path(workspace.record.root_path) / "outputs" / "qa"
        issues = inspect_generated_tests(output_root / "tests", scopes, rows, "cases",
                                        source_root=source if source.is_dir() else None,
                                        host_app_fixture=run is not None and run.scenario_id == SCN_001_ID)
        browser = self.configuration.browser_test_configuration
        suites = [(suite.name, suite.suite_path) for suite in (() if browser is None else browser.suites)
                  if suite.kind == "QA_TESTS" and suite.name in self._selectors.get("run_browser_tests", ())]
        cases = (data.get("cases") or []) if isinstance(data, dict) else []
        for item in inspect_generated_browser_tests(output_root, suites, cases, "cases"):
            if item not in issues:
                issues.append(item)
        return issues

    async def prepare(self, execution):
        execution.budget.check()
        try:
            await _run_file_operation(self._verify, execution)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QAServicesError() from None
        execution.budget.check()

    async def source_interface(self, execution):
        if execution.configuration.scenario_contract.get('scenarioKey') != 'SERVICE-WEB':
            return {}
        execution.budget.check()
        def read():
            self._verify(execution)
            source, files = SnapshotReader(self._artifacts)._load(
                self.configuration.binding, self.configuration.frozen_source)
            from agents.runtime.service_interface import service_source_facts
            return {'projectArtifactId': str(source.artifact_id), 'snapshotSha256': source.snapshot_sha256,
                    **service_source_facts(files)}
        try:
            result = await _run_file_operation(read)
            execution.budget.check()
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QAServicesError() from None

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
        self._evaluation_policies.pin(binding, workflow_step_id=execution.metadata.workflow_step_id,
            source_artifact_id=source.artifact_id,
            policy_sha256=qa_policy_sha256(self.configuration, self._protected_cases))

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

    def test_fix_available(self, execution) -> bool:
        """An early protected measurement does not consume the generated-test repair."""
        step = execution.metadata.workflow_step_id
        calls = self._tools.count_step_executions(self.configuration.binding, step, ("run_unit_tests", "run_browser_tests"))
        protected = completed_protected_scopes(self._repository, run_id=execution.metadata.run_id,
                                              workflow_step_id=step, source_artifact_id=execution.source.artifact_id)
        return calls == len(protected) + completed_generated_rechecks(self._repository, step)

    async def run_protected(self, execution, tracked):
        """Measure Host-approved scopes once before any generated draft, without exposing results to the model."""
        scopes = tuple((tool, name) for tool, names in self._protected.items() for name in names)
        if scopes:
            self._check_tracked(tracked, execution)
            await self._measure_scopes(execution, tracked, scopes)

        async def measure(selector):
            self._check_tracked(tracked, execution)
            key = ('run_unit_tests', selector)
            results, _not_run = await self._measure_scopes(execution, tracked, (key,))
            return results.get(key)

        await run_previous_scopes(self, execution, tracked, self._selectors.get('run_unit_tests', ()), measure)

    def _check_tracked(self, tracked, execution):
        if (type(tracked) is not TrackedMCPExecutor or tracked._client.configuration != self.configuration
                or tracked._store._repository is not self._repository
                or tracked._step_id != execution.metadata.workflow_step_id):
            raise QAServicesError("QA_REPORT_BINDING_INVALID")

    async def finalize(self, execution, decision, tracked, *, task_id, context_id, test_fix_available=False):
        execution.budget.check()
        try:
            self._check_tracked(tracked, execution)
            if type(decision) is not QADecision or decision.kind != "READY":
                raise ValueError
            # The public constructor alone does not establish coverage or
            # authorize a selector: repeat the closed local Draft validation.
            validated = validate_qa_decision({"kind": decision.kind, "cases": [_case_json(case) for case in decision.cases],
                "questions": list(decision.questions)}, execution.metadata.requirement_ids, self.selectors)
            validate_generated_case_expectations(validated.cases, configuration=execution.configuration,
                metadata=execution.metadata, role=AgentRole.QA)
            cases = validated.cases + self._protected_cases
            pairs = set((case.tool_name, case.selector) for case in cases)
            wanted = {(tool, selector) for tool, names in self._selectors.items() for selector in names}
            # Every approved generated selector runs as well as every
            # protected selector. The model cannot omit a configured suite.
            if not pairs <= wanted | {(tool, selector) for tool, names in self._protected.items() for selector in names}:
                raise ValueError
            ordered = tuple((tool, selector) for tool in ("run_unit_tests", "run_browser_tests")
                for selector in (*self._protected.get(tool, ()), *self._selectors.get(tool, ())))
        except Exception:
            raise QAServicesError("QA_REPORT_BINDING_INVALID") from None
        measured_results, not_run = await self._measure_scopes(execution, tracked, ordered)
        execution.budget.check()
        incomplete = _unobserved_concurrency(execution.configuration.scenario_contract, cases,
                                              measured_results, self._selectors)
        if test_fix_available:
            # Generated tests that could not run get one in-step fix before any report exists.
            generated = [(tool, selector) for tool, selector in ordered if selector in self._selectors.get(tool, ())]
            reasons = technical_reasons([key for key in not_run if key in generated], [
                actual for key in generated if key in measured_results for actual in measured_results[key][0].report.tests])
            # Bound browser-only cases cannot use the unit runner observation during a same-case rewrite.
            if any(tool == 'run_unit_tests' for tool, _selector, _test in incomplete):
                reasons.add('CONCURRENT_SIGNUPS_NOT_OBSERVED')
            if reasons:
                from agents.runtime.test_fix import generated_test_feedback
                raise TestFixRequired(reasons, [selector for _tool, selector in generated],
                                      observed_cases=generated_test_feedback(measured_results, generated))
        return await _run_file_operation(self._assemble, execution, cases, measured_results, task_id, context_id,
                                         not_run, incomplete)

    async def _measure_scopes(self, execution, tracked, ordered):
        measured_results, not_run = {}, {}
        for tool, selector in ordered:
            execution.budget.check()
            try:
                protected = selector in self._protected.get(tool, ())
                if protected:
                    await _run_file_operation(self._verify, execution)
                    saved = await _run_file_operation(claim_protected_scope, self._repository,
                        self.configuration.binding, execution.metadata.workflow_step_id, execution.source.artifact_id,
                        tool, selector)
                    if saved is not None:
                        measured_results[(tool, selector)] = await _run_file_operation(load_protected_receipt,
                            self._repository, self.configuration.binding, execution, saved)
                        continue
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
            try:
                measured = await asyncio.wait_for(tracked.invoke(tool, arguments,
                    deadline_monotonic=execution.budget.deadline_monotonic), timeout)
            except TrackedMCPError as error:
                # docs/live-api/05 3절: a generated scope whose runner could not
                # run leaves its declared cases not-run with the Host's attempt
                # evidence; independent scopes go on. A protected scope, any other
                # failure class, or evidence that does not reconcile still stops.
                if (selector in self._protected.get(tool, ()) or error.error_kind != "TEST_RUNNER_ERROR"
                        or error.logical_call_id is None):
                    raise
                execution.budget.account_tool_call(tool_sequence)
                execution.budget.check()
                not_run[(tool, selector)] = await _run_file_operation(
                    self._not_run_evidence, execution, tool, selector, error.logical_call_id)
                continue
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
                if protected:
                    await _run_file_operation(commit_protected_scope, self._repository,
                        execution.metadata.workflow_step_id, selector, record, receipt)
                measured_results[(tool, selector)] = (receipt, evidence)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise QAServicesError("QA_TEST_EVIDENCE_INVALID") from None
        return measured_results, not_run

    def _not_run_evidence(self, execution, tool, selector, logical_call_id):
        """Host evidence that a generated scope was attempted and could not run."""
        try:
            record = self._tools.get(self.configuration.binding, logical_call_id)
            evidence = record.to_tool_evidence()
            if (record.workflow_step_id != execution.metadata.workflow_step_id
                    or record.role is not AgentRole.QA or record.tool_name != tool
                    or record.source_artifact_id != execution.source.artifact_id
                    or record.execution_manifest != execution.source.execution_manifest()
                    or record.selector_sha256 != sha256(selector.encode()).hexdigest()
                    or evidence.outcome is ToolExecutionOutcome.PASS):
                raise ValueError
            return evidence
        except Exception:
            raise QAServicesError("QA_TEST_EVIDENCE_INVALID") from None

    def _assemble(self, execution, cases, measured, task_id, context_id, not_run=None, incomplete=()):
        not_run = not_run or {}
        results, consumed = [], set()
        for (tool, selector), (receipt, _evidence) in measured.items():
            bound = {case.test_id: case for case in cases if case.tool_name == tool and case.selector == selector}
            for actual in receipt.report.tests:
                if actual.test_id not in bound and not test_not_run.is_fixture_row(actual.test_id):
                    # Never guess a Requirement from names or discard a
                    # failing, skipped, or passing unbound execution case.
                    raise QAServicesError("QA_REPORT_BINDING_INVALID")
        try:
            for binding in cases:
                key = (binding.tool_name, binding.selector, binding.test_id)
                if key in consumed:
                    raise ValueError
                consumed.add(key)
                if (binding.tool_name, binding.selector) in not_run:
                    # Declared but its scope could not run: kept, not PASS, with attempt evidence.
                    evidence = not_run[(binding.tool_name, binding.selector)]
                    results.append(QATestResult(
                        test_id=f"{binding.tool_name}:{binding.selector}:{binding.test_id}",
                        requirement_id=binding.requirement_id, outcome=ValidationOutcome.UNVERIFIED,
                        title=binding.title, expected_result=binding.expected_result,
                        actual_result=test_not_run.SCOPE_FAILED,
                        details=json_text({'code': test_not_run.SCOPE_FAILED, 'evidenceRefs': [evidence.evidence_ref]}),
                        normalized_location=binding.test_id, tool_evidence=evidence, execution_evidence=(evidence,)))
                    continue
                receipt, evidence = measured[(binding.tool_name, binding.selector)]
                actual = next((case for case in receipt.report.tests if case.test_id == binding.test_id), None)
                reason = test_not_run.runner_reason(actual)
                code = ("PLANNED_CASE_NOT_REPORTED" if actual is None else reason if reason is not None
                        else "RUNNER_REPORTED_SKIP" if actual.outcome == "SKIP" else "MEASURED_" + actual.outcome)
                verified = actual is not None and actual.outcome in {"PASS", "FAIL"}
                outcome = ValidationOutcome(actual.outcome) if verified else ValidationOutcome.UNVERIFIED
                facts = json.loads(getattr(actual, 'signup_checks_json', None) or '{}')
                if facts.get('concurrentCanonicalSignup') is False:
                    verified, outcome, code = True, ValidationOutcome.FAIL, 'MEASURED_CONCURRENT_SIGNUP_FAILURE'
                detail = code
                if actual is not None and hasattr(actual, 'steps') and actual.outcome == 'FAIL':
                    # Browser receipts contain closed action/result coordinates, not DOM or input values.
                    failed_step = next((s for s in actual.steps if s.outcome == 'FAIL'), None)
                    browser_reason = getattr(actual, 'details', None)
                    if browser_reason in {'ACTION_TIMEOUT', 'ASSERTION_FAILED', 'ACTION_FAILED'}:
                        code = 'BROWSER_' + browser_reason
                        detail = json_text({'code': code, 'stepIndex': None if failed_step is None else failed_step.index,
                            'action': None if failed_step is None else failed_step.action,
                            'evidenceRefs': [evidence.evidence_ref], **measured_test_version(receipt)})
                if actual is not None and hasattr(actual, 'observations_json'):
                    detail = json_text({'code': code, 'evidenceRefs': [evidence.evidence_ref],
                                        **measured_test_version(receipt), **case_feedback(actual)})
                if key in incomplete:
                    verified, outcome, code = False, ValidationOutcome.UNVERIFIED, 'CONCURRENT_SIGNUPS_NOT_OBSERVED'
                    detail = json_text({'code': code, 'runnerOutcome': actual.outcome,
                                        'sourceAssertionScope': 'CONCURRENT_SIGNUPS_NOT_OBSERVED',
                                        'evidenceRefs': [evidence.evidence_ref], **measured_test_version(receipt)})
                results.append(QATestResult(
                    test_id=f"{binding.tool_name}:{binding.selector}:{binding.test_id}",
                    requirement_id=binding.requirement_id, outcome=outcome, title=binding.title,
                    expected_result=binding.expected_result, actual_result=code, details=detail,
                    normalized_location=binding.test_id,
                    tool_evidence=evidence if verified or reason in test_not_run.TECHNICAL else None,
                    execution_evidence=(evidence,)))
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
