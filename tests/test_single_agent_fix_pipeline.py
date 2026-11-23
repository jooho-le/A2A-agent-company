"""Single identity/context through measured fixes; no live LLM or Docker.

Real A2A ASGI, Git candidates, SQLite, MCP ACLs, receipts and verdicts. The
generated inert Source/tests never execute on the Host.
"""

import asyncio
from contextlib import asynccontextmanager
import copy
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.single.prompts import build_single_system_prompt
from orchestrator.domain import AgentRole, FinalVerdict, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.sandbox.contracts import CLIResult
from semantic_helpers import SemanticFakeProvider as FakeProvider
from signup_receipt_fixture import generated_signup_evidence
from test_llm_runtime import ToolCall, text_response, tool_response
import test_single_agent_pipeline as single_fixture
import test_owned_agent_pipeline as fixtures


class _SingleFixHarness:
    def __init__(self, owner, *, qa_mode="initial-failure", security_failures=None,
                 build_failures=(), measured=True, cap=40):
        self.base = single_fixture.SingleAgentPipelineTests("runTest")
        self.base.setUp()
        for function, arguments, keywords in self.base._cleanups:
            owner.addCleanup(function, *arguments, **keywords)
        self.base._cleanups.clear()
        self.f = self.base.f
        self.f.configuration = self.f.configuration.model_copy(update={"limits": ExecutionLimits(runtime_budget_ms=60000)})
        self.base.measured_security = measured
        self.base.security_failures = security_failures or {}
        self.qa_mode, self.build_failures, self.cap = qa_mode, frozenset(build_failures), cap
        self.configurations, self.budgets, self.fix_inputs, self.samples = [], [], [], []
        self.developer_versions = {}
        original_services, original_peer = self.base.services, self.f.peer_client

        def services(role, execution):
            self.configurations.append(execution.configuration)
            self.budgets.append(execution.budget)
            self.samples.append((execution.budget.deadline_monotonic, execution.budget.model_calls))
            if role is AgentRole.DEVELOPER:
                self.developer_versions[execution.metadata.run_id] = execution.metadata.code_version
            return original_services(role, execution)

        @asynccontextmanager
        async def peer(configuration):
            async with original_peer(configuration) as client:
                role = configuration.binding.role
                docker = self.f.dockers[role][-1]
                if role is AgentRole.DEVELOPER:
                    # The private candidate exists before Build; Run.codeVersion
                    # is updated only after the Agent's output is ingested.
                    def build(container):
                        version = self.developer_versions[configuration.binding.run_id]
                        failed = int(version in self.build_failures)
                        docker.exit_code = failed
                        docker.start_result = CLIResult(returncode=failed, stdout=b"measured fixture build\n", stderr=b"")
                    docker.start_hook = build
                elif role is AgentRole.QA:
                    version = self.f.repository.get_run(configuration.binding.run_id).code_version
                    failure = self.failure_index(version)
                    failed = int(failure is not None)
                    ids = [f"test_signup.Signup.test_req_{index}" for index in range(1,
                        len(self.f.scenario.requirement_ids_for(RequirementValidator.QA)) + 1)]
                    docker.exit_code = failed
                    docker.start_result = CLIResult(returncode=failed, stdout=json.dumps({
                        "format": "unittest-v1", "total": len(ids), "passed": len(ids) - failed,
                        "failed": failed, "skipped": 0, "tests": [{"testId": identity,
                            "outcome": "FAIL" if index == failure else "PASS",
                            **generated_signup_evidence("QA", index + 1)} for index, identity in enumerate(ids)]
                    }).encode(), stderr=b"")
                yield client
        self.base.services, self.f.peer_client = services, peer

    def failure_index(self, version):
        if self.qa_mode == "pass" or self.qa_mode == "initial-failure" and version > 1:
            return None
        return version - 1 if self.qa_mode == "distinct-failures" else 0

    def provider(self):
        seed = self.base.provider().script
        def write(request):
            data = fixtures._task_input(request)
            payload = json.loads(json.loads(request.input_items_json)[0]["content"])
            version = payload["trusted"]["metadata"]["codeVersion"]
            if "fixRequest" in data:
                self.fix_inputs.append(data)
            return tool_response(ToolCall(call_id=f"single-write-{version}", name="write_source_file",
                arguments_json=json.dumps({"workspaceId": data["workspaceId"], "path": "source/signup.py",
                    "content": f"def signup():\n    return 'single-inert-candidate-{version}'\n",
                    "expectedSha256": fixtures.hashlib.sha256((self.f.directory / 'workspaces'
                        / data['workspaceId'] / 'source/signup.py').read_bytes()).hexdigest()})))
        script = [seed[0]]
        for version in range(1, 5):
            script.extend((write, seed[2]))
            if version not in self.build_failures:
                script.extend(seed[3:])
        return FakeProvider(*script)

    @asynccontextmanager
    async def running(self):
        async with self.base.running(cap=self.cap, tool_cap=80, provider=self.provider()) as values:
            yield values


class SingleAgentFixPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, harness):
        async with harness.running() as (client, provider):
            response = await asyncio.wait_for(client.post("/api/v1/runs", json=harness.f.submission()), timeout=300)
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()["run"]["runId"])
            run = harness.f.repository.get_run(run_id)
            steps = harness.f.repository.list_steps(run_id)
            artifacts = harness.f.repository.list_project_artifacts(run_id)
            config = harness.f.repository.get_run_configuration(run_id)
            budget = harness.base.platform.budgets.resolve(config)
            self.assertTrue(all(item is budget for item in harness.budgets))
            self.assertTrue(all(item == config for item in harness.configurations))
            self.assertEqual(len({deadline for deadline, _calls in harness.samples}), 1)
            self.assertEqual(budget.model_calls, len(provider.requests))
            self.assertEqual(len({step.agent_context_id for step in steps if step.a2a_task_id}), 1)
            mappings = harness.f.repository.list_agent_contexts(run_id)
            self.assertEqual([mapping.agent_id for mapping in mappings], ["single_agent"])
            self.assertEqual({request.system_prompt for request in provider.requests}, {build_single_system_prompt()})
            self.assertTrue(all(config.binding.role is SingleAgentIdentity.SINGLE_AGENT for config in harness.base.model_clients))
            self.assertEqual(len(harness.f.preparations), 1)
            return run, artifacts, steps, provider

    def assert_candidates(self, artifacts, count):
        sources = sorted((item for item in artifacts if item.artifact_type == "SOURCE"), key=lambda item: item.code_version)
        self.assertEqual([item.code_version for item in sources], list(range(1, count + 1)))
        self.assertEqual(len({item.artifact_id for item in sources}), count)
        self.assertEqual(len({item.commit_hash for item in sources}), count)
        self.assertEqual(len({item.snapshot_sha256 for item in sources}), count)
        self.assertEqual(sources[0].previous_artifact_id, None)
        self.assertEqual([item.previous_artifact_id for item in sources[1:]], [item.artifact_id for item in sources[:-1]])
        by_version = {item.code_version: item for item in sources}
        for report in artifacts:
            if report.artifact_type in {"BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"}:
                self.assertEqual(report.execution_manifest, by_version[report.code_version].execution_manifest())
        return sources

    async def test_qa_failure_fixed_in_same_single_context_with_new_candidate_and_reports(self):
        h = _SingleFixHarness(self)
        run, artifacts, steps, provider = await self.submit(h)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 1, 2))
        self.assertEqual(len(steps), 7)
        self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED for step in steps))
        self.assertEqual((len(h.fix_inputs), len(provider.requests)), (1, 20))
        self.assert_candidates(artifacts, 2)
        for kind in ("BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"):
            self.assertEqual(len([item for item in artifacts if item.artifact_type == kind]), 2)
        fix_request = next(request for request in provider.requests if
            json.loads(json.loads(request.input_items_json)[0]["content"])["trusted"]["phase"] == "FIXING")
        payload = json.loads(json.loads(fix_request.input_items_json)[0]["content"])
        initial_request = next(request for request in provider.requests if
            json.loads(json.loads(request.input_items_json)[0]["content"])["trusted"]["phase"] == "IMPLEMENTING")
        initial = json.loads(json.loads(initial_request.input_items_json)[0]["content"])
        self.assertEqual(payload["taskInput"]["fileAccess"], initial["taskInput"]["fileAccess"])
        focus = payload["taskInput"]["activeRepairFocus"]["brief"]
        self.assertEqual(focus['repairRound']['currentAttempt'], 1)
        self.assertTrue(focus['targets'])
        for target in focus['targets']:
            self.assertEqual(target['hostIssueProgress']['consecutiveRepeatCount'], 0)
            self.assertEqual(target['hostIssueProgress']['stopRepeatCount'], 0)
        names = {artifact["name"] for task in payload["history"] for artifact in task["artifacts"]}
        self.assertTrue({"qa-report.json", "security-report.json"} <= names, names)
        self.assertEqual(len(payload["history"]), 5)
        phases = [config.binding.single_phase for config in h.base.model_clients]
        self.assertEqual(phases, [SinglePhase.IMPLEMENTING, SinglePhase.QA, SinglePhase.SECURITY,
                                  SinglePhase.FIXING, SinglePhase.QA, SinglePhase.SECURITY])

    async def test_single_fixing_offers_only_open_for_an_unlinked_claim_and_publishes_it_as_open(self):
        from agents.llm.contracts import LLMRuntimeError
        from agents.runtime.repair_baseline import with_repair_baseline
        from agents.runtime.ready_path_diff import INPUT
        h = _SingleFixHarness(self)
        provider = h.provider()
        # This receipt fixture omits path detail. Supply the observed path and exercise the
        # production Single FIXING entry point, actual tool write and Git checkpoint.
        def project(value, checkpoint):
            result = with_repair_baseline(value, checkpoint)
            if 'fixRequest' in result:
                result['activeRepairFocus']['brief']['readyReportPaths'] = [INPUT]
            return result
        def decision(ref):
            return {'kind': 'READY', 'summary': 'Submit for revalidation.', 'questions': [], 'rejection': None,
                    'repairPaths': {INPUT: {'edit': 'Changed the route.' if ref else None, 'sourceRef': ref,
                                            'recheck': 'Check the rejection response.'}}}
        unsupported = decision('source/signup.py::signup')
        open_path = decision(None)
        slot = len(h.base.provider().script) + 1  # FIXING writes after all v1 role operations.
        provider.script[slot:slot + 1] = [text_response(json.dumps(unsupported)), text_response(json.dumps(open_path))]
        # The inert seed omits main.py; the production startup precheck is covered separately.
        with (patch('agents.runtime.repair_baseline.with_repair_baseline', project),
              patch('agents.single.runtime.start_precheck_for', return_value=None)):
            async with h.base.running(cap=40, tool_cap=80, provider=provider, corrections=1) as (client, _):
                response = await client.post('/api/v1/runs', json=h.f.submission())
        self.assertEqual(response.status_code, 201, response.text)
        run_id = UUID(response.json()['run']['runId'])
        self.assertEqual(h.f.repository.get_run(run_id).status, WorkflowStatus.FINISHED)
        corrections = [r for r in provider.requests if r.output.name == 'developer_decision'
                       and 'READY_PATH_EVIDENCE_MISMATCH' in r.input_items_json]
        self.assertEqual(len(corrections), 1)
        schema = corrections[0].output.schema
        schema.validate(open_path)
        with self.assertRaises(LLMRuntimeError):
            schema.validate(unsupported)
        report, = [item for item in h.f.repository.list_project_artifacts(run_id)
                   if item.artifact_type == 'CHANGE_REPORT' and item.code_version == 2]
        self.assertEqual(report.ready_review.reported_path_edits[0].host_diff_status, 'NO_CHANGE_CLAIMED')

    async def test_three_distinct_fixes_exhaust_limit_without_a_fourth_fix(self):
        h = _SingleFixHarness(self, qa_mode="distinct-failures")
        run, artifacts, steps, provider = await self.submit(h)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.FAIL, 3, 4))
        self.assertEqual((len(steps), len(h.fix_inputs), len(provider.requests)), (13, 3, 38))
        self.assert_candidates(artifacts, 4)

    async def test_same_issue_without_comparable_observations_uses_full_fix_limit(self):
        h = _SingleFixHarness(self, qa_mode="repeated-failure")
        run, artifacts, steps, provider = await self.submit(h)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.FAIL, 3, 4))
        self.assertEqual((len(steps), len(h.fix_inputs), len(provider.requests)), (13, 3, 38))
        self.assert_candidates(artifacts, 4)
        issues = sorted(h.f.repository.list_issue_records(run.run_id), key=lambda item: item.code_version)
        self.assertEqual([item.consecutive_repeat_count for item in issues], [0, 1, 2, 3])
        self.assertEqual(len({item.fingerprint for item in issues}), 1)
        events, _ = h.f.repository.list_events(run.run_id, limit=1000, offset=0)
        self.assertEqual(sum(e.event_type == 'SAME_ISSUE_REPEAT_LIMIT_REACHED' for e in events), 0)

    async def test_measured_security_failure_also_drives_single_fix_and_revalidation(self):
        h = _SingleFixHarness(self, qa_mode="pass", security_failures={1: 0})
        run, artifacts, steps, provider = await self.submit(h)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 1, 2))
        self.assertEqual(len(h.fix_inputs), 1)
        self.assert_candidates(artifacts, 2)
        self.assertEqual(h.fix_inputs[0]["fixRequest"]["attempt"], 1)

    async def test_build_failure_fixes_before_qa_or_security_and_preserves_source_lineage(self):
        h = _SingleFixHarness(self, qa_mode="pass", build_failures=(1,))
        run, artifacts, steps, provider = await self.submit(h)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 1, 2))
        self.assertEqual((len(steps), len(provider.requests), len(h.fix_inputs)), (5, 14, 1))
        self.assert_candidates(artifacts, 2)
        builds = sorted((item for item in artifacts if item.artifact_type == "BUILD_REPORT"), key=lambda item: item.code_version)
        self.assertEqual([item.passed for item in builds], [False, True])
        self.assertTrue(all(item.code_version == 2 for item in artifacts if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}))

    async def test_fix_never_refills_spent_model_budget(self):
        h = _SingleFixHarness(self, cap=11)
        run, artifacts, steps, provider = await self.submit(h)
        self.assertEqual((run.status, run.fix_attempt, run.code_version), (WorkflowStatus.HUMAN_REVIEW, 1, 1))
        self.assertEqual(len(provider.requests), 11)
        self.assertFalse(h.fix_inputs)
        self.assert_candidates(artifacts, 1)

    async def test_missing_security_proof_stays_unverified_instead_of_running_unproven_fixes(self):
        h = _SingleFixHarness(self, qa_mode="pass", measured=False)
        run, artifacts, steps, provider = await self.submit(h)
        self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertNotEqual(run.verdict, FinalVerdict.SUCCESS)
        self.assertEqual((run.fix_attempt, run.code_version, len(h.fix_inputs)), (0, 1, 0))

    async def test_foreign_context_is_rejected_before_fix_model_or_source_write(self):
        original = SQLiteDeveloperContextLoader.__call__
        def foreign(loader, context):
            metadata = context.metadata
            run = loader._repository.get_run(UUID(metadata["runId"]))
            if run.fix_attempt:
                context = copy.copy(context)
                context._context_id = "foreign-single-context"
            return original(loader, context)
        h = _SingleFixHarness(self)
        with patch.object(SQLiteDeveloperContextLoader, "__call__", foreign):
            run, artifacts, steps, provider = await self.submit(h)
        self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual((len(provider.requests), len(h.fix_inputs), run.code_version), (11, 0, 1))
        self.assert_candidates(artifacts, 1)

    async def test_historical_mapping_is_not_accepted_as_the_latest_single_task(self):
        original = SQLiteDeveloperContextLoader.__call__
        def stale(loader, context):
            run_id = UUID(context.metadata["runId"])
            run = loader._repository.get_run(run_id)
            if run.fix_attempt:
                planner = next(step for step in loader._repository.list_steps(run_id) if step.agent_role is AgentRole.PLANNER)
                mapping = next(item for item in loader._repository.list_agent_contexts(run_id) if item.agent_id == "single_agent")
                corrupted = mapping.model_copy(update={"latest_a2a_task_id": planner.a2a_task_id})
                with loader._repository._transaction() as connection:
                    connection.execute("UPDATE agent_contexts SET payload_json=? WHERE run_id=? AND agent_id=?",
                        (corrupted.model_dump_json(), str(run_id), "single_agent"))
            return original(loader, context)
        # A failed Host build provides the first repair immediately. The previous
        # Developer task is now the latest permitted predecessor; the older Planner is not.
        h = _SingleFixHarness(self, qa_mode="pass", build_failures=(1,))
        with patch.object(SQLiteDeveloperContextLoader, "__call__", stale):
            run, artifacts, steps, provider = await self.submit(h)
        self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual((len(h.fix_inputs), run.fix_attempt, run.code_version), (0, 1, 1))
        self.assertEqual([s.agent_role for s in steps], [AgentRole.PLANNER, AgentRole.DEVELOPER, AgentRole.DEVELOPER])
        self.assertEqual(len(provider.requests), 5)
        self.assert_candidates(artifacts, 1)


if __name__ == "__main__":
    unittest.main()
