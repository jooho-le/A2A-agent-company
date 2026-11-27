"""Real Developer Fix handoff/Git/SQLite/HTTP, fake LLM and Docker transport.

Only inert fixture files are changed; no generated code executes on the Host.
Docker/LLM production execution and whole-project success are not claimed.
"""

from dataclasses import replace
from hashlib import sha256
import json
from copy import deepcopy
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import Task
from google.protobuf.json_format import ParseDict

from agents.runtime.developer_context import DeveloperContextError, DeveloperExecutionContext
from agents.runtime.developer_services import DeveloperServicesError
from agents.runtime.developer_workspace import DeveloperCheckpointError
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.domain import AgentContext, AgentRole, TraceEvent, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.states import A2ATaskState
from orchestrator.sandbox.contracts import CLIResult
import test_developer_agent as initial
from test_llm_runtime import FakeProvider


class DeveloperFixTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture helpers, not its TestCase inheritance/test methods.
    setUp = initial.DeveloperAgentTests.setUp
    git = initial.DeveloperAgentTests.git
    peer_client = initial.DeveloperAgentTests.peer_client
    context_factory = initial.DeveloperAgentTests.context_factory
    services_factory = initial.DeveloperAgentTests.services_factory
    executor = initial.DeveloperAgentTests.executor
    headers = staticmethod(initial.DeveloperAgentTests.headers)
    client_for = initial.DeveloperAgentTests.client_for
    send = initial.DeveloperAgentTests.send
    poll = initial.DeveloperAgentTests.poll
    draft_response = initial.DeveloperAgentTests.draft_response
    write_response = initial.DeveloperAgentTests.write_response
    ready_provider = initial.DeveloperAgentTests.ready_provider
    run_to = initial.DeveloperAgentTests.run_to
    parse_completed = initial.DeveloperAgentTests.parse_completed
    tool_calls = initial.DeveloperAgentTests.tool_calls
    status_code = initial.DeveloperAgentTests.status_code

    def payload(self):
        if self.run.status is not WorkflowStatus.FIXING:
            return initial.DeveloperAgentTests.payload(self)
        issues = [issue for issue in self.repository.list_issue_records(self.run.run_id)
                  if issue.fix_workflow_step_id == self.step.workflow_step_id]
        artifacts = [item for item in self.repository.list_project_artifacts(self.run.run_id)
                     if item.artifact_id in self.step.input_artifact_ids]
        data = PlannerRunDispatcher.build_fix_payload(self.run, self.step, self.plan, self.scenario, issues, artifacts)
        data["runConfiguration"] = self.configuration.to_artifact_json()
        return data

    def wire(self, *, payload=None, metadata=None, task_id=None, context_id=None):
        if context_id is None and self.run.status is WorkflowStatus.FIXING:
            context_id = self.developer_context_id
        return initial.DeveloperAgentTests.wire(self, payload=payload, metadata=metadata,
                                                task_id=task_id, context_id=context_id)

    def request(self, *, task_id="new-fix-task", context_id=None, metadata=None):
        context_id = self.developer_context_id if context_id is None else context_id
        return RequestContext(call_context=ServerCallContext(state={}),
            request=build_send_message_request(self.payload(), self.metadata if metadata is None else metadata,
                                                context_id=context_id),
            task_id=task_id, context_id=context_id)

    def update(self, *, step=None, run=None):
        with self.repository._transaction() as connection:
            if step is not None:
                connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                    (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))
            if run is not None:
                connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                    (run.status.value, run.model_dump_json(), str(run.run_id)))

    def observe_completed(self, task, output):
        parsed = ParseDict(task, Task())
        step = self.step.model_copy(update={"status": WorkflowStepStatus.SUCCEEDED,
            "a2a_task_state": A2ATaskState.COMPLETED, "a2a_task_id": parsed.id,
            "agent_context_id": parsed.context_id, "a2a_artifact_ids": [item.artifact_id for item in parsed.artifacts],
            "output_artifact_ids": [output.source.artifact_id, output.change_report.artifact_id, output.build_report.artifact_id]})
        self.repository.save_task_update(self.run, step,
            AgentContext(run_id=self.run.run_id, agent_id="developer", agent_context_id=parsed.context_id,
                         latest_a2a_task_id=parsed.id),
            TraceEvent(run_id=self.run.run_id, workflow_step_id=step.workflow_step_id,
                       a2a_task_id=parsed.id, event_type="A2A_TASK_COMPLETED", actor="Developer",
                       attempt=step.attempt, workflow_state=self.run.status))
        self.developer_context_id = parsed.context_id

    def start_fix(self, task, output):
        self.observe_completed(task, output)
        issue = PlannerRunDispatcher._build_issue(self.run, output.source, output.build_report)
        failed, _developer, _validators = self.repository.record_developer_candidate(
            self.run.run_id, self.step.workflow_step_id, source=output.source,
            change_report=output.change_report, build_report=output.build_report,
            validation_agents_configured=True, detected_issues=(issue,))
        self.assertEqual(failed.status, WorkflowStatus.FIX_REQUIRED)
        self.run, self.step, self.fix_issues, _inputs = self.repository.start_fix_cycle(
            failed.run_id, (issue,), developer_configured=True)
        self.assertEqual(self.run.status, WorkflowStatus.FIXING)
        self.metadata = A2AWorkflowMetadata(run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=self.step.attempt,
            requirement_ids=tuple(self.step.requirement_ids), code_version=self.step.code_version,
            project_artifact_ids=tuple(self.step.input_artifact_ids))
        self.previous = output
        self.previous_task = task

    async def prepare_fix(self):
        self.docker.exit_code = 2
        self.docker.start_result = CLIResult(returncode=2, stdout=b"", stderr=b"inert candidate failure\n")
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        self.start_fix(task, self.parse_completed(task))
        self.context_calls.clear()
        self.service_calls.clear()
        self.sessions.clear()

    def context_copy(self, context, **changes):
        return replace(context, **changes)

    async def test_fix_loader_exact_stored_payload_readonly_existing_budget_and_distinct_task(self):
        await self.prepare_fix()
        before = self.repository.database_path.read_bytes()
        deadline, calls = self.budget.deadline_monotonic, self.budget.model_calls
        context = self.loader(self.request())
        self.assertEqual(self.repository.database_path.read_bytes(), before)
        self.assertIs(context.budget, self.budget)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls, calls)
        self.assertEqual(context.fix_attempt, 1)
        self.assertEqual(context.metadata.code_version, 2)
        self.assertEqual(context.previous_source, self.previous.source)
        self.assertEqual(context.initial_payload, self.payload())
        self.assertEqual({item.artifact_type for item in context.previous_artifacts}, {"SOURCE", "CHANGE_REPORT", "BUILD_REPORT"})
        self.assertEqual(context.fix_issues[0].fix_workflow_step_id, self.step.workflow_step_id)
        self.assertNotIn(str(self.source), repr(context))

    async def test_missing_product_table_goes_to_developer_fix_with_bound_diagnostic(self):
        task, output = await initial.DeveloperAgentTests.check_start_contract_failure(
            self, 'SCN001_USER_TABLE_REQUIRED', 'table initialization', 'main.py')
        self.start_fix(task, output)
        context = self.loader(self.request())
        self.assertEqual(self.run.status, WorkflowStatus.FIXING)
        self.assertEqual(context.fix_attempt, 1)
        payload = json.dumps(context.initial_payload)
        self.assertIn('SCN001_USER_TABLE_REQUIRED', payload)
        self.assertIn('table initialization', payload)
        self.assertIn('do not move product table creation into QA or Security tests', payload)
        self.assertNotIn('untrusted-debug-output', payload)

    async def test_fix_context_inert_detached_payload_and_three_cycle_hard_limit(self):
        await self.prepare_fix()
        context = self.loader(self.request())
        with patch.object(self.repository, "_connection", side_effect=AssertionError("constructor IO")):
            copied = self.context_copy(context)
        copied.initial_payload["fixRequest"]["issues"][0]["title"] = "changed copy"
        self.assertNotEqual(copied.initial_payload["fixRequest"]["issues"][0]["title"], "changed copy")
        for value in (True, -1, 0, 4, "1"):
            with self.subTest(value=value), self.assertRaises(DeveloperContextError):
                self.context_copy(context, fix_attempt=value)
        with self.assertRaises(DeveloperContextError):
            self.context_copy(context, metadata=context.metadata.model_copy(update={"code_version": 3}))

    async def test_missing_issue_wrong_candidate_report_or_step_and_unknown_artifacts_denied(self):
        await self.prepare_fix()
        context = self.loader(self.request())
        variants = ({"fix_issues": ()}, {"previous_artifacts": context.previous_artifacts[:-1]},
            {"previous_source": None}, {"previous_artifacts": (*context.previous_artifacts, context.previous_artifacts[0])},
            {"fix_issues": (context.fix_issues[0].model_copy(update={"fix_workflow_step_id": uuid4()}),)},
            {"fix_issues": (context.fix_issues[0].model_copy(update={"source_artifact_id": uuid4()}),)},
            {"fix_issues": (context.fix_issues[0].model_copy(update={"report_artifact_id": uuid4()}),)},
            {"fix_issues": (context.fix_issues[0].model_copy(update={"consecutive_repeat_count": 2}),)})
        for changes in variants:
            with self.subTest(fields=list(changes)), self.assertRaises(DeveloperContextError):
                self.context_copy(context, **changes)

    async def test_fix_continuation_attempt_does_not_increment_code_revision_or_budget(self):
        await self.prepare_fix()
        resumed = self.step.model_copy(update={"attempt": 7})
        self.update(step=resumed)
        metadata = self.metadata.model_copy(update={"attempt": 7})
        context = self.loader(self.request(metadata=metadata))
        self.assertEqual((context.metadata.attempt, context.fix_attempt, context.metadata.code_version), (7, 1, 2))
        self.assertIs(context.budget, self.budget)
        with self.assertRaises(DeveloperContextError):
            self.loader(self.request())

    async def test_fix_requires_same_opaque_developer_context_and_new_task_identity(self):
        await self.prepare_fix()
        for request in (self.request(task_id=self.previous_task["id"]), self.request(context_id="foreign-context")):
            with self.subTest(task_id=request.task_id), self.assertRaises(DeveloperContextError):
                self.loader(request)
        with self.repository._transaction() as connection:
            connection.execute("DELETE FROM agent_contexts WHERE run_id=? AND agent_id='developer'", (str(self.run.run_id),))
        with self.assertRaises(DeveloperContextError):
            self.loader(self.request())

    async def test_sent_fix_message_unobserved_task_gap_retains_context_without_denial(self):
        await self.prepare_fix()
        def update_mapping(task_id):
            mapping = AgentContext(run_id=self.run.run_id, agent_id="developer",
                agent_context_id=self.developer_context_id, latest_a2a_task_id=task_id)
            with self.repository._transaction() as connection:
                connection.execute("UPDATE agent_contexts SET payload_json=? WHERE run_id=? AND agent_id='developer'",
                    (mapping.model_dump_json(), str(self.run.run_id)))
        update_mapping(None)
        admitted = self.loader(self.request())
        self.assertEqual(admitted.fix_attempt, 1)
        self.assertIs(admitted.budget, self.budget)
        update_mapping("foreign-task")
        with self.assertRaises(DeveloperContextError):
            self.loader(self.request())
        update_mapping(None)
        self.update(step=self.step.model_copy(update={"a2a_task_id": "new-fix-task",
                                                      "agent_context_id": self.developer_context_id}))
        with self.assertRaises(DeveloperContextError):
            self.loader(self.request())
        update_mapping("new-fix-task")
        self.assertEqual(self.loader(self.request()).fix_attempt, 1)

    async def test_wrong_fix_step_input_order_and_missing_reference_are_rejected(self):
        await self.prepare_fix()
        for inputs in (list(reversed(self.step.input_artifact_ids)), self.step.input_artifact_ids[:-1],
                       [*self.step.input_artifact_ids, uuid4()]):
            changed = self.step.model_copy(update={"input_artifact_ids": inputs})
            self.update(step=changed)
            with self.subTest(inputs=len(inputs)), self.assertRaises(DeveloperContextError):
                self.loader(self.request(metadata=self.metadata.model_copy(update={"project_artifact_ids": tuple(inputs)})))

    async def test_unrelated_working_edits_are_not_adopted_or_replayed_before_model(self):
        await self.prepare_fix()
        (self.source / "unrelated.py").write_bytes(b"unrelated fixture edit\n")
        provider = self.ready_provider(content="def signup():\n    return 'fix'\n")
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.tool_calls(), [])
        self.assertEqual((self.source / "unrelated.py").read_bytes(), b"unrelated fixture edit\n")

    async def test_previous_source_bytes_and_changed_requirements_are_not_client_authority(self):
        await self.prepare_fix()
        mutations = []
        data = self.payload()
        data["fixRequest"]["issues"][0]["title"] = "client-controlled repair"
        mutations.append(data)
        data = self.payload()
        data["fixRequest"]["inputArtifacts"][0]["record"]["snapshotSha256"] = "e" * 64
        mutations.append(data)
        data = self.payload()
        data["plan"]["requirements"][0]["acceptanceCriteria"] = ["weakened rule"]
        mutations.append(data)
        for data in mutations:
            provider = self.ready_provider(content="def signup():\n    return 'fix'\n")
            task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=data))
            self.assertEqual(self.status_code(task), "DEVELOPER_INPUT_INVALID")
            self.assertEqual(provider.requests, [])
            self.assertFalse(task.get("artifacts"))

    async def test_path_account_survives_a2a_and_is_separate_from_build_evidence(self):
        from agents.runtime.repair_baseline import with_repair_baseline
        from agents.llm.budget import ExecutionBudget
        await self.prepare_fix()
        self.budget = ExecutionBudget(runtime_budget_ms=60000, limits=self.budget.limits.model_copy(
            update={'max_output_corrections': 1}))
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=b"", stderr=b"")
        paths = ["EMITTED_APPLICATION_LOG_OR_TRACEBACK", "RESPONSE_BEFORE_SIGNUP_HANDLER"]
        def projected(request, checkpoint):
            result = with_repair_baseline(request, checkpoint)
            result["activeRepairFocus"]["brief"]["readyReportPaths"] = paths
            return result
        rows = {paths[0]: {"edit": None, "sourceRef": None, "recheck": "Review logs."},
                paths[1]: {"edit": "Changed the response builder.", "sourceRef": "source/main.py::invalid_request",
                           "recheck": "Review the response."}}
        source = ("from fastapi import FastAPI\nfrom fastapi.exceptions import RequestValidationError\n"
                  "app = FastAPI()\n@app.exception_handler(RequestValidationError)\n"
                  "def invalid_request(request, exc):\n    return 'fixture'\n")
        overclaim = deepcopy(rows)
        overclaim[paths[0]] = {**rows[paths[1]], "recheck": "Review logs."}
        provider = FakeProvider(self.write_response(content=source, path="source/main.py"),
                                self.draft_response(repairPaths=overclaim),
                                self.draft_response(repairPaths=rows))
        with (patch("agents.runtime.repair_baseline.with_repair_baseline", projected),
              patch("agents.runtime.developer.start_precheck_for", return_value=None)):
            task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        feedback = json.loads(json.loads(provider.requests[-1].input_items_json)[-1]['content'])['hostCorrection']
        self.assertEqual(feedback['code'], 'READY_PATH_EVIDENCE_MISMATCH')
        [issue] = feedback['issues']
        self.assertEqual((issue['fieldPath'], issue['reasonCode'], issue['candidateRefs'], issue['action']),
                         ('/repairPaths/' + paths[0] + '/sourceRef', 'NO_LINKED_CODE_CHANGE', [], 'EDIT_SOURCE_OR_REPORT_OPEN'))
        report = self.parse_completed(task).change_report.ready_review
        self.assertEqual(report.build_outcome, 'PASS')
        self.assertEqual(report.reported_path_edits[1].source_ref, 'source/main.py::invalid_request')
        self.assertEqual(report.validation_status, "PENDING_QA_SECURITY")
        self.assertEqual([r.host_diff_status for r in report.reported_path_edits],
                         ["NO_CHANGE_CLAIMED", "LINKED_PENDING_TEST"])
        self.assertEqual([(r.path, r.edit) for r in report.reported_path_edits],
                         [(p, rows[p]["edit"]) for p in paths])

    async def priority_candidate(self, mode):
        from agents.llm.budget import ExecutionBudget
        from agents.runtime.ready_path_diff import INPUT
        from agents.runtime.repair_baseline import with_repair_baseline
        await self.prepare_fix()
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=b"", stderr=b"")
        self.budget = ExecutionBudget(runtime_budget_ms=60000, limits=self.budget.limits.model_copy(
            update={'max_output_corrections': 2, 'max_model_calls': 2 if mode == 'last_call' else 8}))

        def projected(request, checkpoint):
            result = with_repair_baseline(request, checkpoint)
            result["activeRepairFocus"]["brief"].update(readyReportPaths=[INPUT], priorityRepairPaths=[INPUT])
            return result

        partial = {INPUT: {'edit': None, 'sourceRef': None, 'recheck': 'Recheck validation output.'}}
        source = 'def unrelated():\n    return 1\n'
        ready = self.draft_response(repairPaths=partial)
        responses = [self.write_response(content=source, path='source/main.py'), ready]
        if mode == 'edit':
            source += ('from fastapi import FastAPI\nfrom fastapi.exceptions import RequestValidationError\n'
                       'app = FastAPI()\n@app.exception_handler(RequestValidationError)\n'
                       'def invalid_request(request, exc):\n    return None\n')
            # The first scripted write creates main.py. Bind the next write to
            # those actual bytes, rather than precomputing an absence-only call.
            responses += [lambda _request: self.write_response(content=source, path='source/main.py', call_id='write-priority'),
                          self.draft_response(repairPaths={INPUT: {'edit': 'Changed validation output.',
                              'sourceRef': 'source/main.py::invalid_request', 'recheck': 'Measure again.'}})]
        if mode == 'partial':
            responses.append(ready)
        provider = FakeProvider(*responses)
        with (patch('agents.runtime.repair_baseline.with_repair_baseline', projected),
              patch('agents.runtime.developer.start_precheck_for', return_value=None)):
            task, _ = await self.run_to(provider, 'TASK_STATE_COMPLETED')
        output = self.parse_completed(task)
        self.assertNotEqual(output.source.snapshot_sha256, self.previous.source.snapshot_sha256)
        self.assertEqual(output.build_report.execution_manifest, output.source.execution_manifest())
        review = output.change_report.ready_review
        self.assertEqual((review.build_outcome, review.validation_status), ('PASS', 'PENDING_QA_SECURITY'))
        self.assertEqual(review.reported_path_edits[0].host_diff_status,
                         'LINKED_PENDING_TEST' if mode == 'edit' else 'NO_CHANGE_CLAIMED')
        self.assertEqual(len(provider.requests), len(responses))
        if mode != 'last_call':
            correction = json.loads(json.loads(provider.requests[2].input_items_json)[-1]['content'])['hostCorrection']
            self.assertEqual(correction['code'], 'READY_PRIORITY_REPAIR_NEEDED')
            self.assertEqual(correction['issues'][0]['fieldPath'], '/repairPaths/' + INPUT)

    async def test_stalled_priority_gets_an_edit_before_build(self):
        await self.priority_candidate('edit')

    async def test_truthful_partial_is_built_after_one_priority_opportunity(self):
        await self.priority_candidate('partial')

    async def test_truthful_partial_on_last_call_is_built_without_regeneration(self):
        await self.priority_candidate('last_call')

    async def test_shared_response_edit_is_recorded_for_both_observed_paths(self):
        from agents.runtime.ready_path_diff import DATABASE, INPUT
        from agents.runtime.repair_baseline import with_repair_baseline
        await self.prepare_fix()
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=b"", stderr=b"")
        selected = [INPUT, DATABASE[0]]

        def projected(request, checkpoint):
            result = with_repair_baseline(request, checkpoint)
            result["activeRepairFocus"]["brief"]["readyReportPaths"] = selected
            return result

        source = """from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
app = FastAPI()
def public_error():
    return 'fixture'
@app.exception_handler(RequestValidationError)
def invalid(request, exc):
    return public_error()
@app.post('/api/auth/signup')
def signup():
    try:
        db.commit()
    except SQLAlchemyError:
        return public_error()
"""
        ref = "source/main.py::public_error"
        rows = {path: {"edit": "The response uses the shared error builder.", "sourceRef": ref,
                       "recheck": "Measure this response on the submitted candidate."} for path in selected}
        provider = FakeProvider(self.write_response(content=source, path="source/main.py"), self.draft_response(repairPaths=rows))
        with patch("agents.runtime.repair_baseline.with_repair_baseline", projected):
            task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        review = self.parse_completed(task).change_report.ready_review
        self.assertEqual((review.build_outcome, review.validation_status), ("PASS", "PENDING_QA_SECURITY"))
        self.assertEqual([(row.path, row.source_ref, row.host_diff_status) for row in review.reported_path_edits],
                         [(path, ref, "LINKED_PENDING_TEST") for path in selected])

    async def test_actual_fix_build_artifacts_parent_and_lineage_are_measured(self):
        await self.prepare_fix()
        previous = self.previous
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=b"fixed inert fixture\n", stderr=b"")
        deadline = self.budget.deadline_monotonic
        provider = self.ready_provider(content="def signup():\n    return 'corrected-candidate'\n")
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        payload = json.loads(json.loads(provider.requests[0].input_items_json)[0]["content"])["taskInput"]
        self.assertEqual(payload["fileAccess"]["sourceReadMode"], "WORKING_COPY")
        self.assertIn({"repositoryPath": "main.py", "toolPath": "source/main.py"},
                      payload["fileAccess"]["startingFilePaths"])
        output = self.parse_completed(task)
        self.assertEqual(task["contextId"], self.previous_task["contextId"])
        self.assertNotEqual(task["id"], self.previous_task["id"])
        self.assertEqual(self.git("show", "-s", "--format=%P", output.source.commit_hash).strip(), previous.source.commit_hash)
        review = output.change_report.ready_review
        self.assertEqual((review.source_artifact_id, review.build_report_id), (output.source.artifact_id, output.build_report.artifact_id))
        self.assertEqual((review.build_outcome, review.validation_status), ("PASS", "PENDING_QA_SECURITY"))
        self.assertEqual(review.working_base_commit, previous.source.commit_hash)
        self.assertEqual([(e.path, e.changed_functions) for e in review.edits], [("source/signup.py", ("signup",))])
        self.assertIn("which processing paths", payload["runtimeInstructions"])
        from orchestrator.application.developer_output import DeveloperOutputValidationError
        forged = deepcopy(task)
        change = next(a for a in forged["artifacts"] if a["name"] == "change-report.json")
        change["parts"][0]["data"]["readyReview"]["buildOutcome"] = "FAIL"
        with self.assertRaises(DeveloperOutputValidationError):
            self.parse_completed(forged)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline_commit)
        for current, parent in ((output.source, previous.source), (output.change_report, previous.change_report),
                                (output.build_report, previous.build_report)):
            self.assertEqual(current.code_version, 2)
            self.assertEqual(current.artifact_version, parent.artifact_version + 1)
            self.assertEqual(current.previous_artifact_id, parent.artifact_id)
            self.assertNotEqual(current.artifact_id, parent.artifact_id)
        self.assertEqual(output.build_report.execution_manifest, output.source.execution_manifest())
        self.assertEqual(output.build_report.exit_code, 0)
        self.assertEqual(self.outputs.get(self.run.run_id, output.build_report.execution_manifest_id).source_artifact_id,
                         output.source.artifact_id)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls, 4)
        self.assertEqual(self.repository.get_run(self.run.run_id).status, WorkflowStatus.FIXING)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)
        self.assertEqual([name for name, _args in self.tool_calls()], ["write_source_file", "run_build"])
        self.assertEqual([(item.path, item.action) for item in output.change_report.changes], [("source/signup.py", "MODIFIED")])

    async def test_three_distinct_fix_candidates_keep_original_baseline_and_shared_budget(self):
        await self.prepare_fix()
        initial_source = self.previous.source
        deadline = self.budget.deadline_monotonic
        for number in range(1, 4):
            previous = self.previous
            task, _ = await self.run_to(self.ready_provider(content=f"def signup():\n    return 'candidate-{number + 1}'\n"), "TASK_STATE_COMPLETED")
            output = self.parse_completed(task)
            self.assertEqual(output.source.code_version, number + 1)
            self.assertEqual(output.source.previous_artifact_id, previous.source.artifact_id)
            self.assertEqual(self.git("show", "-s", "--format=%P", output.source.commit_hash).strip(), previous.source.commit_hash)
            self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline_commit)
            if number < 3:
                self.start_fix(task, output)
        self.assertEqual(self.budget.model_calls, 8)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.artifacts.get_snapshot(self.run.run_id, initial_source.artifact_id).snapshot_sha256,
                         initial_source.snapshot_sha256)
        with self.assertRaises(DeveloperContextError):
            self.context_copy(self.loader_context_for_max(), fix_attempt=4)

    def loader_context_for_max(self):
        # Last Source is now staged, so normal loader must deny uncertain replay.
        # Structural construction can still prove the three-cycle upper bound.
        return DeveloperExecutionContext(metadata=self.metadata, configuration=self.configuration, budget=self.budget,
            request_text=self.run.request_text, requirement_artifact=self.repository.get_planning_artifact(self.run.run_id),
            fix_attempt=self.run.fix_attempt, previous_source=self.previous.source,
            previous_artifacts=tuple(item for item in self.repository.list_project_artifacts(self.run.run_id)
                                     if item.artifact_id in self.step.input_artifact_ids),
            fix_issues=tuple(item for item in self.repository.list_issue_records(self.run.run_id)
                            if item.fix_workflow_step_id == self.step.workflow_step_id))

    async def test_same_bytes_fix_draft_cannot_fabricate_new_candidate_or_build(self):
        await self.prepare_fix()
        before = len(self.docker.calls)
        task, _ = await self.run_to(FakeProvider(self.draft_response()), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "LLM_DEVELOPMENT_NO_PROGRESS")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.calls), before)
        self.assertEqual(self.artifacts._contents.latest_source(self.run.run_id).artifact_id, self.previous.source.artifact_id)

    async def test_previous_frozen_integrity_check_prevents_changed_commit_or_private_bytes(self):
        await self.prepare_fix()
        context = self.loader(self.request())
        services = self.services_factory(context)
        checkpoint = await services.prepare(context)
        self.assertEqual(checkpoint._baseline_commit, self.baseline_commit)
        self.assertEqual(checkpoint._parent_commit, self.previous.source.commit_hash)
        self.assertEqual(checkpoint._parent.snapshot_sha256, self.previous.source.snapshot_sha256)
        self.assertEqual(sha256(self.artifacts.bind(self.run.run_id, role=AgentRole.DEVELOPER).read(
            self.previous.source.artifact_id).content).hexdigest(), checkpoint._parent.snapshot_sha256)

    async def test_frozen_baseline_or_lock_change_is_denied_without_model_writes(self):
        await self.prepare_fix()
        context = self.loader(self.request())
        services = self.services_factory(context)
        wrong = self.configuration.configuration.model_copy(update={"starting_commit_hash": self.previous.source.commit_hash})
        configured = self.configuration.model_copy(update={"configuration": wrong})
        with self.assertRaises(DeveloperServicesError):
            await services.prepare(replace(context, configuration=configured))
        (self.source / "requirements.lock").write_bytes(b"unapproved-lock-change==2\n")
        with self.assertRaises(DeveloperCheckpointError):
            await services.prepare(context)


if __name__ == "__main__":
    unittest.main()
